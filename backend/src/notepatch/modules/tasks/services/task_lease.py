from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import timedelta
import logging
import threading
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from notepatch.modules.tasks.models.task import Task
from notepatch.modules.tasks.services.queue import queue_name_for_task_type, redis_key_for_queue
from notepatch.modules.tasks.services.task import TaskService
from notepatch.platform.config import get_settings
from notepatch.platform.database import utcnow

logger = logging.getLogger(__name__)

_RENEW_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
  return redis.call("expire", KEYS[1], ARGV[2])
end
return 0
"""
_RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
  return redis.call("del", KEYS[1])
end
return 0
"""


def task_lease_key(task_id: str) -> str:
    return f"notepatch:task-lease:{task_id}"


class TaskLease(AbstractContextManager):
    def __init__(self, client, task_id: str) -> None:
        self.client = client
        self.task_id = task_id
        self.settings = get_settings()
        self.token = str(uuid.uuid4())
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.acquired = False

    def __enter__(self):
        ttl = max(int(self.settings.task_worker_lease_seconds), 10)
        self.acquired = bool(self.client.set(task_lease_key(self.task_id), self.token, nx=True, ex=ttl))
        if not self.acquired:
            return self
        self._thread = threading.Thread(target=self._renew_loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self.acquired:
            try:
                self.client.eval(_RELEASE_SCRIPT, 1, task_lease_key(self.task_id), self.token)
            except Exception as release_error:
                logger.warning("Could not release task lease %s: %s", self.task_id, release_error)
        return False

    def _renew_loop(self) -> None:
        ttl = max(int(self.settings.task_worker_lease_seconds), 10)
        interval = max(ttl / 3, 1)
        while not self._stop.wait(interval):
            try:
                renewed = self.client.eval(
                    _RENEW_SCRIPT,
                    1,
                    task_lease_key(self.task_id),
                    self.token,
                    ttl,
                )
                if not renewed:
                    logger.error("Lost task lease for %s", self.task_id)
                    return
            except Exception as exc:
                logger.warning("Could not renew task lease %s: %s", self.task_id, exc)


def recover_orphaned_tasks(client, db: Session, queue_names: list[str]) -> int:
    settings = get_settings()
    cutoff = utcnow() - timedelta(seconds=max(settings.task_orphan_recovery_grace_seconds, 1))
    tasks = db.scalars(
        select(Task).where(
            Task.status == "running",
            Task.started_at.is_not(None),
            Task.started_at <= cutoff,
            Task.cancel_requested_at.is_(None),
        )
    ).all()
    recovered = 0
    for task in tasks:
        queue_name = queue_name_for_task_type(settings, task.task_type)
        if queue_name not in queue_names or client.get(task_lease_key(task.id)) is not None:
            continue
        service = TaskService(db)
        if task.attempt >= task.max_attempts:
            service.mark_failed(task, "Worker exited and task attempt limit was exhausted")
            recovered += 1
            continue
        task.status = "queued"
        task.started_at = None
        task.next_attempt_at = None
        task.error_message = "Previous worker lease expired; task requeued"
        task.updated_at = utcnow()
        service.add_event(
            task,
            "orphan_requeued",
            "Task requeued after its worker lease expired",
            level="warning",
            data={"attempt": task.attempt, "queue": queue_name},
        )
        db.commit()
        try:
            client.rpush(redis_key_for_queue(settings, queue_name), task.id)
        except Exception as exc:
            db.refresh(task)
            service.mark_failed(task, f"Task queue unavailable during orphan recovery: {exc}")
        recovered += 1
    return recovered

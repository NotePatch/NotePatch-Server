import logging
from datetime import timedelta

import redis
from fastapi import HTTPException, status
from sqlalchemy import inspect as sqlalchemy_inspect, select, update
from sqlalchemy.orm import Session

from notepatch.platform.config import get_settings
from notepatch.platform.database import utcnow
from notepatch.modules.tasks.models.task import TASK_TYPES, Task, TaskEvent
from notepatch.platform.errors import TaskCancelledError
from notepatch.modules.tasks.services.queue import queue_name_for_task_type, redis_key_for_queue, retry_key_for_queue

logger = logging.getLogger(__name__)


class TaskService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.settings = get_settings()

    def create_task(
        self,
        *,
        workspace_id: str,
        task_type: str,
        resource_type: str | None = None,
        resource_id: str | None = None,
        payload: dict | None = None,
        queue_name: str | None = None,
        enqueue: bool = True,
        max_attempts: int | None = None,
    ) -> Task:
        task, queue_name = self.create_task_record(
            workspace_id=workspace_id,
            task_type=task_type,
            resource_type=resource_type,
            resource_id=resource_id,
            payload=payload,
            queue_name=queue_name,
            max_attempts=max_attempts,
        )
        self.db.commit()
        self.db.refresh(task)

        if enqueue and not self.enqueue_task(task.id, queue_name=queue_name):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Task queue is unavailable",
            )
        return task

    def create_task_record(
        self,
        *,
        workspace_id: str,
        task_type: str,
        resource_type: str | None = None,
        resource_id: str | None = None,
        payload: dict | None = None,
        queue_name: str | None = None,
        max_attempts: int | None = None,
    ) -> tuple[Task, str]:
        if task_type not in TASK_TYPES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unsupported task type: {task_type}")
        queue_name = queue_name or queue_name_for_task_type(self.settings, task_type)
        redis_key = redis_key_for_queue(self.settings, queue_name)

        task = Task(
            workspace_id=workspace_id,
            task_type=task_type,
            resource_type=resource_type,
            resource_id=resource_id,
            payload=payload or {},
            status="queued",
            progress=0,
            attempt=0,
            max_attempts=max_attempts or self.settings.task_max_attempts,
        )
        self.db.add(task)
        self.db.flush()
        self.add_event(task, "queued", "Task queued", progress=0, data={"queue": queue_name, "redis_key": redis_key})
        return task, queue_name

    def add_event(
        self,
        task: Task,
        event_type: str,
        message: str,
        *,
        level: str = "info",
        progress: int | None = None,
        data: dict | None = None,
    ) -> TaskEvent:
        event = TaskEvent(
            workspace_id=task.workspace_id,
            task_id=task.id,
            event_type=event_type,
            level=level,
            message=message,
            progress=progress,
            data=data or {},
        )
        self.db.add(event)
        if progress is not None:
            task.progress = progress
        return event

    def find_active_task(
        self,
        *,
        workspace_id: str,
        task_type: str,
        resource_type: str,
        resource_id: str,
    ) -> Task | None:
        return self.db.scalar(
            select(Task)
            .where(
                Task.workspace_id == workspace_id,
                Task.task_type == task_type,
                Task.resource_type == resource_type,
                Task.resource_id == resource_id,
                Task.status.in_(("queued", "running")),
                Task.cancel_requested_at.is_(None),
            )
            .order_by(Task.created_at.desc())
        )

    def cancel_active_tasks(
        self,
        *,
        workspace_id: str,
        resource_type: str,
        resource_id: str,
        reason: str,
        task_types: tuple[str, ...] | None = None,
        commit: bool = True,
    ) -> list[Task]:
        query = select(Task).where(
            Task.workspace_id == workspace_id,
            Task.resource_type == resource_type,
            Task.resource_id == resource_id,
            Task.status.in_(("queued", "running")),
        )
        if task_types:
            query = query.where(Task.task_type.in_(task_types))
        tasks = self.db.scalars(query).all()
        for task in tasks:
            self.request_cancel(task, reason, commit=False)
        if commit and tasks:
            self.db.commit()
        return tasks

    def enqueue_task(self, task_id: str, queue_name: str | None = None) -> bool:
        queue_name = queue_name or self.settings.default_queue_name
        redis_key = redis_key_for_queue(self.settings, queue_name)
        try:
            client = redis.from_url(self.settings.redis_url, socket_connect_timeout=1, socket_timeout=1)
            client.rpush(redis_key, task_id)
            return True
        except Exception as exc:  # Redis may be unavailable during unit tests or local dry runs.
            logger.warning("Could not enqueue task %s to queue %s: %s", task_id, redis_key, exc)
            task = self.db.get(Task, task_id)
            if task is not None:
                task.status = "failed"
                task.error_message = f"Task queue unavailable: {exc}"
                task.finished_at = utcnow()
                self.add_event(
                    task,
                    "queue_failed",
                    "Task could not be pushed to Redis",
                    level="error",
                    data={"queue": queue_name, "redis_key": redis_key, "error": str(exc)},
                )
                self.db.commit()
            return False

    def claim_task(self, task_id: str) -> Task | None:
        now = utcnow()
        result = self.db.execute(
            update(Task)
            .where(Task.id == task_id, Task.status == "queued", Task.cancel_requested_at.is_(None))
            .values(
                status="running",
                attempt=Task.attempt + 1,
                next_attempt_at=None,
                started_at=now,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            self.db.rollback()
            return None
        self.db.expire_all()
        task = self.db.get(Task, task_id)
        if task is None:
            self.db.rollback()
            return None
        self.add_event(
            task,
            "running",
            "Task started",
            progress=task.progress,
            data={"attempt": task.attempt, "max_attempts": task.max_attempts},
        )
        self.db.commit()
        self.db.refresh(task)
        return task

    def ensure_active(self, task: Task) -> None:
        current = task
        if sqlalchemy_inspect(task).persistent:
            self.db.refresh(task, attribute_names=["status", "cancel_requested_at"])
        else:
            persisted = self.db.get(Task, task.id)
            if persisted is not None:
                current = persisted
        if current.status == "cancelled" or current.cancel_requested_at is not None:
            raise TaskCancelledError("Task cancellation was requested")

    def request_cancel(self, task: Task, reason: str, *, commit: bool = True) -> bool:
        if task.status in {"succeeded", "failed", "cancelled"}:
            return False
        now = utcnow()
        task.next_attempt_at = None
        if task.status == "running":
            if task.cancel_requested_at is None:
                task.cancel_requested_at = now
                self.add_event(
                    task,
                    "cancellation_requested",
                    "Task cancellation requested",
                    level="warning",
                    data={"reason": reason},
                )
        else:
            task.status = "cancelled"
            task.cancel_requested_at = now
            task.finished_at = now
            self.add_event(
                task,
                "cancelled",
                "Task cancelled",
                level="warning",
                data={"reason": reason},
            )
        task.updated_at = now
        self._remove_from_redis(task)
        if commit:
            self.db.commit()
        return True

    def mark_cancelled(self, task: Task, reason: str = "Task cancellation was requested") -> None:
        if task.status == "cancelled":
            return
        now = utcnow()
        task.status = "cancelled"
        task.cancel_requested_at = task.cancel_requested_at or now
        task.next_attempt_at = None
        task.finished_at = now
        task.updated_at = now
        self.add_event(task, "cancelled", "Task cancelled", level="warning", data={"reason": reason})
        self._remove_from_redis(task)
        self.db.commit()

    def _remove_from_redis(self, task: Task) -> None:
        queue_name = queue_name_for_task_type(self.settings, task.task_type)
        queue_key = redis_key_for_queue(self.settings, queue_name)
        retry_key = retry_key_for_queue(self.settings, queue_name)
        try:
            client = redis.from_url(self.settings.redis_url, socket_connect_timeout=1, socket_timeout=1)
            client.lrem(queue_key, 0, task.id)
            client.zrem(retry_key, task.id)
        except Exception as exc:
            logger.warning("Could not remove cancelled task %s from Redis: %s", task.id, exc)

    def schedule_retry(self, task: Task, error: str) -> bool:
        self.db.refresh(task, attribute_names=["status", "cancel_requested_at"])
        if task.status == "cancelled" or task.cancel_requested_at is not None:
            self.mark_cancelled(task)
            return False
        if task.attempt >= task.max_attempts:
            return False
        delay = min(
            self.settings.task_retry_base_seconds * (2 ** max(task.attempt - 1, 0)),
            self.settings.task_retry_max_seconds,
        )
        next_attempt = utcnow() + timedelta(seconds=delay)
        queue_name = queue_name_for_task_type(self.settings, task.task_type)
        retry_key = retry_key_for_queue(self.settings, queue_name)
        task.status = "queued"
        task.error_message = error
        task.next_attempt_at = next_attempt
        task.finished_at = None
        task.updated_at = utcnow()
        self.add_event(
            task,
            "retry_scheduled",
            "Task retry scheduled",
            level="warning",
            data={
                "attempt": task.attempt,
                "max_attempts": task.max_attempts,
                "delay_seconds": delay,
                "next_attempt_at": next_attempt.isoformat(),
                "error": error,
            },
        )
        self.db.commit()
        try:
            client = redis.from_url(self.settings.redis_url, socket_connect_timeout=1, socket_timeout=1)
            client.zadd(retry_key, {task.id: next_attempt.timestamp()})
            return True
        except Exception as exc:
            logger.error("Could not schedule task %s retry in %s: %s", task.id, retry_key, exc)
            return False

    def mark_succeeded(self, task: Task, result: dict | None = None) -> None:
        now = utcnow()
        payload = result or {}
        self.db.flush()
        claimed = self.db.execute(
            update(Task)
            .where(
                Task.id == task.id,
                Task.status == "running",
                Task.cancel_requested_at.is_(None),
            )
            .values(
                status="succeeded",
                progress=100,
                result=payload,
                error_message=None,
                finished_at=now,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            self.db.rollback()
            current = self.db.get(Task, task.id)
            if current is not None and (current.status == "cancelled" or current.cancel_requested_at is not None):
                self.mark_cancelled(current)
                raise TaskCancelledError("Task cancellation won the completion race")
            return
        self.db.refresh(task)
        self.add_event(task, "succeeded", "Task succeeded", progress=100, data=task.result)
        self.db.commit()

    def mark_failed(self, task: Task, error: str) -> None:
        now = utcnow()
        self.db.flush()
        failed = self.db.execute(
            update(Task)
            .where(
                Task.id == task.id,
                Task.status.in_(("queued", "running")),
                Task.cancel_requested_at.is_(None),
            )
            .values(status="failed", error_message=error, finished_at=now, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        if failed.rowcount != 1:
            self.db.rollback()
            current = self.db.get(Task, task.id)
            if current is not None and (current.status == "cancelled" or current.cancel_requested_at is not None):
                self.mark_cancelled(current)
            return
        self.db.refresh(task)
        self.add_event(task, "failed", "Task failed", level="error", data={"error": error})
        self.db.commit()

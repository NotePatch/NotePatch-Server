import argparse
import logging
import signal
import sys
import time

import redis

from notepatch.platform.config import get_settings
from notepatch.platform.database import SessionLocal
from notepatch.modules.documents.ocr import OcrPipeline
from notepatch.modules.tasks.models.task import Task
from notepatch.modules.tasks.services.queue import (
    parse_queue_names,
    queue_name_for_task_type,
    redis_key_for_queue,
    redis_keys_for_queue_names,
    promote_due_retries,
)
from notepatch.modules.tasks.services.executor import process_task
from notepatch.modules.tasks.services.task_lease import TaskLease, recover_orphaned_tasks

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

running = True


def _handle_stop(signum, frame) -> None:
    global running
    running = False


def queue_names_from_args(value: str | None) -> list[str]:
    settings = get_settings()
    return parse_queue_names(value or settings.worker_queues, default=settings.default_queue_name)


def redis_keys_for_worker_queues(queue_names: list[str]) -> list[str]:
    return redis_keys_for_queue_names(get_settings(), queue_names)


def ocr_pipeline_for_worker_queues(queue_names: list[str]) -> OcrPipeline | None:
    return OcrPipeline() if get_settings().ocr_queue_name in queue_names else None


def _decode_redis_value(value) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _requeue_if_misrouted(client, db, task_id: str, popped_key: str) -> bool:
    settings = get_settings()
    task = db.get(Task, task_id)
    if task is None:
        return False
    expected_queue = queue_name_for_task_type(settings, task.task_type)
    expected_key = redis_key_for_queue(settings, expected_queue)
    if popped_key == expected_key:
        return False
    client.rpush(expected_key, task_id)
    logger.warning(
        "Task %s of type %s was popped from %s but belongs on %s; requeued",
        task_id,
        task.task_type,
        popped_key,
        expected_key,
    )
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the NotePatch async worker.")
    parser.add_argument("--queues", default=None, help="Comma-separated logical queue names to consume.")
    args = parser.parse_args(argv)

    settings = get_settings()
    queue_names = queue_names_from_args(args.queues)
    redis_keys = redis_keys_for_worker_queues(queue_names)
    client = redis.from_url(settings.redis_url)
    ocr_pipeline = ocr_pipeline_for_worker_queues(queue_names)
    logger.info("Worker listening on queues %s (%s)", ", ".join(queue_names), ", ".join(redis_keys))
    last_orphan_recovery = 0.0

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    while running:
        try:
            promote_due_retries(client, settings, queue_names)
        except Exception as exc:
            logger.warning("Could not promote delayed retries: %s", exc)
        now = time.monotonic()
        if now - last_orphan_recovery >= settings.task_orphan_recovery_interval_seconds:
            try:
                with SessionLocal() as recovery_db:
                    recovered = recover_orphaned_tasks(client, recovery_db, queue_names)
                if recovered:
                    logger.warning("Recovered %s orphaned task(s)", recovered)
            except Exception as exc:
                logger.warning("Could not recover orphaned tasks: %s", exc)
            last_orphan_recovery = now
        item = client.brpop(redis_keys, timeout=5)
        if item is None:
            continue
        raw_queue_key, raw_task_id = item
        queue_key = _decode_redis_value(raw_queue_key)
        task_id = _decode_redis_value(raw_task_id)
        logger.info("Processing task %s from queue %s", task_id, queue_key)
        with SessionLocal() as db:
            if _requeue_if_misrouted(client, db, task_id, queue_key):
                continue
        with TaskLease(client, task_id) as lease:
            if not lease.acquired:
                logger.warning("Task %s already has an active worker lease; skipped duplicate queue item", task_id)
                continue
            with SessionLocal() as db:
                process_task(db, task_id, ocr_pipeline=ocr_pipeline)
    logger.info("Worker stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())

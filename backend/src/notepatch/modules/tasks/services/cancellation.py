from __future__ import annotations

import logging

import redis

from notepatch.platform.config import get_settings

logger = logging.getLogger(__name__)


def cancellation_signal_key(task_id: str) -> str:
    return f"notepatch:task-cancel:{task_id}"


def signal_task_cancellation(task_id: str) -> None:
    """Best-effort fast-path signal; the database remains the source of truth."""
    settings = get_settings()
    try:
        client = redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=1)
        client.set(cancellation_signal_key(task_id), "1", ex=max(settings.task_cancellation_grace_seconds, 60))
    except Exception as exc:
        logger.warning("Could not publish cancellation signal for task %s: %s", task_id, exc)


def is_task_cancellation_signalled(task_id: str) -> bool:
    settings = get_settings()
    try:
        client = redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=1)
        return client.get(cancellation_signal_key(task_id)) is not None
    except Exception as exc:
        logger.warning("Could not read cancellation signal for task %s: %s", task_id, exc)
        return False

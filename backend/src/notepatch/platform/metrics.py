from __future__ import annotations

import time

import redis
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

from notepatch.platform.config import get_settings

HTTP_REQUESTS = Counter("notepatch_http_requests_total", "HTTP requests", ["method", "route", "status"])
HTTP_LATENCY = Histogram("notepatch_http_request_duration_seconds", "HTTP request latency", ["method", "route"])
TASK_TERMINALS = Counter("notepatch_tasks_terminal_total", "Terminal tasks", ["task_type", "status"])
TASK_DURATION = Histogram("notepatch_task_duration_seconds", "Task runtime", ["task_type", "status"])
QUEUE_LENGTH = Gauge("notepatch_queue_length", "Redis task queue length", ["queue"])
SSE_CONNECTIONS = Gauge("notepatch_sse_connections", "Active task SSE connections")
GPU_LEASE_EVENTS = Counter("notepatch_gpu_lease_events_total", "GPU lease events", ["event"])
SCAN_RESULTS = Counter("notepatch_document_scan_total", "Document scan results", ["status"])


def observe_task(task, terminal_status: str) -> None:
    TASK_TERMINALS.labels(task.task_type, terminal_status).inc()
    if task.started_at:
        started = task.started_at.timestamp()
        TASK_DURATION.labels(task.task_type, terminal_status).observe(max(time.time() - started, 0))


def refresh_queue_metrics() -> None:
    settings = get_settings()
    client = redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=1)
    for queue in {
        settings.default_queue_name,
        settings.ocr_queue_name,
        settings.chat_queue_name,
        settings.ai_queue_name,
    }:
        key = settings.redis_task_queue if queue == settings.default_queue_name else f"{settings.redis_task_queue}:{queue}"
        QUEUE_LENGTH.labels(queue).set(client.llen(key))


def render_metrics() -> tuple[bytes, str]:
    try:
        refresh_queue_metrics()
    except Exception:
        pass
    return generate_latest(), CONTENT_TYPE_LATEST

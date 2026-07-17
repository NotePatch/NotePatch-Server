from __future__ import annotations

import time

import httpx
import redis
from sqlalchemy import literal_column, select
from sqlalchemy.orm import Session

from notepatch.modules.admin.schemas.admin import AdminQueueStatus, AdminServiceStatus
from notepatch.modules.tasks.services.queue import redis_key_for_queue
from notepatch.platform.config import get_settings
from notepatch.platform.storage import StorageService


def queue_statuses() -> list[AdminQueueStatus]:
    settings = get_settings()
    names = [settings.default_queue_name, settings.ocr_queue_name, settings.chat_queue_name]
    try:
        client = redis.from_url(
            settings.redis_url,
            socket_connect_timeout=1,
            socket_timeout=1,
            decode_responses=True,
        )
        return [
            AdminQueueStatus(
                name=name,
                redis_key=redis_key_for_queue(settings, name),
                length=int(client.llen(redis_key_for_queue(settings, name))),
                status="ok",
            )
            for name in names
        ]
    except Exception as exc:
        return [
            AdminQueueStatus(
                name=name,
                redis_key=redis_key_for_queue(settings, name),
                length=None,
                status="degraded",
                error=str(exc),
            )
            for name in names
        ]


def _status(name: str, callback) -> AdminServiceStatus:
    started = time.perf_counter()
    try:
        detail = callback()
        status = "ok"
    except Exception as exc:
        detail = str(exc)
        status = "degraded"
    return AdminServiceStatus(
        name=name,
        status=status,
        detail=detail,
        latency_ms=round((time.perf_counter() - started) * 1000, 2),
    )


def _http_health(url: str, token: str | None = None) -> str:
    headers = {"Authorization": f"Bearer {token}"} if token else None
    response = httpx.get(url.rstrip("/") + "/healthz", headers=headers, timeout=2.0)
    response.raise_for_status()
    return response.text[:200]


def service_statuses(db: Session, storage: StorageService) -> list[AdminServiceStatus]:
    settings = get_settings()
    services = [
        _status("database", lambda: str(db.execute(select(literal_column("1"))).scalar_one())),
        _status(
            "redis",
            lambda: str(
                redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=1).ping()
            ),
        ),
        _status(
            "seaweedfs",
            lambda: "bucket ok" if storage.bucket_exists(storage.bucket) else _raise("bucket missing"),
        ),
    ]
    if settings.doctr_enabled:
        services.append(_status("doctr", lambda: _http_health(settings.doctr_base_url)))
    services.append(_status("embedding", lambda: _http_health(settings.embedding_service_url)))
    services.append(
        _status(
            "openclaw",
            lambda: _http_health(settings.openclaw_gateway_base_url, settings.openclaw_gateway_token),
        )
    )
    return services


def _raise(message: str):
    raise RuntimeError(message)

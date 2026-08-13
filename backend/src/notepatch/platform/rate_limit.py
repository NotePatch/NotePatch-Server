from __future__ import annotations

import logging
import time

import redis
from fastapi import HTTPException, status

from notepatch.platform.config import Settings, get_settings

logger = logging.getLogger(__name__)


class RateLimiter:
    def __init__(self, settings: Settings | None = None, client=None) -> None:
        self.settings = settings or get_settings()
        self.client = client

    def check(self, scope: str, identity: str, limit: int, window_seconds: int = 60) -> None:
        if not self.settings.rate_limit_enabled or limit <= 0:
            return
        bucket = int(time.time()) // window_seconds
        key = f"notepatch:rate:{scope}:{identity}:{bucket}"
        try:
            client = self.client or redis.from_url(
                self.settings.redis_url, socket_connect_timeout=1, socket_timeout=1
            )
            pipeline = client.pipeline()
            pipeline.incr(key)
            pipeline.expire(key, window_seconds + 2)
            count, _ = pipeline.execute()
        except Exception as exc:
            logger.warning("Rate limiter unavailable for %s: %s", scope, exc)
            return
        if int(count) > limit:
            retry_after = window_seconds - (int(time.time()) % window_seconds)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded",
                headers={"Retry-After": str(max(retry_after, 1))},
            )

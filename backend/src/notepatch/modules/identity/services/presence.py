from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone

import redis

from notepatch.platform.config import get_settings


class PresenceService:
    session_prefix = "notepatch:presence:user:"
    last_seen_key = "notepatch:presence:last_seen"

    def __init__(self, redis_client=None) -> None:
        self.settings = get_settings()
        self.redis = redis_client or redis.from_url(
            self.settings.redis_url,
            socket_connect_timeout=1,
            socket_timeout=1,
            decode_responses=True,
        )

    def heartbeat(self, user_id: str, client_id: str | None = None) -> dict:
        client_id = client_id or str(uuid.uuid4())
        now = int(time.time())
        ttl = self.settings.presence_session_ttl_seconds
        payload = {
            "user_id": user_id,
            "client_id": client_id,
            "last_seen": now,
        }
        self.redis.setex(self.session_key(user_id, client_id), ttl, json.dumps(payload, separators=(",", ":")))
        self.redis.zadd(self.last_seen_key, {user_id: now})
        return {
            "client_id": client_id,
            "online_until": datetime.fromtimestamp(now + ttl, tz=timezone.utc),
            "heartbeat_interval_seconds": self.settings.presence_heartbeat_interval_seconds,
        }

    def offline(self, user_id: str, client_id: str) -> None:
        now = int(time.time())
        self.redis.delete(self.session_key(user_id, client_id))
        self.redis.zadd(self.last_seen_key, {user_id: now})

    def active_client_ids(self, user_id: str) -> set[str]:
        client_ids: set[str] = set()
        pattern = f"{self.session_prefix}{user_id}:session:*"
        for key in self.redis.scan_iter(match=pattern):
            parsed = self._parse_session_key(self._decode(key))
            if parsed is not None:
                client_ids.add(parsed[1])
        return client_ids

    def online_user_ids(self) -> set[str]:
        user_ids: set[str] = set()
        pattern = f"{self.session_prefix}*:session:*"
        for key in self.redis.scan_iter(match=pattern):
            parsed = self._parse_session_key(self._decode(key))
            if parsed is not None:
                user_ids.add(parsed[0])
        return user_ids

    def tracked_user_ids(self) -> set[str]:
        values = self.redis.zrange(self.last_seen_key, 0, -1)
        return {self._decode(value) for value in values}

    def last_seen_epoch(self, user_id: str) -> float | None:
        value = self.redis.zscore(self.last_seen_key, user_id)
        return float(value) if value is not None else None

    def is_user_online(self, user_id: str) -> bool:
        return bool(self.active_client_ids(user_id))

    @classmethod
    def session_key(cls, user_id: str, client_id: str) -> str:
        return f"{cls.session_prefix}{user_id}:session:{client_id}"

    @classmethod
    def _parse_session_key(cls, key: str) -> tuple[str, str] | None:
        if not key.startswith(cls.session_prefix):
            return None
        remainder = key[len(cls.session_prefix) :]
        marker = ":session:"
        if marker not in remainder:
            return None
        user_id, client_id = remainder.split(marker, 1)
        if not user_id or not client_id:
            return None
        return user_id, client_id

    @staticmethod
    def _decode(value) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return str(value)

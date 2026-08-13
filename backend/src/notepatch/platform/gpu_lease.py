from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager

import redis

from notepatch.platform.config import get_settings
from notepatch.platform.errors import RetryableTaskError
from notepatch.platform.metrics import GPU_LEASE_EVENTS


class GpuLeaseError(RetryableTaskError):
    pass


class GpuLeaseService:
    def __init__(self, client=None) -> None:
        self.settings = get_settings()
        self.client = client or redis.from_url(
            self.settings.redis_url,
            socket_connect_timeout=2,
            socket_timeout=2,
            decode_responses=True,
        )

    @contextmanager
    def lease(self, *, owner: str, event_callback: Callable[[str, dict], None] | None = None):
        raw_callback = event_callback or (lambda _event, _data: None)

        def callback(event: str, data: dict) -> None:
            GPU_LEASE_EVENTS.labels(event).inc()
            raw_callback(event, data)
        if not self.settings.gpu_lock_enabled:
            yield
            return
        token = f"{owner}:{secrets.token_hex(16)}"
        deadline = time.monotonic() + self.settings.gpu_lock_wait_seconds
        callback("gpu_lease_waiting", {"owner": owner, "key": self.settings.gpu_lock_key})
        while time.monotonic() <= deadline:
            try:
                acquired = self.client.set(
                    self.settings.gpu_lock_key,
                    token,
                    nx=True,
                    ex=self.settings.gpu_lock_lease_seconds,
                )
            except Exception as exc:
                raise GpuLeaseError(f"Redis GPU lease is unavailable: {exc}") from exc
            if acquired:
                break
            time.sleep(0.5)
        else:
            callback(
                "gpu_lease_timeout",
                {
                    "owner": owner,
                    "key": self.settings.gpu_lock_key,
                    "wait_seconds": self.settings.gpu_lock_wait_seconds,
                },
            )
            raise GpuLeaseError(
                f"Timed out waiting {self.settings.gpu_lock_wait_seconds}s for shared GPU lease"
            )

        callback("gpu_lease_acquired", {"owner": owner, "key": self.settings.gpu_lock_key})
        stop = threading.Event()
        renewer = threading.Thread(target=self._renew_loop, args=(token, stop), daemon=True)
        renewer.start()
        try:
            yield
        finally:
            stop.set()
            renewer.join(timeout=2)
            self._release(token)
            callback("gpu_lease_released", {"owner": owner, "key": self.settings.gpu_lock_key})

    def _renew_loop(self, token: str, stop: threading.Event) -> None:
        interval = max(self.settings.gpu_lock_lease_seconds / 3, 1)
        while not stop.wait(interval):
            try:
                renewed = self.client.eval(
                    "if redis.call('get', KEYS[1]) == ARGV[1] then "
                    "return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end",
                    1,
                    self.settings.gpu_lock_key,
                    token,
                    self.settings.gpu_lock_lease_seconds,
                )
                if not renewed:
                    return
            except Exception:
                return

    def _release(self, token: str) -> None:
        try:
            self.client.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then "
                "return redis.call('del', KEYS[1]) else return 0 end",
                1,
                self.settings.gpu_lock_key,
                token,
            )
        except Exception:
            return

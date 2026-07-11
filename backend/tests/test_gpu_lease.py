from notepatch.platform.config import get_settings
from notepatch.platform.gpu_lease import GpuLeaseService
from tests.conftest import FakeRedis


def test_gpu_lease_uses_token_and_emits_lifecycle_events():
    settings = get_settings()
    old_enabled = settings.gpu_lock_enabled
    settings.gpu_lock_enabled = True
    redis = FakeRedis()
    events = []
    try:
        with GpuLeaseService(client=redis).lease(
            owner="test-owner",
            event_callback=lambda event, data: events.append((event, data)),
        ):
            assert settings.gpu_lock_key in redis.values
            assert redis.values[settings.gpu_lock_key].startswith("test-owner:")
        assert settings.gpu_lock_key not in redis.values
        assert [event for event, _data in events] == [
            "gpu_lease_waiting",
            "gpu_lease_acquired",
            "gpu_lease_released",
        ]
    finally:
        settings.gpu_lock_enabled = old_enabled

from __future__ import annotations

from notepatch.platform.config import Settings, get_settings

_DEFAULTS = {
    "change-me-in-production-use-at-least-32-bytes",
    "change-me-tusd-webhook-secret",
    "notepatch-secret",
    "",
}


def validate_production_settings(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    if settings.environment.lower() in {"local", "development", "test"}:
        return
    invalid = []
    if settings.effective_secret_key in _DEFAULTS or len(settings.effective_secret_key) < 32:
        invalid.append("JWT_SECRET")
    if settings.tusd_webhook_secret in _DEFAULTS:
        invalid.append("TUSD_WEBHOOK_SECRET")
    if settings.storage_secret_key in _DEFAULTS:
        invalid.append("SEAWEEDFS_SECRET_KEY")
    if settings.backend_cors_origins.strip() == "*":
        invalid.append("BACKEND_CORS_ORIGINS")
    if invalid:
        raise RuntimeError(f"Unsafe production configuration: {', '.join(invalid)}")

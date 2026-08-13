from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import re

import httpx
import redis

from notepatch.platform.config import get_settings


logger = logging.getLogger(__name__)
MODEL_ID_PATTERN = re.compile(r"^[^\s\x00-\x1f\x7f]{1,255}$")


class AiModelCatalogUnavailableError(RuntimeError):
    pass


class AiModelNotFoundError(ValueError):
    pass


def normalize_ai_model_id(model_id: str) -> str:
    value = model_id.strip()
    if not MODEL_ID_PATTERN.fullmatch(value):
        raise ValueError("Invalid AI model id")
    return value if value.startswith("openai/") else f"openai/{value}"


def upstream_ai_model_id(model_id: str) -> str:
    normalized = normalize_ai_model_id(model_id)
    return normalized.removeprefix("openai/")


class AiModelCatalogService:
    fresh_cache_key = "notepatch:ai:models:openai:fresh"
    last_good_cache_key = "notepatch:ai:models:openai:last-good"

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        redis_client=None,
    ) -> None:
        self.settings = get_settings()
        self._client = client or httpx.Client(timeout=self.settings.ai_provider_timeout_seconds)
        self._redis = redis_client or redis.from_url(self.settings.redis_url, decode_responses=True)

    @property
    def default_model(self) -> str:
        return normalize_ai_model_id(self.settings.openclaw_agent_model)

    def get_catalog(self, *, force_refresh: bool = False) -> dict:
        if not force_refresh:
            cached = self._read_cache(self.fresh_cache_key)
            if cached is not None:
                return {**cached, "stale": False}

        try:
            catalog = self._fetch_catalog()
        except AiModelCatalogUnavailableError:
            cached = self._read_cache(self.last_good_cache_key)
            if cached is not None:
                return {**cached, "stale": True}
            raise

        self._write_cache(catalog)
        return {**catalog, "stale": False}

    def validate_model(self, model_id: str) -> str:
        try:
            normalized = normalize_ai_model_id(model_id)
        except ValueError as exc:
            raise AiModelNotFoundError("AI model is not available") from exc
        catalog = self.get_catalog()
        available = {item["id"] for item in catalog["items"]}
        if normalized not in available:
            raise AiModelNotFoundError("AI model is not available")
        return normalized

    def _fetch_catalog(self) -> dict:
        api_key = (self.settings.openai_api_key or "").strip()
        if not api_key:
            raise AiModelCatalogUnavailableError("AI provider credentials are not configured")
        base_url = (self.settings.openai_base_url or "https://api.openai.com/v1").rstrip("/")
        headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        if self.settings.openai_organization:
            headers["OpenAI-Organization"] = self.settings.openai_organization
        if self.settings.openai_project:
            headers["OpenAI-Project"] = self.settings.openai_project
        try:
            response = self._client.get(f"{base_url}/models", headers=headers)
        except httpx.HTTPError as exc:
            raise AiModelCatalogUnavailableError("AI provider model catalog is unavailable") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise AiModelCatalogUnavailableError(
                f"AI provider model catalog returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise AiModelCatalogUnavailableError("AI provider model catalog returned invalid JSON") from exc
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise AiModelCatalogUnavailableError("AI provider model catalog response is invalid")

        allowlist = {
            normalize_ai_model_id(item)
            for item in self.settings.ai_model_allowlist_set
        }
        items: list[dict] = []
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                continue
            try:
                model_id = normalize_ai_model_id(row["id"])
            except ValueError:
                continue
            if model_id in seen or (allowlist and model_id not in allowlist):
                continue
            seen.add(model_id)
            created = row.get("created")
            items.append(
                {
                    "id": model_id,
                    "upstream_id": upstream_ai_model_id(model_id),
                    "owned_by": row.get("owned_by") if isinstance(row.get("owned_by"), str) else None,
                    "created": created if isinstance(created, int) and not isinstance(created, bool) else None,
                }
            )
        items.sort(key=lambda item: item["id"].lower())
        return {
            "provider": "openai",
            "default_model": self.default_model,
            "items": items,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

    def _read_cache(self, key: str) -> dict | None:
        try:
            raw = self._redis.get(key)
        except Exception as exc:
            logger.warning("Could not read AI model catalog cache: %s", exc)
            return None
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def _write_cache(self, catalog: dict) -> None:
        payload = json.dumps(catalog, ensure_ascii=False, separators=(",", ":"))
        try:
            if self.settings.ai_model_catalog_ttl_seconds > 0:
                self._redis.setex(
                    self.fresh_cache_key,
                    self.settings.ai_model_catalog_ttl_seconds,
                    payload,
                )
            self._redis.set(self.last_good_cache_key, payload)
        except Exception as exc:
            logger.warning("Could not write AI model catalog cache: %s", exc)

import json

import httpx

from notepatch.entrypoints.api import app
from notepatch.entrypoints.deps import get_ai_model_catalog_service
from notepatch.modules.ai.services.model_catalog import (
    AiModelCatalogService,
    AiModelNotFoundError,
)
from notepatch.modules.identity.models.user import User
from notepatch.platform.config import get_settings
from tests.conftest import auth_headers, first_workspace_id, register_user


class CatalogStub:
    @property
    def default_model(self):
        return get_settings().openclaw_agent_model

    def get_catalog(self):
        return {
            "provider": "openai",
            "default_model": self.default_model,
            "items": [
                {
                    "id": "openai/fast-chat",
                    "upstream_id": "fast-chat",
                    "owned_by": "test-provider",
                    "created": 123,
                },
                {
                    "id": self.default_model,
                    "upstream_id": self.default_model.removeprefix("openai/"),
                    "owned_by": "test-provider",
                    "created": None,
                },
            ],
            "fetched_at": "2026-07-23T00:00:00+00:00",
            "stale": False,
        }

    def validate_model(self, model_id: str) -> str:
        normalized = model_id if model_id.startswith("openai/") else f"openai/{model_id}"
        if normalized not in {item["id"] for item in self.get_catalog()["items"]}:
            raise AiModelNotFoundError("AI model is not available")
        return normalized


class MemoryRedis:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value

    def setex(self, key, ttl, value):
        self.values[key] = value


def test_model_catalog_and_global_selection_are_workspace_scoped(client):
    alice = register_user(client, "models-alice@example.com")
    bob = register_user(client, "models-bob@example.com")
    alice_token = alice["access_token"]
    bob_token = bob["access_token"]
    alice_workspace = first_workspace_id(client, alice_token)
    bob_workspace = first_workspace_id(client, bob_token)
    expected_default = get_settings().openclaw_agent_model
    app.dependency_overrides[get_ai_model_catalog_service] = CatalogStub
    try:
        listed = client.get(
            f"/api/v1/workspaces/{alice_workspace}/ai/models",
            headers=auth_headers(alice_token),
        )
        assert listed.status_code == 200, listed.text
        assert listed.json()["selected_model"] == expected_default
        assert [item["id"] for item in listed.json()["items"]] == [
            "openai/fast-chat",
            expected_default,
        ]

        selected = client.put(
            f"/api/v1/workspaces/{alice_workspace}/ai/model",
            headers=auth_headers(alice_token),
            json={"model_id": "fast-chat"},
        )
        assert selected.status_code == 200, selected.text
        assert selected.json()["selected_model"] == "openai/fast-chat"
        assert client.get("/api/v1/auth/me", headers=auth_headers(alice_token)).json()[
            "preferred_ai_model"
        ] == "openai/fast-chat"

        forbidden = client.put(
            f"/api/v1/workspaces/{bob_workspace}/ai/model",
            headers=auth_headers(alice_token),
            json={"model_id": "fast-chat"},
        )
        assert forbidden.status_code == 403

        invalid = client.put(
            f"/api/v1/workspaces/{alice_workspace}/ai/model",
            headers=auth_headers(alice_token),
            json={"model_id": "missing-model"},
        )
        assert invalid.status_code == 422

        reset = client.put(
            f"/api/v1/workspaces/{alice_workspace}/ai/model",
            headers=auth_headers(alice_token),
            json={"model_id": None},
        )
        assert reset.status_code == 200
        assert reset.json()["preferred_model"] is None
        assert reset.json()["selected_model"] == expected_default
    finally:
        app.dependency_overrides.pop(get_ai_model_catalog_service, None)


def test_provider_catalog_normalizes_models_and_uses_last_good_cache(monkeypatch):
    settings = get_settings()
    old_values = {
        "openai_api_key": settings.openai_api_key,
        "openai_base_url": settings.openai_base_url,
        "openclaw_agent_model": settings.openclaw_agent_model,
        "ai_model_allowlist": settings.ai_model_allowlist,
    }
    settings.openai_api_key = "test-secret"
    settings.openai_base_url = "http://provider.test/v1"
    settings.openclaw_agent_model = "openai/default-chat"
    settings.ai_model_allowlist = ""
    cache = MemoryRedis()
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "z-model", "owned_by": "provider", "created": 10},
                    {"id": "openai/a-model", "owned_by": "provider"},
                ]
            },
        )

    try:
        service = AiModelCatalogService(
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            redis_client=cache,
        )
        catalog = service.get_catalog(force_refresh=True)
        assert captured["url"] == "http://provider.test/v1/models"
        assert captured["authorization"] == "Bearer test-secret"
        assert [item["id"] for item in catalog["items"]] == ["openai/a-model", "openai/z-model"]
        assert "test-secret" not in json.dumps(catalog)

        failed = AiModelCatalogService(
            client=httpx.Client(
                transport=httpx.MockTransport(lambda request: httpx.Response(503, text="secret body"))
            ),
            redis_client=cache,
        )
        stale = failed.get_catalog(force_refresh=True)
        assert stale["stale"] is True
        assert [item["id"] for item in stale["items"]] == ["openai/a-model", "openai/z-model"]
    finally:
        for key, value in old_values.items():
            setattr(settings, key, value)


def test_chat_task_snapshots_user_model(client, db_sessionmaker):
    registered = register_user(client, "model-snapshot@example.com")
    token = registered["access_token"]
    workspace_id = first_workspace_id(client, token)
    user_id = registered["user"]["id"]
    with db_sessionmaker() as db:
        user = db.get(User, user_id)
        user.preferred_ai_model = "openai/selected-chat"
        db.commit()

    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/ai/chat",
        headers=auth_headers(token),
        json={"prompt": "使用选中的模型", "input": {}, "options": {}},
    )
    assert response.status_code == 201, response.text
    task_id = response.json()["id"]

    from notepatch.modules.ai.services.model_selection import AiModelSelectionService
    from notepatch.modules.tasks.models.task import Task

    with db_sessionmaker() as db:
        task = db.get(Task, task_id)
        selected, created = AiModelSelectionService(db).resolve_for_task(task)
        assert selected == "openai/selected-chat"
        assert created is True
        db.commit()

        user = db.get(User, user_id)
        user.preferred_ai_model = "openai/new-chat"
        db.commit()
        selected_again, created_again = AiModelSelectionService(db).resolve_for_task(task)
        assert selected_again == "openai/selected-chat"
        assert created_again is False

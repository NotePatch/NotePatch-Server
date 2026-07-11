from notepatch.entrypoints.deps import get_presence_service
from notepatch.entrypoints.api import app
from notepatch.modules.identity.services.presence import PresenceService
from tests.conftest import FakeRedis, auth_headers, register_user


def test_presence_heartbeat_requires_login(client):
    response = client.post("/api/v1/presence/heartbeat", json={})
    assert response.status_code == 401


def test_presence_heartbeat_and_offline_support_multiple_clients(client):
    redis = FakeRedis()
    presence = PresenceService(redis_client=redis)
    app.dependency_overrides[get_presence_service] = lambda: presence
    user = register_user(client, "presence@example.com")

    first = client.post(
        "/api/v1/presence/heartbeat",
        headers=auth_headers(user["access_token"]),
        json={},
    )
    assert first.status_code == 200, first.text
    first_client_id = first.json()["client_id"]
    assert first.json()["heartbeat_interval_seconds"] == 30

    second = client.post(
        "/api/v1/presence/heartbeat",
        headers=auth_headers(user["access_token"]),
        json={"client_id": "browser-tab-2"},
    )
    assert second.status_code == 200, second.text
    assert second.json()["client_id"] == "browser-tab-2"
    assert presence.active_client_ids(user["user"]["id"]) == {first_client_id, "browser-tab-2"}

    offline = client.post(
        "/api/v1/presence/offline",
        headers=auth_headers(user["access_token"]),
        json={"client_id": first_client_id},
    )
    assert offline.status_code == 200, offline.text
    assert presence.active_client_ids(user["user"]["id"]) == {"browser-tab-2"}


def test_logout_with_client_id_clears_only_that_presence_session(client):
    redis = FakeRedis()
    presence = PresenceService(redis_client=redis)
    app.dependency_overrides[get_presence_service] = lambda: presence
    user = register_user(client, "presence-logout@example.com")
    user_id = user["user"]["id"]
    presence.heartbeat(user_id, "tab-a")
    presence.heartbeat(user_id, "tab-b")

    response = client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": user["refresh_token"], "client_id": "tab-a"},
    )
    assert response.status_code == 200, response.text
    assert presence.active_client_ids(user_id) == {"tab-b"}

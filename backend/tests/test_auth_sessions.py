from datetime import timedelta

from sqlalchemy import select

from notepatch.modules.identity.models.user import RefreshToken
from notepatch.platform.database import utcnow
from notepatch.platform.security import hash_token
from tests.conftest import auth_headers, register_user


def _refresh(client, token: str):
    return client.post("/api/v1/auth/refresh", json={"refresh_token": token})


def test_refresh_rotation_accepts_concurrent_reuse_and_logout_revokes_only_family(client):
    registered = register_user(client, "refresh-family@example.com")
    original = registered["refresh_token"]

    first = _refresh(client, original)
    second = _refresh(client, original)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert client.get("/api/v1/auth/me", headers=auth_headers(first.json()["access_token"])).status_code == 200
    assert client.get("/api/v1/auth/me", headers=auth_headers(second.json()["access_token"])).status_code == 200

    independent = client.post(
        "/api/v1/auth/login",
        json={"email": "refresh-family@example.com", "password": "password123"},
    )
    assert independent.status_code == 200

    logout = client.post("/api/v1/auth/logout", json={"refresh_token": first.json()["refresh_token"]})
    assert logout.status_code == 200
    assert _refresh(client, second.json()["refresh_token"]).status_code == 401
    assert _refresh(client, independent.json()["refresh_token"]).status_code == 200


def test_rotated_refresh_token_is_rejected_after_grace_period(client, db_sessionmaker):
    registered = register_user(client, "refresh-grace@example.com")
    original = registered["refresh_token"]
    assert _refresh(client, original).status_code == 200

    with db_sessionmaker() as db:
        token = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_token(original)))
        token.revoked_at = utcnow() - timedelta(seconds=30)
        db.commit()

    assert _refresh(client, original).status_code == 401

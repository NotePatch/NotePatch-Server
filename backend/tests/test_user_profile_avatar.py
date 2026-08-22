from io import BytesIO

from PIL import Image

from notepatch.modules.identity.models.user import IdentityAuditLog, User
from notepatch.modules.tasks.models.task import Task
from notepatch.modules.tasks.services.executor import process_task
from tests.conftest import FakeRedis, auth_headers, register_user


def _png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (32, 32), (30, 120, 210)).save(output, format="PNG")
    return output.getvalue()


def test_profile_update_uses_etag_and_idempotency(client, db_sessionmaker):
    registered = register_user(client, "profile-update@example.com")
    headers = auth_headers(registered["access_token"])
    loaded = client.get("/api/v1/user/profile", headers=headers)
    assert loaded.status_code == 200
    assert loaded.headers["etag"] == '"profile-1"'

    mutation_headers = {
        **headers,
        "If-Match": loaded.headers["etag"],
        "Idempotency-Key": "profile-update-0001",
    }
    updated = client.put(
        "/api/v1/user/profile",
        headers=mutation_headers,
        json={"name": "  张   三  "},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["name"] == "张 三"
    assert updated.json()["data"]["profile_version"] == 2
    assert updated.headers["idempotent-replayed"] == "false"

    replay = client.put(
        "/api/v1/user/profile",
        headers=mutation_headers,
        json={"name": "  张   三  "},
    )
    assert replay.status_code == 200
    assert replay.json() == updated.json()
    assert replay.headers["idempotent-replayed"] == "true"

    stale = client.put(
        "/api/v1/user/profile",
        headers={**headers, "If-Match": '"profile-1"', "Idempotency-Key": "profile-update-0002"},
        json={"name": "New name"},
    )
    assert stale.status_code == 412
    assert stale.json()["code"] == "profile_version_mismatch"

    conflict = client.put(
        "/api/v1/user/profile",
        headers={**headers, "If-Match": '"profile-2"', "Idempotency-Key": "profile-update-0001"},
        json={"name": "Different payload"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "idempotency_conflict"

    with db_sessionmaker() as db:
        logs = db.query(IdentityAuditLog).filter(IdentityAuditLog.action == "profile.update").all()
        assert len(logs) == 1
        assert "password" not in str(logs[0].after_data).lower()


def test_email_change_requires_password_and_invalidates_existing_tokens(client):
    registered = register_user(client, "email-before@example.com")
    access_token = registered["access_token"]
    profile = client.get("/api/v1/user/profile", headers=auth_headers(access_token))
    base_headers = {
        **auth_headers(access_token),
        "If-Match": profile.headers["etag"],
        "Idempotency-Key": "profile-email-0001",
    }
    denied = client.put(
        "/api/v1/user/profile",
        headers=base_headers,
        json={"email": "email-after@example.com"},
    )
    assert denied.status_code == 403
    assert denied.json()["code"] == "current_password_invalid"

    changed = client.put(
        "/api/v1/user/profile",
        headers=base_headers,
        json={"email": "email-after@example.com", "current_password": "password123"},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["data"]["reauthentication_required"] is True
    assert client.get("/api/v1/auth/me", headers=auth_headers(access_token)).status_code == 401
    assert client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": registered["refresh_token"]},
    ).status_code == 401
    login = client.post(
        "/api/v1/auth/login",
        json={"email": "email-after@example.com", "password": "password123"},
    )
    assert login.status_code == 200


def test_profile_cannot_claim_reserved_admin_email(client, monkeypatch):
    registered = register_user(client, "ordinary-profile@example.com")
    from notepatch.platform.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "admin_emails", "reserved-admin@example.com")
    response = client.put(
        "/api/v1/user/profile",
        headers={
            **auth_headers(registered["access_token"]),
            "If-Match": '"profile-1"',
            "Idempotency-Key": "reserved-email-0001",
        },
        json={"email": "reserved-admin@example.com", "current_password": "password123"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "reserved_email"


def test_profile_validation_errors_use_the_standard_envelope(client):
    registered = register_user(client, "profile-validation@example.com")
    response = client.put(
        "/api/v1/user/profile",
        headers={
            **auth_headers(registered["access_token"]),
            "If-Match": '"profile-1"',
            "Idempotency-Key": "profile-invalid-0001",
        },
        json={"email": "not-an-email"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert response.json()["data"]["errors"]


def test_avatar_upload_download_and_delete(client, fake_storage, db_sessionmaker):
    registered = register_user(client, "avatar-owner@example.com")
    headers = auth_headers(registered["access_token"])
    uploaded = client.post(
        "/api/v1/user/avatar/upload",
        headers={**headers, "If-Match": '"profile-1"', "Idempotency-Key": "avatar-upload-0001"},
        files={"file": ("unsafe-name.png", _png_bytes(), "image/png")},
    )
    assert uploaded.status_code == 200, uploaded.text
    data = uploaded.json()["data"]
    assert data["mime_type"] == "image/png"
    assert data["profile_version"] == 2
    assert data["avatar_url"].startswith("/api/v1/user/avatar/content?v=")

    with db_sessionmaker() as db:
        user = db.query(User).filter(User.email == "avatar-owner@example.com").one()
        assert user.avatar_object_key.startswith(f"users/{user.id}/profile/avatar/")
        assert "unsafe-name" not in user.avatar_object_key
        assert (fake_storage.bucket, user.avatar_object_key) in fake_storage.objects

    download = client.get("/api/v1/user/avatar/download-url", headers=headers)
    assert download.status_code == 200
    assert download.json()["data"]["download_url"].startswith("mock://download/")

    deleted = client.delete(
        "/api/v1/user/avatar",
        headers={**headers, "If-Match": '"profile-2"', "Idempotency-Key": "avatar-delete-0001"},
    )
    assert deleted.status_code == 200
    assert deleted.json()["data"]["avatar_url"] is None
    assert not fake_storage.objects


def test_avatar_rejects_declared_image_with_invalid_content(client):
    registered = register_user(client, "invalid-avatar@example.com")
    response = client.post(
        "/api/v1/user/avatar/upload",
        headers={
            **auth_headers(registered["access_token"]),
            "If-Match": '"profile-1"',
            "Idempotency-Key": "avatar-invalid-0001",
        },
        files={"file": ("avatar.png", b"not an image", "image/png")},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "avatar_invalid"


def test_avatar_replacement_schedules_cleanup_when_old_object_delete_fails(
    client,
    fake_storage,
    db_sessionmaker,
    monkeypatch,
):
    fake_redis = FakeRedis()
    monkeypatch.setattr("notepatch.modules.tasks.services.task.redis.from_url", lambda *args, **kwargs: fake_redis)
    registered = register_user(client, "avatar-cleanup@example.com")
    headers = auth_headers(registered["access_token"])
    first = client.post(
        "/api/v1/user/avatar/upload",
        headers={**headers, "If-Match": '"profile-1"', "Idempotency-Key": "avatar-cleanup-0001"},
        files={"file": ("first.png", _png_bytes(), "image/png")},
    )
    assert first.status_code == 200

    original_delete = fake_storage.delete_object

    def fail_delete(_bucket: str, _object_key: str) -> None:
        raise RuntimeError("storage temporarily unavailable")

    monkeypatch.setattr(fake_storage, "delete_object", fail_delete)
    second = client.post(
        "/api/v1/user/avatar/upload",
        headers={**headers, "If-Match": '"profile-2"', "Idempotency-Key": "avatar-cleanup-0002"},
        files={"file": ("second.png", _png_bytes(), "image/png")},
    )
    assert second.status_code == 200

    with db_sessionmaker() as db:
        cleanup = db.query(Task).filter(Task.task_type == "purge_avatar_object").one()
        old_key = cleanup.payload["object_key"]
        assert cleanup.status == "queued"
        assert (fake_storage.bucket, old_key) in fake_storage.objects

    monkeypatch.setattr(fake_storage, "delete_object", original_delete)
    with db_sessionmaker() as db:
        completed = process_task(db, cleanup.id, storage=fake_storage)
        assert completed is not None
        assert completed.status == "succeeded"
    assert (fake_storage.bucket, old_key) not in fake_storage.objects

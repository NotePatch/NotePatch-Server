from notepatch.platform.config import get_settings
from tests.conftest import FakeRedis, auth_headers, first_workspace_id, register_user
from tests.test_document_permissions import create_upload_session


def _set_admins(value: str) -> None:
    get_settings().admin_emails = value


def _create_artifact(client, fake_storage, token: str, workspace_id: str, document_id: str):
    object_key = f"workspaces/{workspace_id}/documents/{document_id}/artifacts/admin-test/ocr.md"
    fake_storage.objects[(fake_storage.bucket, object_key)] = {
        "file_size": 42,
        "mime_type": "text/markdown",
        "metadata": {},
        "body": b"admin artifact",
    }
    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/{document_id}/artifacts",
        headers=auth_headers(token),
        json={
            "artifact_type": "ocr_markdown",
            "object_key": object_key,
            "mime_type": "text/markdown",
            "file_size": 42,
            "metadata": {"source": "test"},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_admin_api_requires_login_and_admin_email(client):
    user = register_user(client, "plain-admin-denied@example.com")

    missing_token = client.get("/api/v1/admin/me")
    disabled = client.get("/api/v1/admin/me", headers=auth_headers(user["access_token"]))
    _set_admins("someone-else@example.com")
    forbidden = client.get("/api/v1/admin/me", headers=auth_headers(user["access_token"]))
    _set_admins("plain-admin-denied@example.com")
    allowed = client.get("/api/v1/admin/me", headers=auth_headers(user["access_token"]))

    assert missing_token.status_code == 401
    assert disabled.status_code == 403
    assert forbidden.status_code == 403
    assert allowed.status_code == 200
    assert allowed.json()["admin"] is True


def test_admin_can_query_users_documents_tasks_and_download_urls(client, fake_storage):
    admin = register_user(client, "ops-admin@example.com")
    student = register_user(client, "student-for-admin@example.com")
    _set_admins("ops-admin@example.com")

    admin_workspace_id = first_workspace_id(client, admin["access_token"])
    student_workspace_id = first_workspace_id(client, student["access_token"])
    upload = create_upload_session(client, student["access_token"], student_workspace_id, filename="student.pdf")
    document_id = upload["document"]["id"]
    fake_storage.objects[(upload["bucket"], upload["object_key"])] = {
        "file_size": 8,
        "mime_type": "application/pdf",
        "metadata": {},
        "body": b"pdf-data",
    }
    completed = client.post(
        f"/api/v1/workspaces/{student_workspace_id}/documents/complete-upload",
        headers=auth_headers(student["access_token"]),
        json={"upload_session_id": upload["upload_session"]["id"]},
    )
    assert completed.status_code == 200, completed.text
    artifact = _create_artifact(client, fake_storage, student["access_token"], student_workspace_id, document_id)
    task_response = client.post(
        f"/api/v1/workspaces/{student_workspace_id}/documents/{document_id}/process",
        headers=auth_headers(student["access_token"]),
        json={"options": {}},
    )
    assert task_response.status_code == 201, task_response.text
    task_id = task_response.json()["id"]

    users = client.get("/api/v1/admin/users?search=student-for-admin", headers=auth_headers(admin["access_token"]))
    documents = client.get(
        f"/api/v1/admin/documents?workspace_id={student_workspace_id}",
        headers=auth_headers(admin["access_token"]),
    )
    document_detail = client.get(f"/api/v1/admin/documents/{document_id}", headers=auth_headers(admin["access_token"]))
    artifacts = client.get(
        f"/api/v1/admin/documents/{document_id}/artifacts",
        headers=auth_headers(admin["access_token"]),
    )
    document_download = client.get(
        f"/api/v1/admin/documents/{document_id}/download-url",
        headers=auth_headers(admin["access_token"]),
    )
    artifact_download = client.get(
        f"/api/v1/admin/artifacts/{artifact['id']}/download-url?expires_seconds=600",
        headers=auth_headers(admin["access_token"]),
    )
    tasks = client.get("/api/v1/admin/tasks?task_type=document_processing_pipeline", headers=auth_headers(admin["access_token"]))
    task_detail = client.get(f"/api/v1/admin/tasks/{task_id}", headers=auth_headers(admin["access_token"]))
    task_events = client.get(f"/api/v1/admin/tasks/{task_id}/events", headers=auth_headers(admin["access_token"]))
    overview = client.get("/api/v1/admin/overview", headers=auth_headers(admin["access_token"]))

    assert admin_workspace_id != student_workspace_id
    assert users.status_code == 200
    assert users.json()["total"] == 1
    assert users.json()["items"][0]["email"] == "student-for-admin@example.com"
    assert documents.status_code == 200
    assert documents.json()["items"][0]["id"] == document_id
    assert document_detail.status_code == 200
    assert document_detail.json()["document"]["id"] == document_id
    assert artifacts.status_code == 200
    assert any(item["id"] == artifact["id"] for item in artifacts.json())
    assert document_download.status_code == 200
    assert document_download.json()["download_url"].startswith("mock://download/")
    assert artifact_download.status_code == 200
    assert artifact_download.json()["filename"] == "ocr.md"
    assert artifact_download.json()["expires_in"] == 600
    assert tasks.status_code == 200
    assert any(item["id"] == task_id for item in tasks.json()["items"])
    assert task_detail.status_code == 200
    assert task_detail.json()["payload"]["document_id"] == document_id
    assert task_events.status_code == 200
    assert task_events.json()[0]["event_type"] == "queued"
    assert [item["sequence_no"] for item in task_events.json()] == list(
        range(1, len(task_events.json()) + 1)
    )
    assert overview.status_code == 200
    assert overview.json()["users_count"] >= 2


def test_admin_queues_and_services_degrade_without_redis(client, monkeypatch):
    admin = register_user(client, "ops-admin-health@example.com")
    _set_admins("ops-admin-health@example.com")
    monkeypatch.setattr(
        "notepatch.modules.admin.services.health.redis.from_url",
        lambda *args, **kwargs: FakeRedis(fail_reads=True),
    )

    queues = client.get("/api/v1/admin/queues", headers=auth_headers(admin["access_token"]))
    services = client.get("/api/v1/admin/services", headers=auth_headers(admin["access_token"]))

    assert queues.status_code == 200
    assert {item["name"] for item in queues.json()["queues"]} == {"default", "ocr", "chat", "ai"}
    assert {item["status"] for item in queues.json()["queues"]} == {"degraded"}
    assert services.status_code == 200
    service_statuses = {item["name"]: item["status"] for item in services.json()["services"]}
    assert service_statuses["database"] == "ok"
    assert service_statuses["redis"] == "degraded"

from pathlib import Path

from notepatch.platform.config import get_settings
from notepatch.modules.documents.models.document import DocumentArtifact
from notepatch.modules.tasks.models.task import Task
from notepatch.modules.tasks.services.task import TaskService
from tests.conftest import auth_headers, first_workspace_id, register_user


def test_tusd_finished_webhook_is_idempotent(client, db_sessionmaker, tmp_path: Path):
    user = register_user(client, "webhook@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    upload = client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/upload-session",
        headers=auth_headers(user["access_token"]),
        json={
            "filename": "homework.jpg",
            "mime_type": "image/jpeg",
            "file_size": 5,
            "document_kind": "homework",
        },
    )
    assert upload.status_code == 201, upload.text
    data = upload.json()
    document_id = data["document"]["id"]
    tus_upload_id = "upload-abc"
    (tmp_path / tus_upload_id).write_bytes(b"hello")

    payload = {
        "Type": "post-finish",
        "Event": {
            "Upload": {
                "ID": tus_upload_id,
                "Size": 5,
                "Offset": 5,
                "MetaData": data["tus_metadata"],
                "Storage": {"Type": "filestore", "Path": f"/data/{tus_upload_id}"},
            }
        },
    }

    secret = get_settings().tusd_webhook_secret
    first = client.post(f"/api/v1/webhooks/tusd?secret={secret}", json=payload)
    second = client.post(f"/api/v1/webhooks/tusd?secret={secret}", json=payload)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text

    document = client.get(
        f"/api/v1/workspaces/{workspace_id}/documents/{document_id}",
        headers=auth_headers(user["access_token"]),
    )
    assert document.status_code == 200
    assert document.json()["status"] == "uploaded"
    assert document.json()["scan_status"] == "skipped"
    assert document.json()["upload_id"] == tus_upload_id

    with db_sessionmaker() as db:
        artifacts = (
            db.query(DocumentArtifact)
            .filter_by(workspace_id=workspace_id, document_id=document_id, artifact_type="original")
            .all()
        )
        assert len(artifacts) == 1
        assert (
            db.query(Task)
            .filter_by(
                workspace_id=workspace_id,
                task_type="scan_document",
                resource_id=document_id,
            )
            .count()
            == 0
        )


def test_tusd_finish_and_client_completion_create_one_scan_task(
    client, db_sessionmaker, tmp_path: Path, monkeypatch
):
    settings = get_settings()
    previous_clamav = settings.clamav_enabled
    settings.clamav_enabled = True
    monkeypatch.setattr(TaskService, "enqueue_task", lambda self, task_id, queue_name=None: True)
    try:
        user = register_user(client, "webhook-scan-idempotent@example.com")
        workspace_id = first_workspace_id(client, user["access_token"])
        upload = client.post(
            f"/api/v1/workspaces/{workspace_id}/documents/upload-session",
            headers=auth_headers(user["access_token"]),
            json={
                "filename": "homework.pdf",
                "mime_type": "application/pdf",
                "file_size": 5,
                "document_kind": "homework",
            },
        )
        assert upload.status_code == 201, upload.text
        data = upload.json()
        tus_upload_id = "upload-scan-idempotent"
        (tmp_path / tus_upload_id).write_bytes(b"hello")
        hook = {
            "Type": "post-finish",
            "Event": {
                "Upload": {
                    "ID": tus_upload_id,
                    "Size": 5,
                    "Offset": 5,
                    "MetaData": data["tus_metadata"],
                    "Storage": {"Type": "filestore", "Path": f"/data/{tus_upload_id}"},
                }
            },
        }
        secret = settings.tusd_webhook_secret
        assert client.post(f"/api/v1/webhooks/tusd?secret={secret}", json=hook).status_code == 200
        complete = client.post(
            f"/api/v1/workspaces/{workspace_id}/documents/complete-upload",
            headers=auth_headers(user["access_token"]),
            json={
                "upload_session_id": data["upload_session"]["id"],
                "tus_upload_id": tus_upload_id,
                "file_size": 5,
                "mime_type": "application/pdf",
            },
        )
        assert complete.status_code == 200, complete.text
        assert complete.json()["status"] == "scanning"
        with db_sessionmaker() as db:
            scan_tasks = (
                db.query(Task)
                .filter_by(
                    workspace_id=workspace_id,
                    task_type="scan_document",
                    resource_type="document",
                    resource_id=data["document"]["id"],
                )
                .all()
            )
            assert len(scan_tasks) == 1
    finally:
        settings.clamav_enabled = previous_clamav

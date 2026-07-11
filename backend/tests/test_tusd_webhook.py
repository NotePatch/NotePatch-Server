from pathlib import Path

from notepatch.platform.config import get_settings
from notepatch.modules.documents.models.document import DocumentArtifact
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
    assert document.json()["upload_id"] == tus_upload_id

    with db_sessionmaker() as db:
        artifacts = (
            db.query(DocumentArtifact)
            .filter_by(workspace_id=workspace_id, document_id=document_id, artifact_type="original")
            .all()
        )
        assert len(artifacts) == 1

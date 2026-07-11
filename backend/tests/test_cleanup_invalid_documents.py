from sqlalchemy import select

from notepatch.modules.documents.models.document import Document, DocumentArtifact
from notepatch.modules.documents.models.upload import UploadSession
from scripts.cleanup_invalid_documents import delete_invalid_documents, find_invalid_documents
from tests.conftest import auth_headers, first_workspace_id, register_user


def _create_upload_session(client, token: str, workspace_id: str, filename: str) -> dict:
    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/upload-session",
        headers=auth_headers(token),
        json={
            "filename": filename,
            "mime_type": "application/pdf",
            "file_size": 12,
            "document_kind": "homework",
            "title": filename,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_cleanup_invalid_documents_dry_run_does_not_delete(client, db_sessionmaker, fake_storage):
    user = register_user(client, "cleanup-dry-run@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    upload = _create_upload_session(client, user["access_token"], workspace_id, "bad.pdf")

    with db_sessionmaker() as db:
        candidates, errors = find_invalid_documents(
            db=db,
            storage=fake_storage,
            workspace_id=workspace_id,
            older_than_minutes=0,
        )
        assert errors == []
        assert [candidate.document_id for candidate in candidates] == [upload["document"]["id"]]
        assert db.get(Document, upload["document"]["id"]) is not None
        assert db.scalar(select(UploadSession).where(UploadSession.document_id == upload["document"]["id"])) is not None


def test_cleanup_invalid_documents_apply_deletes_document_artifacts_and_upload_sessions(
    client,
    db_sessionmaker,
    fake_storage,
):
    user = register_user(client, "cleanup-apply@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    upload = _create_upload_session(client, user["access_token"], workspace_id, "bad.pdf")

    with db_sessionmaker() as db:
        db.add(
            DocumentArtifact(
                workspace_id=workspace_id,
                document_id=upload["document"]["id"],
                artifact_type="ocr_json",
                bucket=upload["bucket"],
                object_key=f"workspaces/{workspace_id}/documents/{upload['document']['id']}/artifacts/a/ocr_json.json",
                mime_type="application/json",
                file_size=2,
                metadata_={},
            )
        )
        db.commit()
        candidates, _errors = find_invalid_documents(
            db=db,
            storage=fake_storage,
            workspace_id=workspace_id,
            older_than_minutes=0,
        )
        deleted = delete_invalid_documents(db=db, candidates=candidates)

        assert deleted == {"documents": 1, "artifacts": 1, "upload_sessions": 1}
        assert db.get(Document, upload["document"]["id"]) is None
        assert db.scalar(select(UploadSession).where(UploadSession.document_id == upload["document"]["id"])) is None
        assert db.scalar(select(DocumentArtifact).where(DocumentArtifact.document_id == upload["document"]["id"])) is None


def test_cleanup_invalid_documents_keeps_valid_existing_object_and_other_workspaces(
    client,
    db_sessionmaker,
    fake_storage,
):
    alice = register_user(client, "cleanup-valid-alice@example.com")
    bob = register_user(client, "cleanup-valid-bob@example.com")
    alice_workspace_id = first_workspace_id(client, alice["access_token"])
    bob_workspace_id = first_workspace_id(client, bob["access_token"])
    alice_upload = _create_upload_session(client, alice["access_token"], alice_workspace_id, "valid.pdf")
    bob_upload = _create_upload_session(client, bob["access_token"], bob_workspace_id, "bad.pdf")
    fake_storage.objects[(alice_upload["bucket"], alice_upload["object_key"])] = {
        "file_size": 12,
        "mime_type": "application/pdf",
        "metadata": {},
        "body": b"valid",
    }

    with db_sessionmaker() as db:
        db.get(Document, alice_upload["document"]["id"]).status = "uploaded"
        candidates, _errors = find_invalid_documents(
            db=db,
            storage=fake_storage,
            workspace_id=alice_workspace_id,
            older_than_minutes=0,
        )
        assert candidates == []
        delete_invalid_documents(db=db, candidates=candidates)
        assert db.get(Document, alice_upload["document"]["id"]) is not None
        assert db.get(Document, bob_upload["document"]["id"]) is not None

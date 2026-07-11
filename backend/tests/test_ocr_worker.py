import json

from notepatch.platform.config import get_settings
from notepatch.modules.documents.models.document import DocumentArtifact
from notepatch.modules.tasks.models.task import TaskEvent
from notepatch.modules.documents.ocr import OcrPipeline
from notepatch.modules.tasks.services.executor import process_task
from tests.conftest import FakeRedis, auth_headers, first_workspace_id, register_user
from tests.test_doctr_worker import FailingDocTrClient, PNG_BYTES, minimal_pdf_bytes


def _create_document(
    client,
    token: str,
    workspace_id: str,
    *,
    filename: str,
    mime_type: str,
    file_size: int | None = None,
) -> dict:
    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/upload-session",
        headers=auth_headers(token),
        json={
            "filename": filename,
            "mime_type": mime_type,
            "file_size": file_size if file_size is not None else len(PNG_BYTES),
            "document_kind": "homework",
            "title": filename,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _process(client, token: str, workspace_id: str, document_id: str, *, force: bool = False) -> str:
    completed = client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/complete-upload",
        headers=auth_headers(token),
        json={"document_id": document_id},
    )
    assert completed.status_code == 200, completed.text
    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/{document_id}/process",
        headers=auth_headers(token),
        json={"options": {"force_reprocess": force}},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _ocr_artifacts(db, document_id: str):
    return (
        db.query(DocumentArtifact)
        .filter(
            DocumentArtifact.document_id == document_id,
            DocumentArtifact.artifact_type.in_(("ocr_json", "ocr_markdown", "ocr_text")),
        )
        .all()
    )


def test_ocr_artifacts_are_idempotent_and_force_reprocess_creates_new_set(client, db_sessionmaker, fake_storage):
    user = register_user(client, "ocr-idempotent@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    upload = _create_document(client, user["access_token"], workspace_id, filename="paper.png", mime_type="image/png")
    fake_storage.objects[(upload["bucket"], upload["object_key"])] = {
        "file_size": len(PNG_BYTES),
        "mime_type": "image/png",
        "metadata": {},
        "body": PNG_BYTES,
    }

    with db_sessionmaker() as db:
        first_task_id = _process(client, user["access_token"], workspace_id, upload["document"]["id"])
        first = process_task(db, first_task_id, storage=fake_storage, doctr_client=FailingDocTrClient())
        second_task_id = _process(client, user["access_token"], workspace_id, upload["document"]["id"])
        second = process_task(db, second_task_id, storage=fake_storage, doctr_client=FailingDocTrClient())
        third_task_id = _process(client, user["access_token"], workspace_id, upload["document"]["id"], force=True)
        third = process_task(db, third_task_id, storage=fake_storage, doctr_client=FailingDocTrClient())
        assert first.status == "succeeded"
        assert second.status == "succeeded"
        assert third.status == "succeeded"
        artifacts = _ocr_artifacts(db, upload["document"]["id"])
        assert len(artifacts) == 6
        run_ids = {(artifact.metadata_ or {}).get("ocr_run_id") for artifact in artifacts}
        assert len(run_ids) == 2
        second_events = {event.event_type for event in db.query(TaskEvent).filter_by(task_id=second_task_id).all()}
        assert "ocr_reused" in second_events


def test_paddleocr_unavailable_schedules_retry_without_fake_output(
    client, db_sessionmaker, fake_storage, monkeypatch
):
    fake_redis = FakeRedis()
    monkeypatch.setattr("notepatch.modules.tasks.services.task.redis.from_url", lambda *args, **kwargs: fake_redis)
    try:
        user = register_user(client, "ocr-paddle-fallback@example.com")
        workspace_id = first_workspace_id(client, user["access_token"])
        upload = _create_document(client, user["access_token"], workspace_id, filename="paper.png", mime_type="image/png")
        fake_storage.objects[(upload["bucket"], upload["object_key"])] = {
            "file_size": len(PNG_BYTES),
            "mime_type": "image/png",
            "metadata": {},
            "body": PNG_BYTES,
        }
        task_id = _process(client, user["access_token"], workspace_id, upload["document"]["id"])
        with db_sessionmaker() as db:
            task = process_task(
                db,
                task_id,
                storage=fake_storage,
                doctr_client=FailingDocTrClient(),
                ocr_pipeline=OcrPipeline(),
            )
            assert task.status == "queued"
            assert _ocr_artifacts(db, upload["document"]["id"]) == []
            event_types = {event.event_type for event in db.query(TaskEvent).filter_by(task_id=task_id).all()}
            assert "retry_scheduled" in event_types
    finally:
        pass


def test_paddleocr_unavailable_fails_after_retry_budget(
    client, db_sessionmaker, fake_storage, monkeypatch
):
    fake_redis = FakeRedis()
    monkeypatch.setattr("notepatch.modules.tasks.services.task.redis.from_url", lambda *args, **kwargs: fake_redis)
    try:
        user = register_user(client, "ocr-paddle-fail@example.com")
        workspace_id = first_workspace_id(client, user["access_token"])
        upload = _create_document(client, user["access_token"], workspace_id, filename="paper.png", mime_type="image/png")
        fake_storage.objects[(upload["bucket"], upload["object_key"])] = {
            "file_size": len(PNG_BYTES),
            "mime_type": "image/png",
            "metadata": {},
            "body": PNG_BYTES,
        }
        task_id = _process(client, user["access_token"], workspace_id, upload["document"]["id"])
        with db_sessionmaker() as db:
            for _ in range(3):
                task = process_task(
                    db,
                    task_id,
                    storage=fake_storage,
                    doctr_client=FailingDocTrClient(),
                    ocr_pipeline=OcrPipeline(),
                )
            assert task.status == "failed"
            assert "PP-StructureV3 is not available" in (task.error_message or "")
    finally:
        pass


def test_pdf_max_pages_failure_marks_task_failed(client, db_sessionmaker, fake_storage):
    settings = get_settings()
    old_max_pages = settings.ocr_max_pages
    settings.ocr_max_pages = 0
    try:
        user = register_user(client, "ocr-pdf-max@example.com")
        workspace_id = first_workspace_id(client, user["access_token"])
        upload = _create_document(client, user["access_token"], workspace_id, filename="paper.pdf", mime_type="application/pdf")
        pdf_bytes = minimal_pdf_bytes()
        fake_storage.objects[(upload["bucket"], upload["object_key"])] = {
            "file_size": len(pdf_bytes),
            "mime_type": "application/pdf",
            "metadata": {},
            "body": pdf_bytes,
        }
        task_id = _process(client, user["access_token"], workspace_id, upload["document"]["id"])
        with db_sessionmaker() as db:
            task = process_task(db, task_id, storage=fake_storage)
            assert task.status == "failed"
            assert "OCR_MAX_PAGES" in (task.error_message or "")
            event_types = {event.event_type for event in db.query(TaskEvent).filter_by(task_id=task_id).all()}
            assert "failed" in event_types
    finally:
        settings.ocr_max_pages = old_max_pages


def test_docx_requires_conversion_before_ocr(client, db_sessionmaker, fake_storage):
    user = register_user(client, "ocr-docx@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    upload = _create_document(
        client,
        user["access_token"],
        workspace_id,
        filename="notes.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    fake_storage.objects[(upload["bucket"], upload["object_key"])] = {
        "file_size": 8,
        "mime_type": upload["document"]["mime_type"],
        "metadata": {},
        "body": b"docx mock",
    }
    task_id = _process(client, user["access_token"], workspace_id, upload["document"]["id"])
    with db_sessionmaker() as db:
        task = process_task(db, task_id, storage=fake_storage)
        assert task.status == "failed"
        assert "converted to PDF or image" in (task.error_message or "")


def test_malicious_filename_does_not_affect_ocr_object_keys(client, db_sessionmaker, fake_storage):
    user = register_user(client, "ocr-safe-key@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    upload = _create_document(
        client,
        user["access_token"],
        workspace_id,
        filename="../evil path\r\n.png",
        mime_type="image/png",
    )
    fake_storage.objects[(upload["bucket"], upload["object_key"])] = {
        "file_size": len(PNG_BYTES),
        "mime_type": "image/png",
        "metadata": {},
        "body": PNG_BYTES,
    }
    task_id = _process(client, user["access_token"], workspace_id, upload["document"]["id"])
    with db_sessionmaker() as db:
        task = process_task(db, task_id, storage=fake_storage, doctr_client=FailingDocTrClient())
        assert task.status == "succeeded"
        for artifact in _ocr_artifacts(db, upload["document"]["id"]):
            assert artifact.object_key.startswith(
                f"workspaces/{workspace_id}/documents/{upload['document']['id']}/artifacts/"
            )
            assert ".." not in artifact.object_key
            assert "\\" not in artifact.object_key
            assert "\r" not in artifact.object_key
            assert "\n" not in artifact.object_key

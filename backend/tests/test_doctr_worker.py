from pathlib import Path

from notepatch.modules.documents.models.document import DocumentArtifact
from notepatch.modules.tasks.models.task import TaskEvent
from notepatch.modules.tasks.services.executor import process_task
from tests.conftest import auth_headers, first_workspace_id, register_user


PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00"
    b"\x00\x04\x00\x01\xf6\x178U\x00\x00\x00\x00IEND\xaeB`\x82"
)


def minimal_pdf_bytes() -> bytes:
    import fitz  # type: ignore

    pdf = fitz.open()
    page = pdf.new_page(width=200, height=200)
    page.insert_text((32, 72), "OCR smoke")
    return pdf.tobytes()


class FakeDocTrClient:
    def __init__(self) -> None:
        self.uploads: list[dict] = []

    def health(self) -> dict:
        return {"ok": True, "weights_ready": True}

    def rectify_image(
        self,
        file_path: str | Path,
        output_path: str | Path,
        *,
        filename: str,
        content_type: str | None = None,
        ill_rec: bool = True,
    ) -> None:
        path = Path(file_path)
        assert path.exists()
        self.uploads.append({"filename": filename, "content_type": content_type, "ill_rec": ill_rec})
        Path(output_path).write_bytes(PNG_BYTES)


class FailingDocTrClient(FakeDocTrClient):
    def rectify_image(
        self,
        file_path: str | Path,
        output_path: str | Path,
        *,
        filename: str,
        content_type: str | None = None,
        ill_rec: bool = True,
    ) -> None:
        raise RuntimeError("doctr unavailable")


class ExplodingDocTrClient(FakeDocTrClient):
    def health(self) -> dict:
        raise AssertionError("DocTr should not be called for non-image documents")


def create_document_process_task(
    client,
    fake_storage,
    token: str,
    workspace_id: str,
    *,
    filename: str,
    mime_type: str,
) -> tuple[dict, str]:
    upload = client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/upload-session",
        headers=auth_headers(token),
        json={
            "filename": filename,
            "mime_type": mime_type,
            "file_size": len(PNG_BYTES),
            "document_kind": "homework",
            "title": filename,
        },
    )
    assert upload.status_code == 201, upload.text
    upload_payload = upload.json()
    document_id = upload_payload["document"]["id"]
    body = PNG_BYTES if mime_type.startswith("image/") else minimal_pdf_bytes()
    fake_storage.objects[(upload_payload["bucket"], upload_payload["object_key"])] = {
        "file_size": len(body),
        "mime_type": mime_type,
        "metadata": {},
        "body": body,
    }
    completed = client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/complete-upload",
        headers=auth_headers(token),
        json={"upload_session_id": upload_payload["upload_session"]["id"]},
    )
    assert completed.status_code == 200, completed.text
    task = client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/{document_id}/process",
        headers=auth_headers(token),
        json={},
    )
    assert task.status_code == 201, task.text
    return upload_payload, task.json()["id"]


def test_image_document_pipeline_uses_doctr_and_writes_png_artifact(client, db_sessionmaker, fake_storage):
    user = register_user(client, "doctr-image@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    upload, task_id = create_document_process_task(
        client,
        fake_storage,
        user["access_token"],
        workspace_id,
        filename="paper.png",
        mime_type="image/png",
    )
    doctr_client = FakeDocTrClient()

    with db_sessionmaker() as db:
        task = process_task(db, task_id, storage=fake_storage, doctr_client=doctr_client)
        assert task is not None
        assert task.status == "succeeded"
        assert doctr_client.uploads == [{"filename": "paper.png", "content_type": "image/png", "ill_rec": False}]

        artifacts = db.query(DocumentArtifact).filter_by(document_id=upload["document"]["id"]).all()
        doctr_artifacts = [
            artifact
            for artifact in artifacts
            if artifact.artifact_type == "deskewed_image" and artifact.metadata_.get("processor") == "doctr"
        ]
        assert len(doctr_artifacts) == 1
        assert doctr_artifacts[0].mime_type == "image/png"
        assert doctr_artifacts[0].object_key.endswith("/deskewed_image.png")
        assert doctr_artifacts[0].metadata_["illumination_rectification"] is False
        assert (doctr_artifacts[0].bucket, doctr_artifacts[0].object_key) in fake_storage.objects
        ocr_artifacts = [artifact for artifact in artifacts if artifact.artifact_type.startswith("ocr_")]
        assert {artifact.artifact_type for artifact in ocr_artifacts} == {"ocr_json", "ocr_markdown", "ocr_text"}
        assert all(
            artifact.metadata_.get("source_artifact_id") == doctr_artifacts[0].id
            for artifact in ocr_artifacts
        )

        event_types = {event.event_type for event in db.query(TaskEvent).filter_by(task_id=task_id).all()}
        assert {
            "doctr_health",
            "doctr_running",
            "doctr_succeeded",
            "ocr_started",
            "ocr_artifacts_uploaded",
            "succeeded",
        } <= event_types


def test_image_document_pipeline_falls_back_to_original_when_doctr_fails(client, db_sessionmaker, fake_storage):
    user = register_user(client, "doctr-failure@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    upload, task_id = create_document_process_task(
        client,
        fake_storage,
        user["access_token"],
        workspace_id,
        filename="paper.png",
        mime_type="image/png",
    )

    with db_sessionmaker() as db:
        task = process_task(db, task_id, storage=fake_storage, doctr_client=FailingDocTrClient())
        assert task is not None
        assert task.status == "succeeded"
        artifacts = db.query(DocumentArtifact).filter_by(document_id=upload["document"]["id"]).all()
        assert {artifact.artifact_type for artifact in artifacts if artifact.artifact_type.startswith("ocr_")} == {
            "ocr_json",
            "ocr_markdown",
            "ocr_text",
        }
        ocr_json = next(artifact for artifact in artifacts if artifact.artifact_type == "ocr_json")
        assert ocr_json.metadata_.get("source_artifact_id") is None
        event_types = {event.event_type for event in db.query(TaskEvent).filter_by(task_id=task_id).all()}
        assert "warning" in event_types


def test_pdf_document_pipeline_skips_doctr_and_runs_ocr(client, db_sessionmaker, fake_storage):
    user = register_user(client, "doctr-non-image@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    upload, task_id = create_document_process_task(
        client,
        fake_storage,
        user["access_token"],
        workspace_id,
        filename="paper.pdf",
        mime_type="application/pdf",
    )

    with db_sessionmaker() as db:
        task = process_task(db, task_id, storage=fake_storage, doctr_client=ExplodingDocTrClient())
        assert task is not None
        assert task.status == "succeeded"
        artifacts = db.query(DocumentArtifact).filter_by(document_id=upload["document"]["id"]).all()
        assert not any(artifact.artifact_type == "deskewed_image" for artifact in artifacts)
        assert {artifact.artifact_type for artifact in artifacts if artifact.artifact_type.startswith("ocr_")} == {
            "ocr_json",
            "ocr_markdown",
            "ocr_text",
        }

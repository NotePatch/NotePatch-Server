from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest
from sqlalchemy import select

from notepatch.modules.documents.models.document import Document, DocumentArtifact
from notepatch.modules.documents.services.visual_preparation import (
    DocumentVisualPreparationService,
)
from notepatch.modules.tasks.models.task import TaskEvent
from notepatch.modules.tasks.services.task import TaskService
from notepatch.platform.errors import PermanentTaskError, RetryableTaskError
from notepatch.platform.gpu_lease import GpuLeaseService
from tests.conftest import FakeRedis, first_workspace_id, register_user
from tests.test_doctr_worker import FakeDocTrClient, PNG_BYTES
from tests.test_learning_workflow import _create_and_complete_document


def _service(db, fake_storage, doctr_client):
    return DocumentVisualPreparationService(
        db=db,
        tasks=TaskService(db),
        storage=fake_storage,
        doctr_client=doctr_client,
        gpu_lease=GpuLeaseService(client=FakeRedis()),
    )


def _task(db, workspace_id: str, document_id: str):
    return TaskService(db).create_task(
        workspace_id=workspace_id,
        task_type="generate_study_notes",
        resource_type="document",
        resource_id=document_id,
        enqueue=False,
    )


def _doctr_artifact(
    db,
    fake_storage,
    document: Document,
    *,
    persist_object: bool,
    illumination_rectification: bool = False,
):
    artifact_id = str(uuid.uuid4())
    object_key = fake_storage.document_artifact_key(
        document.workspace_id,
        document.id,
        artifact_id,
        "deskewed_image",
        "png",
    )
    if persist_object:
        fake_storage.objects[(document.bucket, object_key)] = {
            "file_size": len(PNG_BYTES),
            "mime_type": "image/png",
            "metadata": {"processor": "doctr"},
            "body": PNG_BYTES,
        }
    artifact = DocumentArtifact(
        id=artifact_id,
        workspace_id=document.workspace_id,
        document_id=document.id,
        artifact_type="deskewed_image",
        bucket=document.bucket,
        object_key=object_key,
        mime_type="image/png",
        file_size=len(PNG_BYTES),
        metadata_={
            "processor": "doctr",
            "illumination_rectification": illumination_rectification,
        },
    )
    db.add(artifact)
    db.commit()
    return artifact


def test_ai_visual_preparation_reuses_existing_doctr_artifact(
    client, db_sessionmaker, fake_storage
):
    user = register_user(client, "visual-reuse@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    upload = _create_and_complete_document(
        client,
        fake_storage,
        user["access_token"],
        workspace_id,
        filename="note.png",
        document_kind="note",
    )
    doctr = FakeDocTrClient()
    with db_sessionmaker() as db:
        document = db.get(Document, upload["document"]["id"])
        existing = _doctr_artifact(db, fake_storage, document, persist_object=True)
        task = _task(db, workspace_id, document.id)

        prepared = _service(db, fake_storage, doctr).ensure_for_ai(task, [document.id])
        events = db.scalars(select(TaskEvent).where(TaskEvent.task_id == task.id)).all()

    assert prepared[document.id].id == existing.id
    assert doctr.uploads == []
    assert "ai_visual_deskewed_reused" in {event.event_type for event in events}


def test_ai_visual_preparation_regenerates_stale_artifact_from_original(
    client, db_sessionmaker, fake_storage
):
    user = register_user(client, "visual-regenerate@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    upload = _create_and_complete_document(
        client,
        fake_storage,
        user["access_token"],
        workspace_id,
        filename="note.png",
        document_kind="note",
    )
    doctr = FakeDocTrClient()
    with db_sessionmaker() as db:
        document = db.get(Document, upload["document"]["id"])
        stale = _doctr_artifact(db, fake_storage, document, persist_object=False)
        task = _task(db, workspace_id, document.id)

        prepared = _service(db, fake_storage, doctr).ensure_for_ai(task, [document.id])
        events = db.scalars(select(TaskEvent).where(TaskEvent.task_id == task.id)).all()

    assert prepared[document.id].id != stale.id
    assert fake_storage.object_exists(prepared[document.id].bucket, prepared[document.id].object_key)
    assert doctr.uploads[0]["filename"] == "note.png"
    event_types = {event.event_type for event in events}
    assert {
        "ai_visual_deskewed_stale",
        "ai_visual_deskewed_regeneration_started",
        "ai_visual_deskewed_regenerated",
    } <= event_types


def test_ai_visual_preparation_replaces_legacy_illumination_artifact(
    client, db_sessionmaker, fake_storage
):
    user = register_user(client, "visual-legacy-illumination@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    upload = _create_and_complete_document(
        client,
        fake_storage,
        user["access_token"],
        workspace_id,
        filename="note.png",
        document_kind="note",
    )
    doctr = FakeDocTrClient()
    with db_sessionmaker() as db:
        document = db.get(Document, upload["document"]["id"])
        legacy = _doctr_artifact(
            db,
            fake_storage,
            document,
            persist_object=True,
            illumination_rectification=True,
        )
        task = _task(db, workspace_id, document.id)

        prepared = _service(db, fake_storage, doctr).ensure_for_ai(task, [document.id])

    assert prepared[document.id].id != legacy.id
    assert prepared[document.id].metadata_["illumination_rectification"] is False
    assert doctr.uploads[0]["ill_rec"] is False


def test_ai_visual_preparation_fails_when_corrected_and_original_are_missing(
    client, db_sessionmaker, fake_storage
):
    user = register_user(client, "visual-missing@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    upload = _create_and_complete_document(
        client,
        fake_storage,
        user["access_token"],
        workspace_id,
        filename="note.png",
        document_kind="note",
    )
    fake_storage.delete_object(upload["bucket"], upload["object_key"])
    doctr = FakeDocTrClient()
    with db_sessionmaker() as db:
        document = db.get(Document, upload["document"]["id"])
        task = _task(db, workspace_id, document.id)
        with pytest.raises(PermanentTaskError, match="Original image is missing"):
            _service(db, fake_storage, doctr).ensure_for_ai(task, [document.id])
        events = db.scalars(select(TaskEvent).where(TaskEvent.task_id == task.id)).all()

    assert doctr.uploads == []
    assert "ai_visual_deskewed_original_missing" in {
        event.event_type for event in events
    }



class _InjectingLease:
    def __init__(self, callback):
        self.callback = callback

    @contextmanager
    def lease(self, *, owner, event_callback=None):
        self.callback()
        yield


def test_ai_visual_preparation_rechecks_inside_gpu_lease(
    client, db_sessionmaker, fake_storage
):
    user = register_user(client, "visual-concurrent@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    upload = _create_and_complete_document(
        client,
        fake_storage,
        user["access_token"],
        workspace_id,
        filename="note.png",
        document_kind="note",
    )
    doctr = FakeDocTrClient()
    with db_sessionmaker() as db:
        document = db.get(Document, upload["document"]["id"])
        task = _task(db, workspace_id, document.id)
        injected = []

        def create_concurrent_artifact():
            injected.append(_doctr_artifact(db, fake_storage, document, persist_object=True))

        service = DocumentVisualPreparationService(
            db=db,
            tasks=TaskService(db),
            storage=fake_storage,
            doctr_client=doctr,
            gpu_lease=_InjectingLease(create_concurrent_artifact),
        )
        prepared = service.ensure_for_ai(task, [document.id])
        events = db.scalars(select(TaskEvent).where(TaskEvent.task_id == task.id)).all()

    assert prepared[document.id].id == injected[0].id
    assert doctr.uploads == []
    event_types = [event.event_type for event in events]
    assert "ai_visual_deskewed_reused" in event_types
    assert "ai_visual_deskewed_regenerated" not in event_types


def test_ai_visual_preparation_classifies_unexpected_doctr_failure_as_retryable(
    client, db_sessionmaker, fake_storage
):
    user = register_user(client, "visual-retry@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    upload = _create_and_complete_document(
        client,
        fake_storage,
        user["access_token"],
        workspace_id,
        filename="note.png",
        document_kind="note",
    )

    class UnexpectedFailureDocTr(FakeDocTrClient):
        def rectify_image(self, *args, **kwargs):
            raise RuntimeError("GPU temporarily unavailable")

    with db_sessionmaker() as db:
        document = db.get(Document, upload["document"]["id"])
        task = _task(db, workspace_id, document.id)
        with pytest.raises(RetryableTaskError, match="GPU temporarily unavailable"):
            _service(db, fake_storage, UnexpectedFailureDocTr()).ensure_for_ai(
                task, [document.id]
            )



def test_ai_visual_preparation_is_workspace_scoped(client, db_sessionmaker, fake_storage):
    alice = register_user(client, "visual-scope-alice@example.com")
    bob = register_user(client, "visual-scope-bob@example.com")
    alice_workspace_id = first_workspace_id(client, alice["access_token"])
    bob_workspace_id = first_workspace_id(client, bob["access_token"])
    bob_upload = _create_and_complete_document(
        client,
        fake_storage,
        bob["access_token"],
        bob_workspace_id,
        filename="private-note.png",
        document_kind="note",
    )
    doctr = FakeDocTrClient()

    with db_sessionmaker() as db:
        task = _task(db, alice_workspace_id, bob_upload["document"]["id"])
        with pytest.raises(PermanentTaskError, match="documents were not found"):
            _service(db, fake_storage, doctr).ensure_for_ai(
                task, [bob_upload["document"]["id"]]
            )

    assert doctr.uploads == []

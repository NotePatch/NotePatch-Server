from datetime import timedelta

from sqlalchemy import select

from notepatch.modules.documents.models.document import Document, DocumentArtifact
from notepatch.modules.learning.models.assignment import LearningUnitAssignment
from notepatch.modules.learning.models.knowledge import KnowledgeChunk
from notepatch.modules.learning.models.learning import LearningUnit
from notepatch.modules.learning.services.assignment import LearningUnitAssignmentService
from notepatch.modules.learning.services.embedding import EmbeddingClientError
from notepatch.modules.tasks.models.task import Task
from notepatch.modules.tasks.models.workflow import WorkflowEvent, WorkflowRun, WorkflowTaskLink
from notepatch.modules.tasks.services.executor import process_task
from notepatch.modules.tasks.services.workflow import WorkflowTracker
from notepatch.platform.config import get_settings
from notepatch.platform.database import utcnow
from tests.conftest import NoQueueTaskService, auth_headers, first_workspace_id, register_user
from tests.test_doctr_worker import FailingDocTrClient
from tests.test_learning_workflow import (
    _create_and_complete_document,
    _latest_task,
    _process_assignment_if_present,
)


def test_upload_exposes_workspace_scoped_workflow(client, db_sessionmaker, fake_storage):
    alice = register_user(client, "workflow-alice@example.com")
    bob = register_user(client, "workflow-bob@example.com")
    alice_workspace = first_workspace_id(client, alice["access_token"])
    bob_workspace = first_workspace_id(client, bob["access_token"])

    upload = client.post(
        f"/api/v1/workspaces/{alice_workspace}/documents/upload-session",
        headers=auth_headers(alice["access_token"]),
        json={
            "filename": "workflow.png",
            "mime_type": "image/png",
            "file_size": 10,
            "document_kind": "courseware",
        },
    )
    assert upload.status_code == 201
    payload = upload.json()
    workflow_id = payload["workflow_run_id"]
    assert payload["document"]["latest_workflow_run_id"] == workflow_id

    own = client.get(
        f"/api/v1/workspaces/{alice_workspace}/workflows/{workflow_id}",
        headers=auth_headers(alice["access_token"]),
    )
    assert own.status_code == 200
    assert own.json()["workflow"]["status"] == "waiting_upload"

    schema = client.get("/api/v1/openapi.json").json()
    assert "/api/v1/workspaces/{workspace_id}/workflows/{workflow_run_id}" in schema["paths"]
    assert client.get(f"/workspaces/{alice_workspace}/workflows/{workflow_id}").status_code == 404

    cross = client.get(
        f"/api/v1/workspaces/{bob_workspace}/workflows/{workflow_id}",
        headers=auth_headers(bob["access_token"]),
    )
    assert cross.status_code == 404


def test_document_workflow_links_assignment_and_downstream_tasks(
    client,
    db_sessionmaker,
    fake_storage,
    monkeypatch,
):
    settings = get_settings()
    monkeypatch.setattr(settings, "auto_learning_pipeline", True)
    user = register_user(client, "workflow-chain@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    upload = _create_and_complete_document(
        client,
        fake_storage,
        user["access_token"],
        workspace_id,
        filename="workflow-chain.png",
        document_kind="courseware",
    )
    document_id = upload["document"]["id"]
    workflow_id = upload["workflow_run_id"]

    with db_sessionmaker() as db:
        process_task(
            db,
            _latest_task(db, workspace_id, "document_processing_pipeline", document_id).id,
            storage=fake_storage,
            doctr_client=FailingDocTrClient(),
        )
        assignment_task = _process_assignment_if_present(db, workspace_id, document_id, fake_storage)
        assert assignment_task is not None
        build_task = _latest_task(db, workspace_id, "build_knowledge_base", document_id)
        links = db.execute(
            select(WorkflowTaskLink, Task)
            .join(Task, Task.id == WorkflowTaskLink.task_id)
            .where(WorkflowTaskLink.workflow_run_id == workflow_id)
        ).all()
        assert {link.stage for link, _task in links} == {
            "ocr",
            "learning_unit_assignment",
            "knowledge_base",
        }
        workflow_event_types = [
            event.event_type
            for event in db.scalars(
                select(WorkflowEvent)
                .where(WorkflowEvent.workflow_run_id == workflow_id)
                .order_by(WorkflowEvent.sequence_no.asc())
            ).all()
        ]
        assert "queued" in workflow_event_types
        assert "succeeded" in workflow_event_types
        run = db.get(WorkflowRun, workflow_id)
        assert run.learning_unit_id is not None
        assert run.core_status == "queued"
        assert build_task.status == "queued"

    response = client.get(
        f"/api/v1/workspaces/{workspace_id}/documents/{document_id}/workflow",
        headers=auth_headers(user["access_token"]),
    )
    assert response.status_code == 200
    assert response.json()["workflow"]["id"] == workflow_id


def test_explicit_and_exact_learning_unit_assignment(client, db_sessionmaker, fake_storage):
    user = register_user(client, "unit-exact@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    with db_sessionmaker() as db:
        unit = LearningUnit(
            workspace_id=workspace_id,
            title="Quadratic Equations",
            subject="math",
            grade_level="g8",
        )
        db.add(unit)
        db.commit()
        unit_id = unit.id

    upload = client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/upload-session",
        headers=auth_headers(user["access_token"]),
        json={
            "filename": "quadratic.png",
            "mime_type": "image/png",
            "file_size": 10,
            "document_kind": "note",
            "learning_unit_title": "Quadratic Equations",
            "subject": "math",
            "grade_level": "g8",
        },
    )
    assert upload.status_code == 201
    document_id = upload.json()["document"]["id"]
    with db_sessionmaker() as db:
        assignment = db.scalar(
            select(LearningUnitAssignment).where(
                LearningUnitAssignment.workspace_id == workspace_id,
                LearningUnitAssignment.document_id == document_id,
            )
        )
        assert assignment.learning_unit_id == unit_id
        assert assignment.method == "exact"

    invalid = client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/upload-session",
        headers=auth_headers(user["access_token"]),
        json={
            "filename": "invalid.png",
            "mime_type": "image/png",
            "file_size": 10,
            "document_kind": "other",
            "learning_unit_id": "00000000-0000-0000-0000-000000000000",
        },
    )
    assert invalid.status_code == 404


class FixedEmbeddingClient:
    def __init__(self, vector):
        self.vector = vector

    def embed(self, texts, *, owner, event_callback=None):
        return [self.vector for _text in texts]


class FailingEmbeddingClient:
    def embed(self, texts, *, owner, event_callback=None):
        raise EmbeddingClientError("embedding unavailable")


def _semantic_fixture(db, fake_storage, workspace_id: str, user_id: str):
    vector = [1.0, *([0.0] * 1023)]
    unit = LearningUnit(workspace_id=workspace_id, title="Linear Functions", subject="math")
    db.add(unit)
    db.flush()
    chunk = KnowledgeChunk(
        workspace_id=workspace_id,
        document_id=None,
        subject="math",
        source_type="openclaw_skill",
        content="linear functions slope intercept",
        embedding=vector,
        metadata_={"learning_unit_id": unit.id},
    )
    document = Document(
        workspace_id=workspace_id,
        uploaded_by=user_id,
        title="Algebra homework",
        original_filename="algebra.png",
        mime_type="image/png",
        file_size=10,
        file_type="image",
        document_kind="homework",
        storage_backend="seaweedfs",
        bucket=fake_storage.bucket,
        object_key=f"workspaces/{workspace_id}/documents/semantic/original/algebra.png",
        status="ready",
        metadata_={"subject": "math"},
    )
    db.add_all([chunk, document])
    db.flush()
    key = f"workspaces/{workspace_id}/documents/{document.id}/artifacts/ocr/ocr.txt"
    fake_storage.objects[(fake_storage.bucket, key)] = {
        "body": b"linear functions slope intercept",
        "file_size": 32,
        "mime_type": "text/plain",
        "metadata": {},
    }
    db.add(
        DocumentArtifact(
            workspace_id=workspace_id,
            document_id=document.id,
            artifact_type="ocr_text",
            bucket=fake_storage.bucket,
            object_key=key,
            mime_type="text/plain",
            file_size=32,
        )
    )
    db.commit()
    return unit, document, vector


def test_semantic_assignment_and_embedding_degradation(
    client,
    db_sessionmaker,
    fake_storage,
):
    user = register_user(client, "unit-semantic@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    with db_sessionmaker() as db:
        unit, document, vector = _semantic_fixture(
            db,
            fake_storage,
            workspace_id,
            user["user"]["id"],
        )
        selected, assignment, warning = LearningUnitAssignmentService(
            db,
            storage=fake_storage,
            embedding_client=FixedEmbeddingClient(vector),
        ).assign_after_ocr(document)
        db.commit()
        assert selected.id == unit.id
        assert assignment.method == "semantic"
        assert assignment.confidence == 1.0
        assert warning is None

        second = Document(
            workspace_id=workspace_id,
            uploaded_by=user["user"]["id"],
            title="Unrelated",
            original_filename="unrelated.png",
            mime_type="image/png",
            file_size=10,
            file_type="image",
            document_kind="note",
            storage_backend="seaweedfs",
            bucket=fake_storage.bucket,
            object_key=f"workspaces/{workspace_id}/documents/unrelated/original/file.png",
            status="ready",
            metadata_={},
        )
        db.add(second)
        db.commit()
        selected, assignment, warning = LearningUnitAssignmentService(
            db,
            storage=fake_storage,
            embedding_client=FailingEmbeddingClient(),
        ).assign_after_ocr(second)
        db.commit()
        assert selected.id != unit.id
        assert assignment.method == "new"
        assert "embedding unavailable" in warning


def test_shared_downstream_task_links_multiple_workflows(db_sessionmaker):
    with db_sessionmaker() as db:
        from notepatch.modules.identity.models.user import User
        from notepatch.modules.identity.models.workspace import Workspace

        user = User(email="workflow-share@example.com", password_hash="hash")
        db.add(user)
        db.flush()
        workspace = Workspace(name="Personal", type="personal", owner_user_id=user.id)
        db.add(workspace)
        db.flush()
        documents = [
            Document(
                workspace_id=workspace.id,
                uploaded_by=user.id,
                original_filename=f"{index}.png",
                file_type="image",
                document_kind="courseware",
                storage_backend="seaweedfs",
                bucket="test",
                object_key=f"workspaces/{workspace.id}/documents/{index}/original/file.png",
                status="ready",
            )
            for index in range(2)
        ]
        db.add_all(documents)
        db.flush()
        tracker = WorkflowTracker(db)
        runs = [
            tracker.create_for_document(document, user_id=user.id, trigger_type="upload", waiting_upload=False)
            for document in documents
        ]
        service = NoQueueTaskService(db)
        parents = []
        for document, run in zip(documents, runs, strict=True):
            parents.append(
                service.create_task(
                    workspace_id=workspace.id,
                    task_type="build_knowledge_base",
                    resource_type="document",
                    resource_id=document.id,
                    payload={"document_id": document.id, "workflow_run_id": run.id},
                )
            )
        child = service.create_task(
            workspace_id=workspace.id,
            task_type="generate_study_notes",
            resource_type="learning_unit",
            resource_id="unit-id",
            payload={"learning_unit_id": "unit-id"},
        )
        for parent in parents:
            parent.result = {"downstream_tasks": [{"id": child.id, "task_type": child.task_type}]}
            tracker.reconcile_downstream(parent)
        db.commit()
        links = db.scalars(
            select(WorkflowTaskLink).where(WorkflowTaskLink.task_id == child.id)
        ).all()
        assert {link.workflow_run_id for link in links} == {run.id for run in runs}


def test_learning_unit_enrichment_task_auto_links_all_pending_workflows(db_sessionmaker):
    with db_sessionmaker() as db:
        from notepatch.modules.identity.models.user import User
        from notepatch.modules.identity.models.workspace import Workspace

        user = User(email="workflow-auto-share@example.com", password_hash="hash")
        db.add(user)
        db.flush()
        workspace = Workspace(name="Personal", type="personal", owner_user_id=user.id)
        db.add(workspace)
        db.flush()
        unit = LearningUnit(workspace_id=workspace.id, title="Shared notes")
        db.add(unit)
        db.flush()
        tracker = WorkflowTracker(db)
        runs = []
        for index in range(2):
            document = Document(
                workspace_id=workspace.id,
                uploaded_by=user.id,
                original_filename=f"note-{index}.png",
                file_type="image",
                document_kind="note",
                storage_backend="seaweedfs",
                bucket="test",
                object_key=(
                    f"workspaces/{workspace.id}/documents/{index}/original/file.png"
                ),
                status="ready",
                metadata_={"learning_unit_id": unit.id},
            )
            db.add(document)
            db.flush()
            run = tracker.create_for_document(
                document,
                user_id=user.id,
                trigger_type="upload",
                waiting_upload=False,
            )
            run.learning_unit_id = unit.id
            run.status = "waiting"
            run.core_status = "succeeded"
            run.enrichment_status = "waiting"
            runs.append(run)
        db.flush()

        child = NoQueueTaskService(db).create_task(
            workspace_id=workspace.id,
            task_type="generate_study_notes",
            resource_type="learning_unit",
            resource_id=unit.id,
            payload={"learning_unit_id": unit.id},
        )

        links = db.scalars(
            select(WorkflowTaskLink).where(WorkflowTaskLink.task_id == child.id)
        ).all()
        assert {link.workflow_run_id for link in links} == {run.id for run in runs}


def test_workflow_reports_waiting_then_partial_success(db_sessionmaker):
    with db_sessionmaker() as db:
        from notepatch.modules.identity.models.user import User
        from notepatch.modules.identity.models.workspace import Workspace

        user = User(email="workflow-status@example.com", password_hash="hash")
        db.add(user)
        db.flush()
        workspace = Workspace(name="Personal", type="personal", owner_user_id=user.id)
        db.add(workspace)
        db.flush()
        document = Document(
            workspace_id=workspace.id,
            uploaded_by=user.id,
            original_filename="status.png",
            file_type="image",
            document_kind="courseware",
            storage_backend="seaweedfs",
            bucket="test",
            object_key=f"workspaces/{workspace.id}/documents/status/original/file.png",
            status="ready",
        )
        db.add(document)
        db.flush()
        tracker = WorkflowTracker(db)
        run = tracker.create_for_document(
            document,
            user_id=user.id,
            trigger_type="upload",
            waiting_upload=False,
        )
        service = NoQueueTaskService(db)
        core = service.create_task(
            workspace_id=workspace.id,
            task_type="build_knowledge_base",
            resource_type="document",
            resource_id=document.id,
            payload={"document_id": document.id, "workflow_run_id": run.id},
        )
        enrichment = service.create_task(
            workspace_id=workspace.id,
            task_type="generate_study_notes",
            resource_type="learning_unit",
            resource_id="unit-id",
            payload={"learning_unit_id": "unit-id", "workflow_run_id": run.id},
        )
        core.status = "succeeded"
        core.progress = 100
        core.result = {"learning_unit_id": "unit-id", "chunk_ids": ["chunk-id"]}
        enrichment.next_attempt_at = utcnow() + timedelta(minutes=5)
        db.flush()
        tracker.recompute(run)
        assert run.core_status == "succeeded"
        assert run.enrichment_status == "waiting"
        assert run.status == "waiting"
        assert run.waiting_until == enrichment.next_attempt_at

        enrichment.status = "failed"
        enrichment.error_message = "notes unavailable"
        enrichment.next_attempt_at = None
        db.flush()
        tracker.recompute(run)
        assert run.core_status == "succeeded"
        assert run.enrichment_status == "failed"
        assert run.status == "partially_succeeded"
        assert run.error_message == "notes unavailable"

        replacement = service.create_task(
            workspace_id=workspace.id,
            task_type="generate_study_notes",
            resource_type="learning_unit",
            resource_id="unit-id",
            payload={"learning_unit_id": "unit-id", "workflow_run_id": run.id},
        )
        replacement.status = "succeeded"
        replacement.progress = 100
        replacement.result = {
            "learning_unit_id": "unit-id",
            "study_note_version_id": "note-version-id",
        }
        db.flush()
        tracker.recompute(run)
        old_link = db.scalar(
            select(WorkflowTaskLink).where(
                WorkflowTaskLink.workflow_run_id == run.id,
                WorkflowTaskLink.task_id == enrichment.id,
            )
        )
        assert old_link.required is False
        assert run.status == "succeeded"
        assert run.error_message is None
        assert run.result == {
            "learning_unit_id": "unit-id",
            "knowledge_chunk_ids": ["chunk-id"],
            "study_note_version_id": "note-version-id",
        }

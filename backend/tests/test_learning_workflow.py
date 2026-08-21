from sqlalchemy import select

from notepatch.platform.config import get_settings
from notepatch.modules.documents.models.document import DocumentArtifact
from notepatch.modules.learning.models.homework import GradingResult, Mistake
from notepatch.modules.learning.models.knowledge import KnowledgeChunk
from notepatch.modules.learning.services.content_operations import LearningContentOperations
from notepatch.modules.learning.models.learning import (
    Flashcard,
    FlashcardDeck,
    KnowledgePointAttempt,
    LearningUnit,
    StudyNoteVersion,
)
from notepatch.modules.tasks.models.task import Task
from notepatch.modules.tasks.services.executor import process_task
from notepatch.modules.tasks.services.task import TaskService
from tests.conftest import auth_headers, first_workspace_id, register_user
from tests.test_doctr_worker import FailingDocTrClient, PNG_BYTES


def _set_auto_learning(enabled: bool):
    settings = get_settings()
    old_value = settings.auto_learning_pipeline
    settings.auto_learning_pipeline = enabled
    return settings, old_value


def _create_and_complete_document(
    client,
    fake_storage,
    token: str,
    workspace_id: str,
    *,
    filename: str,
    document_kind: str,
    metadata: dict | None = None,
) -> dict:
    upload = client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/upload-session",
        headers=auth_headers(token),
        json={
            "filename": filename,
            "mime_type": "image/png",
            "file_size": len(PNG_BYTES),
            "document_kind": document_kind,
            "title": filename,
            "metadata": metadata or {},
        },
    )
    assert upload.status_code == 201, upload.text
    payload = upload.json()
    fake_storage.objects[(payload["bucket"], payload["object_key"])] = {
        "file_size": len(PNG_BYTES),
        "mime_type": "image/png",
        "metadata": {},
        "body": PNG_BYTES,
    }
    complete = client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/complete-upload",
        headers=auth_headers(token),
        json={"upload_session_id": payload["upload_session"]["id"]},
    )
    assert complete.status_code == 200, complete.text
    payload["document"] = complete.json()
    return payload


def _latest_task(db, workspace_id: str, task_type: str, resource_id: str | None = None) -> Task:
    query = select(Task).where(Task.workspace_id == workspace_id, Task.task_type == task_type)
    if resource_id is not None:
        query = query.where(Task.resource_id == resource_id)
    task = db.scalar(query.order_by(Task.created_at.desc()))
    assert task is not None
    return task


def _process_assignment_if_present(db, workspace_id: str, document_id: str, storage) -> Task | None:
    task = db.scalar(
        select(Task)
        .where(
            Task.workspace_id == workspace_id,
            Task.task_type == "assign_learning_unit",
            Task.resource_id == document_id,
            Task.status == "queued",
        )
        .order_by(Task.created_at.desc())
    )
    if task is not None:
        process_task(db, task.id, storage=storage)
    return task


def test_upload_to_ocr_to_knowledge_to_study_note_workflow(client, db_sessionmaker, fake_storage, monkeypatch):
    settings, old_auto = _set_auto_learning(True)
    try:
        user = register_user(client, "learning-courseware@example.com")
        workspace_id = first_workspace_id(client, user["access_token"])
        upload = _create_and_complete_document(
            client,
            fake_storage,
            user["access_token"],
            workspace_id,
            filename="algebra-notes.png",
            document_kind="note",
            metadata={"subject": "math", "grade_level": "g8", "topic": "Quadratic Equations"},
        )
        document_id = upload["document"]["id"]

        with db_sessionmaker() as db:
            process_task(db, _latest_task(db, workspace_id, "document_processing_pipeline", document_id).id, storage=fake_storage, doctr_client=FailingDocTrClient())
            _process_assignment_if_present(db, workspace_id, document_id, fake_storage)
            build_task = _latest_task(db, workspace_id, "build_knowledge_base", document_id)
            assert build_task.status == "queued"

            process_task(db, build_task.id, storage=fake_storage)
            chunks = db.scalars(
                select(KnowledgeChunk).where(
                    KnowledgeChunk.workspace_id == workspace_id,
                    KnowledgeChunk.source_type == "openclaw_skill",
                )
            ).all()
            assert chunks
            assert chunks[0].metadata_.get("skill") == "notepatch_kb_builder"

            learning_unit = db.scalar(select(LearningUnit).where(LearningUnit.workspace_id == workspace_id))
            assert learning_unit is not None
            db.add(
                Mistake(
                    workspace_id=workspace_id,
                    description="Review factoring",
                    status="open",
                    metadata_={"learning_unit_id": learning_unit.id},
                )
            )
            db.commit()
            notes_task = _latest_task(db, workspace_id, "generate_study_notes", learning_unit.id)
            process_task(db, notes_task.id, storage=fake_storage)

            note = db.scalar(select(StudyNoteVersion).where(StudyNoteVersion.workspace_id == workspace_id))
            assert note is not None
            assert note.html_object_key in {key[1] for key in fake_storage.objects}
            assert note.json_object_key in {key[1] for key in fake_storage.objects}
            flashcard_task = _latest_task(db, workspace_id, "generate_flashcards", learning_unit.id)
            highlight_task = _latest_task(db, workspace_id, "highlight_study_notes", learning_unit.id)
            assert highlight_task.status == "queued"
            assert highlight_task.payload["expected_note_version_id"] == note.id
            process_task(db, flashcard_task.id, storage=fake_storage)
            deck = db.scalar(select(FlashcardDeck).where(FlashcardDeck.learning_unit_id == learning_unit.id))
            assert deck is not None
            cards = db.scalars(select(Flashcard).where(Flashcard.deck_id == deck.id)).all()
            assert len(cards) == 2
            assert cards[0].knowledge_point_id == cards[1].knowledge_point_id

            duplicate_task = TaskService(db).create_task(
                workspace_id=workspace_id,
                task_type="generate_flashcards",
                resource_type="learning_unit",
                resource_id=learning_unit.id,
                payload={
                    "learning_unit_id": learning_unit.id,
                    "study_note_version_id": note.id,
                    "expected_attempt_revision": learning_unit.attempt_revision,
                },
                enqueue=False,
            )
            original_lookup = LearningContentOperations._flashcard_deck_for_revision
            lookup_count = 0

            def simulate_concurrent_insert(service, **kwargs):
                nonlocal lookup_count
                lookup_count += 1
                if lookup_count == 1:
                    return None
                return original_lookup(service, **kwargs)

            monkeypatch.setattr(
                LearningContentOperations,
                "_flashcard_deck_for_revision",
                simulate_concurrent_insert,
            )
            process_task(db, duplicate_task.id, storage=fake_storage)
            db.refresh(duplicate_task)
            assert duplicate_task.status == "succeeded"
            assert duplicate_task.result["reused"] is True
            assert duplicate_task.result["flashcard_deck_id"] == deck.id
            assert len(
                db.scalars(
                    select(FlashcardDeck).where(FlashcardDeck.learning_unit_id == learning_unit.id)
                ).all()
            ) == 1

        units = client.get(f"/api/v1/workspaces/{workspace_id}/learning-units", headers=auth_headers(user["access_token"]))
        assert units.status_code == 200, units.text
        assert units.json()[0]["title"] == "algebra-notes.png"

        notes = client.get(
            f"/api/v1/workspaces/{workspace_id}/learning-units/{learning_unit.id}/notes?include_download_url=true",
            headers=auth_headers(user["access_token"]),
        )
        assert notes.status_code == 200, notes.text
        assert notes.json()[0]["download_urls"]["html"].startswith("mock://download/")
    finally:
        settings.auto_learning_pipeline = old_auto



def test_force_knowledge_reprocess_replaces_document_chunks(client, db_sessionmaker, fake_storage):
    settings, old_auto = _set_auto_learning(True)
    try:
        user = register_user(client, "learning-force-kb@example.com")
        workspace_id = first_workspace_id(client, user["access_token"])
        upload = _create_and_complete_document(
            client,
            fake_storage,
            user["access_token"],
            workspace_id,
            filename="force-kb.png",
            document_kind="courseware",
        )
        document_id = upload["document"]["id"]
        with db_sessionmaker() as db:
            process_task(
                db,
                _latest_task(db, workspace_id, "document_processing_pipeline", document_id).id,
                storage=fake_storage,
                doctr_client=FailingDocTrClient(),
            )
            _process_assignment_if_present(db, workspace_id, document_id, fake_storage)
            first = _latest_task(db, workspace_id, "build_knowledge_base", document_id)
            process_task(db, first.id, storage=fake_storage)
            first_chunk_ids = set(
                db.scalars(select(KnowledgeChunk.id).where(KnowledgeChunk.document_id == document_id)).all()
            )
            assert first_chunk_ids
            replacement = TaskService(db).create_task(
                workspace_id=workspace_id,
                task_type="build_knowledge_base",
                resource_type="document",
                resource_id=document_id,
                payload={
                    "document_id": document_id,
                    "learning_unit_id": first.payload["learning_unit_id"],
                    "source_ocr_run_id": first.payload.get("source_ocr_run_id"),
                    "force_reprocess": True,
                },
                enqueue=False,
            )
            process_task(db, replacement.id, storage=fake_storage)
            current_chunk_ids = set(
                db.scalars(select(KnowledgeChunk.id).where(KnowledgeChunk.document_id == document_id)).all()
            )
            assert current_chunk_ids
            assert current_chunk_ids.isdisjoint(first_chunk_ids)
            assert len(current_chunk_ids) == len(first_chunk_ids)
    finally:
        settings.auto_learning_pipeline = old_auto

def test_homework_grading_creates_mistake_knowledge_and_highlights_note(client, db_sessionmaker, fake_storage):
    settings, old_auto = _set_auto_learning(True)
    try:
        user = register_user(client, "learning-homework@example.com")
        workspace_id = first_workspace_id(client, user["access_token"])
        courseware = _create_and_complete_document(
            client,
            fake_storage,
            user["access_token"],
            workspace_id,
            filename="lesson-notes.png",
            document_kind="note",
            metadata={"learning_unit_title": "Linear Functions", "subject": "math"},
        )

        with db_sessionmaker() as db:
            courseware_document_id = courseware["document"]["id"]
            process_task(db, _latest_task(db, workspace_id, "document_processing_pipeline", courseware_document_id).id, storage=fake_storage, doctr_client=FailingDocTrClient())
            _process_assignment_if_present(db, workspace_id, courseware_document_id, fake_storage)
            process_task(db, _latest_task(db, workspace_id, "build_knowledge_base", courseware_document_id).id, storage=fake_storage)
            learning_unit = db.scalar(select(LearningUnit).where(LearningUnit.workspace_id == workspace_id))
            assert learning_unit is not None
            process_task(db, _latest_task(db, workspace_id, "generate_study_notes", learning_unit.id).id, storage=fake_storage)

        homework = _create_and_complete_document(
            client,
            fake_storage,
            user["access_token"],
            workspace_id,
            filename="homework.png",
            document_kind="homework",
            metadata={"learning_unit_id": learning_unit.id},
        )
        homework_document_id = homework["document"]["id"]

        with db_sessionmaker() as db:
            process_task(db, _latest_task(db, workspace_id, "document_processing_pipeline", homework_document_id).id, storage=fake_storage, doctr_client=FailingDocTrClient())
            extract_task = _latest_task(db, workspace_id, "extract_questions", homework_document_id)
            process_task(db, extract_task.id, storage=fake_storage)
            questions_artifact = db.scalar(
                select(DocumentArtifact).where(
                    DocumentArtifact.workspace_id == workspace_id,
                    DocumentArtifact.document_id == homework_document_id,
                    DocumentArtifact.artifact_type == "questions_json",
                )
            )
            assert questions_artifact is not None
            assert questions_artifact.file_size > 0
            grade_task = _latest_task(db, workspace_id, "grade_homework")
            process_task(db, grade_task.id, storage=fake_storage)
            grading = db.scalar(select(GradingResult).where(GradingResult.workspace_id == workspace_id))
            assert grading is not None
            assert grading.grading_mode == "provisional"
            assert grading.confidence == 0.8
            assert db.scalar(select(Mistake).where(Mistake.workspace_id == workspace_id)) is not None
            assert db.scalar(
                select(KnowledgePointAttempt).where(KnowledgePointAttempt.workspace_id == workspace_id)
            ) is not None
            mistake_chunks = db.scalars(
                select(KnowledgeChunk).where(
                    KnowledgeChunk.workspace_id == workspace_id,
                    KnowledgeChunk.source_type == "mistake",
                )
            ).all()
            assert mistake_chunks

            highlight_task = _latest_task(db, workspace_id, "highlight_study_notes", learning_unit.id)
            process_task(db, highlight_task.id, storage=fake_storage)
            note = db.scalar(select(StudyNoteVersion).where(StudyNoteVersion.workspace_id == workspace_id))
            assert note is not None
            assert note.highlighted_html_object_key is not None
            highlighted = fake_storage.objects[(fake_storage.bucket, note.highlighted_html_object_key)]["body"].decode()
            assert "<article" in highlighted

        detail = client.get(
            f"/api/v1/workspaces/{workspace_id}/learning-units/{learning_unit.id}?include_download_url=true",
            headers=auth_headers(user["access_token"]),
        )
        assert detail.status_code == 200, detail.text
        assert detail.json()["latest_note"]["download_urls"]["highlighted_html"].startswith("mock://download/")
    finally:
        settings.auto_learning_pipeline = old_auto


def test_learning_unit_api_is_workspace_scoped(client, db_sessionmaker, fake_storage):
    settings, old_auto = _set_auto_learning(True)
    try:
        alice = register_user(client, "learning-scope-a@example.com")
        bob = register_user(client, "learning-scope-b@example.com")
        alice_workspace_id = first_workspace_id(client, alice["access_token"])
        bob_workspace_id = first_workspace_id(client, bob["access_token"])
        upload = _create_and_complete_document(
            client,
            fake_storage,
            alice["access_token"],
            alice_workspace_id,
            filename="scope.png",
            document_kind="courseware",
            metadata={"learning_unit_title": "Private Unit"},
        )
        with db_sessionmaker() as db:
            process_task(db, _latest_task(db, alice_workspace_id, "document_processing_pipeline", upload["document"]["id"]).id, storage=fake_storage, doctr_client=FailingDocTrClient())
            _process_assignment_if_present(db, alice_workspace_id, upload["document"]["id"], fake_storage)
            learning_unit = db.scalar(select(LearningUnit).where(LearningUnit.workspace_id == alice_workspace_id))
            assert learning_unit is not None

        response = client.get(
            f"/api/v1/workspaces/{bob_workspace_id}/learning-units/{learning_unit.id}",
            headers=auth_headers(bob["access_token"]),
        )
        assert response.status_code == 404
    finally:
        settings.auto_learning_pipeline = old_auto


def test_grading_without_study_note_skips_highlight_task(client, db_sessionmaker, fake_storage):
    settings, old_auto = _set_auto_learning(True)
    try:
        user = register_user(client, "learning-no-note@example.com")
        workspace_id = first_workspace_id(client, user["access_token"])
        homework = _create_and_complete_document(
            client,
            fake_storage,
            user["access_token"],
            workspace_id,
            filename="homework-without-notes.png",
            document_kind="homework",
            metadata={"learning_unit_title": "No Courseware Yet", "subject": "math"},
        )
        document_id = homework["document"]["id"]
        with db_sessionmaker() as db:
            process_task(
                db,
                _latest_task(db, workspace_id, "document_processing_pipeline", document_id).id,
                storage=fake_storage,
                doctr_client=FailingDocTrClient(),
            )
            _process_assignment_if_present(db, workspace_id, document_id, fake_storage)
            process_task(db, _latest_task(db, workspace_id, "extract_questions", document_id).id, storage=fake_storage)
            grade_task = _latest_task(db, workspace_id, "grade_homework")
            process_task(db, grade_task.id, storage=fake_storage)
            db.refresh(grade_task)
            assert grade_task.status == "succeeded"
            assert grade_task.result["note_highlight_status"] == "skipped_no_study_note"
            assert db.scalar(
                select(Task).where(
                    Task.workspace_id == workspace_id,
                    Task.task_type == "highlight_study_notes",
                )
            ) is None
    finally:
        settings.auto_learning_pipeline = old_auto

def test_chat_attachments_and_other_uploads_skip_automatic_learning(
    client, db_sessionmaker, fake_storage
):
    settings, old_auto = _set_auto_learning(True)
    try:
        user = register_user(client, "chat-attachment-routing@example.com")
        workspace_id = first_workspace_id(client, user["access_token"])
        uploads = [
            _create_and_complete_document(
                client,
                fake_storage,
                user["access_token"],
                workspace_id,
                filename=f"{document_kind}.png",
                document_kind=document_kind,
            )
            for document_kind in ("chat_attachment", "other")
        ]

        assert {upload["document"]["status"] for upload in uploads} == {"ready"}
        assert {upload["document"]["scan_status"] for upload in uploads} == {"skipped"}
        document_ids = {upload["document"]["id"] for upload in uploads}
        with db_sessionmaker() as db:
            assert db.scalar(
                select(LearningUnit).where(LearningUnit.workspace_id == workspace_id)
            ) is None
            pipelines = db.scalars(
                select(Task).where(
                    Task.workspace_id == workspace_id,
                    Task.task_type == "document_processing_pipeline",
                    Task.resource_id.in_(document_ids),
                )
            ).all()
            assert pipelines == []

        blocked = client.post(
            f"/api/v1/workspaces/{workspace_id}/documents/{uploads[0]['document']['id']}/process",
            headers=auth_headers(user["access_token"]),
            json={"options": {}},
        )
        assert blocked.status_code == 409
        assert "cannot enter the learning pipeline" in blocked.json()["detail"]
    finally:
        settings.auto_learning_pipeline = old_auto

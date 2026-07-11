from sqlalchemy import select

from notepatch.platform.config import get_settings
from notepatch.modules.learning.models.homework import GradingResult, Mistake
from notepatch.modules.learning.models.knowledge import KnowledgeChunk
from notepatch.modules.learning.models.learning import LearningUnit, StudyNoteVersion
from notepatch.modules.tasks.models.task import Task
from notepatch.modules.tasks.services.executor import process_task
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


def test_upload_to_ocr_to_knowledge_to_study_note_workflow(client, db_sessionmaker, fake_storage):
    settings, old_auto = _set_auto_learning(True)
    try:
        user = register_user(client, "learning-courseware@example.com")
        workspace_id = first_workspace_id(client, user["access_token"])
        upload = _create_and_complete_document(
            client,
            fake_storage,
            user["access_token"],
            workspace_id,
            filename="algebra-courseware.png",
            document_kind="courseware",
            metadata={"subject": "math", "grade_level": "g8", "topic": "Quadratic Equations"},
        )
        document_id = upload["document"]["id"]

        with db_sessionmaker() as db:
            process_task(db, _latest_task(db, workspace_id, "document_processing_pipeline", document_id).id, storage=fake_storage, doctr_client=FailingDocTrClient())
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
            notes_task = _latest_task(db, workspace_id, "generate_study_notes", learning_unit.id)
            process_task(db, notes_task.id, storage=fake_storage)

            note = db.scalar(select(StudyNoteVersion).where(StudyNoteVersion.workspace_id == workspace_id))
            assert note is not None
            assert note.markdown_object_key in {key[1] for key in fake_storage.objects}
            assert note.json_object_key in {key[1] for key in fake_storage.objects}

        units = client.get(f"/api/v1/workspaces/{workspace_id}/learning-units", headers=auth_headers(user["access_token"]))
        assert units.status_code == 200, units.text
        assert units.json()[0]["title"] == "Quadratic Equations"

        notes = client.get(
            f"/api/v1/workspaces/{workspace_id}/learning-units/{learning_unit.id}/notes?include_download_url=true",
            headers=auth_headers(user["access_token"]),
        )
        assert notes.status_code == 200, notes.text
        assert notes.json()[0]["download_urls"]["markdown"].startswith("mock://download/")
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
            filename="lesson.png",
            document_kind="courseware",
            metadata={"learning_unit_title": "Linear Functions", "subject": "math"},
        )

        with db_sessionmaker() as db:
            courseware_document_id = courseware["document"]["id"]
            process_task(db, _latest_task(db, workspace_id, "document_processing_pipeline", courseware_document_id).id, storage=fake_storage, doctr_client=FailingDocTrClient())
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
            grade_task = _latest_task(db, workspace_id, "grade_homework")
            process_task(db, grade_task.id, storage=fake_storage)
            grading = db.scalar(select(GradingResult).where(GradingResult.workspace_id == workspace_id))
            assert grading is not None
            assert grading.grading_mode == "provisional"
            assert grading.confidence == 0.8
            assert db.scalar(select(Mistake).where(Mistake.workspace_id == workspace_id)) is not None
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
            assert note.highlighted_object_key is not None
            highlighted = fake_storage.objects[(fake_storage.bucket, note.highlighted_object_key)]["body"].decode()
            assert "**" in highlighted

        detail = client.get(
            f"/api/v1/workspaces/{workspace_id}/learning-units/{learning_unit.id}?include_download_url=true",
            headers=auth_headers(user["access_token"]),
        )
        assert detail.status_code == 200, detail.text
        assert detail.json()["latest_note"]["download_urls"]["highlighted"].startswith("mock://download/")
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
            learning_unit = db.scalar(select(LearningUnit).where(LearningUnit.workspace_id == alice_workspace_id))
            assert learning_unit is not None

        response = client.get(
            f"/api/v1/workspaces/{bob_workspace_id}/learning-units/{learning_unit.id}",
            headers=auth_headers(bob["access_token"]),
        )
        assert response.status_code == 404
    finally:
        settings.auto_learning_pipeline = old_auto

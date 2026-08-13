from sqlalchemy import select

from notepatch.modules.documents.models.document import Document
from notepatch.modules.identity.models.user import User
from notepatch.modules.learning.models.learning import LearningUnit, LearningUnitDocument, StudyNoteVersion
from notepatch.modules.tasks.models.task import TASK_TYPES, Task
from notepatch.modules.tasks.services.queue import OPENCLAW_BACKED_TASK_TYPES
from notepatch.modules.tasks.services.registry import LEARNING_TASK_TYPES, REGISTERED_TASK_TYPES
from tests.conftest import auth_headers, first_workspace_id, register_user


def _seed_unit(db, workspace_id: str, user_id: str, *, title: str, subject: str = "math", with_note: bool = True):
    unit = LearningUnit(workspace_id=workspace_id, title=title, subject=subject)
    db.add(unit)
    db.flush()
    note = None
    if with_note:
        note = StudyNoteVersion(
            workspace_id=workspace_id,
            learning_unit_id=unit.id,
            version_no=1,
            title=f"{title} notes",
            html_object_key=f"workspaces/{workspace_id}/learning-units/{unit.id}/notes/note.html",
            json_object_key=f"workspaces/{workspace_id}/learning-units/{unit.id}/notes/note.json",
            knowledge_point_ids=[],
            source_document_ids=[],
            source_mistake_ids=[],
        )
        db.add(note)
    document = Document(
        workspace_id=workspace_id,
        uploaded_by=user_id,
        title=f"{title}.pdf",
        original_filename=f"{title}.pdf",
        mime_type="application/pdf",
        file_size=10,
        file_type="pdf",
        document_kind="courseware",
        bucket="notepatch-test",
        object_key=f"workspaces/{workspace_id}/documents/doc/original/source.pdf",
        status="ready",
    )
    db.add(document)
    db.flush()
    db.add(
        LearningUnitDocument(
            workspace_id=workspace_id,
            learning_unit_id=unit.id,
            document_id=document.id,
            role="courseware",
        )
    )
    db.commit()
    return unit, note, document


def test_every_supported_task_type_has_a_registered_handler():
    assert TASK_TYPES == REGISTERED_TASK_TYPES
    assert OPENCLAW_BACKED_TASK_TYPES == {"openclaw_agent_run", *LEARNING_TASK_TYPES}


def test_generate_flashcards_resolves_learning_unit_and_document(client, db_sessionmaker):
    auth = register_user(client, "flashcard-api@example.com")
    token = auth["access_token"]
    workspace_id = first_workspace_id(client, token)
    with db_sessionmaker() as db:
        user_id = db.scalar(select(User.id).where(User.email == "flashcard-api@example.com"))
        unit, note, document = _seed_unit(db, workspace_id, user_id, title="Algebra")

    by_unit = client.post(
        f"/api/v1/workspaces/{workspace_id}/ai/generate-flashcards",
        headers=auth_headers(token),
        json={"learning_unit_id": unit.id},
    )
    assert by_unit.status_code == 201, by_unit.text
    assert by_unit.json()["resource_type"] == "learning_unit"
    assert by_unit.json()["resource_id"] == unit.id
    assert by_unit.json()["payload"]["study_note_version_id"] == note.id

    by_document = client.post(
        f"/api/v1/workspaces/{workspace_id}/ai/generate-flashcards",
        headers=auth_headers(token),
        json={"document_id": document.id},
    )
    assert by_document.status_code == 201, by_document.text
    assert by_document.json()["resource_id"] == unit.id

    with db_sessionmaker() as db:
        tasks = db.scalars(
            select(Task).where(Task.workspace_id == workspace_id, Task.task_type == "generate_flashcards")
        ).all()
        assert len(tasks) == 2
        assert all(task.payload["learning_unit_id"] == unit.id for task in tasks)


def test_generate_flashcards_rejects_missing_note_and_ambiguous_subject(client, db_sessionmaker):
    auth = register_user(client, "flashcard-validation@example.com")
    token = auth["access_token"]
    workspace_id = first_workspace_id(client, token)
    with db_sessionmaker() as db:
        user_id = db.scalar(select(User.id).where(User.email == "flashcard-validation@example.com"))
        no_note, _, _ = _seed_unit(db, workspace_id, user_id, title="No note", with_note=False)
        _seed_unit(db, workspace_id, user_id, title="Geometry one", subject="geometry")
        _seed_unit(db, workspace_id, user_id, title="Geometry two", subject="geometry")

    missing_note = client.post(
        f"/api/v1/workspaces/{workspace_id}/ai/generate-flashcards",
        headers=auth_headers(token),
        json={"learning_unit_id": no_note.id},
    )
    assert missing_note.status_code == 409
    assert missing_note.json()["detail"] == "Learning unit has no study note"

    ambiguous = client.post(
        f"/api/v1/workspaces/{workspace_id}/ai/generate-flashcards",
        headers=auth_headers(token),
        json={"subject": "geometry"},
    )
    assert ambiguous.status_code == 422
    assert "multiple learning units" in ambiguous.json()["detail"]

    empty = client.post(
        f"/api/v1/workspaces/{workspace_id}/ai/generate-flashcards",
        headers=auth_headers(token),
        json={},
    )
    assert empty.status_code == 422

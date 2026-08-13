from pathlib import Path

import pytest
from sqlalchemy import select

from notepatch.modules.documents.services.scanner import (
    DocumentScanError,
    DocumentScanner,
    MalwareDetectedError,
    ScannerUnavailableError,
)
from notepatch.modules.learning.models.learning import LearningUnit, StudyNoteVersion
from notepatch.modules.learning.services.html_notes import validate_note_structure
from notepatch.modules.learning.services.note_render import NoteRenderService
from notepatch.modules.tasks.models.task import Task, TaskEvent
from notepatch.modules.tasks.services.task import TaskService
from notepatch.platform.config import get_settings
from tests.conftest import auth_headers, first_workspace_id, register_user
from tests.test_doctr_worker import PNG_BYTES


def test_versioned_note_theme_is_public_and_immutable(client):
    response = client.get("/api/v1/assets/note-themes/notepatch-paper-v1.css")
    assert response.status_code == 200
    assert "immutable" in response.headers["cache-control"]
    assert ".np-note-theme" in response.text


def test_signed_render_url_prefers_highlight_and_rejects_bad_token(client, db_sessionmaker, fake_storage):
    user = register_user(client, "note-render@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    with db_sessionmaker() as db:
        unit = LearningUnit(workspace_id=workspace_id, title="Algebra")
        db.add(unit)
        db.flush()
        note = StudyNoteVersion(
            workspace_id=workspace_id,
            learning_unit_id=unit.id,
            version_no=1,
            title="Algebra Note",
            html_object_key="plain.html",
            json_object_key="note.json",
            highlighted_html_object_key="highlighted.html",
        )
        db.add(note)
        db.commit()
        db.refresh(note)
        token_url = NoteRenderService().create_url(note, 900)
    fake_storage.objects[(fake_storage.bucket, "plain.html")] = {"body": b"<article class='np-note'>plain</article>"}
    fake_storage.objects[(fake_storage.bucket, "highlighted.html")] = {"body": b"<article class='np-note'>highlighted</article>"}
    response = client.get(token_url)
    assert response.status_code == 200
    assert "highlighted" in response.text
    assert "Content-Security-Policy" in response.headers
    assert client.get("/api/v1/assets/study-notes/render?token=bad-token-value").status_code == 401


def test_scholar_note_structure_requires_visual_contract():
    valid = (
        '<article class="np-note"><header><h1 class="np-note-title">Title</h1>'
        '<p class="np-note-summary">Summary</p></header>'
        '<section class="np-note-section" data-knowledge-point-id="point-1">Body</section></article>'
    )
    validate_note_structure(valid)
    with pytest.raises(ValueError):
        validate_note_structure("<p>markdown-like output</p>")


def test_document_scanner_hashes_detects_and_rejects_mime(tmp_path, monkeypatch):
    settings = get_settings()
    old_clamav = settings.clamav_enabled
    settings.clamav_enabled = True
    try:
        path = tmp_path / "image.png"
        path.write_bytes(PNG_BYTES)
        monkeypatch.setattr(DocumentScanner, "_clamav_scan", lambda self, value: None)
        result = DocumentScanner().scan(path, "image/png")
        assert len(result.sha256) == 64
        assert result.detected_mime_type == "image/png"
        with pytest.raises(DocumentScanError):
            DocumentScanner().scan(path, "application/pdf")
        monkeypatch.setattr(
            DocumentScanner,
            "_clamav_scan",
            lambda self, value: (_ for _ in ()).throw(MalwareDetectedError("Eicar-Signature")),
        )
        with pytest.raises(MalwareDetectedError):
            DocumentScanner().scan(path, "image/png")
        monkeypatch.setattr(
            DocumentScanner,
            "_clamav_scan",
            lambda self, value: (_ for _ in ()).throw(ScannerUnavailableError("clamav unavailable")),
        )
        with pytest.raises(ScannerUnavailableError):
            DocumentScanner().scan(path, "image/png")
    finally:
        settings.clamav_enabled = old_clamav


def test_task_events_have_monotonic_sequence_numbers(db_sessionmaker):
    with db_sessionmaker() as db:
        task = TaskService(db).create_task_record(
            workspace_id=_workspace_id(db),
            task_type="build_knowledge_base",
        )[0]
        TaskService(db).add_event(task, "next", "next")
        db.commit()
        sequences = db.scalars(
            select(TaskEvent.sequence_no).where(TaskEvent.task_id == task.id).order_by(TaskEvent.sequence_no)
        ).all()
        assert sequences == [1, 2]


def _workspace_id(db):
    from notepatch.modules.identity.models.user import User
    from notepatch.modules.identity.models.workspace import Workspace
    from notepatch.platform.security import hash_password

    user = User(email="sequence@example.com", password_hash=hash_password("password123"))
    db.add(user)
    db.flush()
    workspace = Workspace(name="Personal", type="personal", owner_user_id=user.id)
    db.add(workspace)
    db.flush()
    return workspace.id


def test_task_event_stream_resumes_and_closes_on_terminal(client, db_sessionmaker):
    user = register_user(client, "sse@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    with db_sessionmaker() as db:
        service = TaskService(db)
        task, _queue = service.create_task_record(
            workspace_id=workspace_id,
            task_type="build_knowledge_base",
        )
        db.commit()
        task = service.claim_task(task.id)
        assert task is not None
        service.mark_succeeded(task, {"ok": True})
        task_id = task.id
    response = client.get(
        f"/api/v1/workspaces/{workspace_id}/tasks/{task_id}/events/stream",
        headers=auth_headers(user["access_token"]),
    )
    assert response.status_code == 200
    assert "event: task_event" in response.text
    assert "event: done" in response.text
    last_sequence = max(
        int(line.split(":", 1)[1].strip())
        for line in response.text.splitlines()
        if line.startswith("id:")
    )
    resumed = client.get(
        f"/api/v1/workspaces/{workspace_id}/tasks/{task_id}/events/stream",
        headers={**auth_headers(user["access_token"]), "Last-Event-ID": str(last_sequence)},
    )
    assert resumed.status_code == 200
    assert "event: task_event" not in resumed.text
    assert "event: done" in resumed.text


def test_unassigned_uploads_create_distinct_learning_units(client, db_sessionmaker, fake_storage):
    from tests.test_learning_workflow import _create_and_complete_document

    settings = get_settings()
    old_auto = settings.auto_learning_pipeline
    settings.auto_learning_pipeline = True
    try:
        user = register_user(client, "separate-units@example.com")
        workspace_id = first_workspace_id(client, user["access_token"])
        for filename in ("chapter-one.png", "chapter-two.png"):
            _create_and_complete_document(
                client,
                fake_storage,
                user["access_token"],
                workspace_id,
                filename=filename,
                document_kind="courseware",
            )
        with db_sessionmaker() as db:
            units = db.scalars(
                select(LearningUnit).where(LearningUnit.workspace_id == workspace_id)
            ).all()
            assert {unit.title for unit in units} == {"chapter-one.png", "chapter-two.png"}
    finally:
        settings.auto_learning_pipeline = old_auto


def test_learning_unit_merge_is_async_and_workspace_scoped(client, db_sessionmaker):
    alice = register_user(client, "merge-alice@example.com")
    bob = register_user(client, "merge-bob@example.com")
    alice_workspace = first_workspace_id(client, alice["access_token"])
    bob_workspace = first_workspace_id(client, bob["access_token"])
    with db_sessionmaker() as db:
        target = LearningUnit(workspace_id=alice_workspace, title="Target")
        source = LearningUnit(workspace_id=alice_workspace, title="Source")
        db.add_all([target, source])
        db.commit()
        target_id, source_id = target.id, source.id
    response = client.post(
        f"/api/v1/workspaces/{alice_workspace}/learning-units/{target_id}/merge",
        headers=auth_headers(alice["access_token"]),
        json={"source_learning_unit_ids": [source_id]},
    )
    assert response.status_code == 202
    assert response.json()["task_type"] == "merge_learning_units"
    assert response.json()["payload"]["source_learning_unit_ids"] == [source_id]
    cross_workspace = client.post(
        f"/api/v1/workspaces/{bob_workspace}/learning-units/{target_id}/merge",
        headers=auth_headers(bob["access_token"]),
        json={"source_learning_unit_ids": [source_id]},
    )
    assert cross_workspace.status_code == 404


def test_merge_status_closes_after_last_related_task(db_sessionmaker):
    from notepatch.modules.learning.models.learning import LearningUnitDocument
    from notepatch.modules.learning.services.merge import reconcile_learning_unit_merge

    with db_sessionmaker() as db:
        workspace_id = _workspace_id(db)
        unit = LearningUnit(workspace_id=workspace_id, title="Merged", merge_status="rebuilding")
        db.add(unit)
        db.flush()
        current, _ = TaskService(db).create_task_record(
            workspace_id=workspace_id,
            task_type="generate_flashcards",
            resource_type="learning_unit",
            resource_id=unit.id,
            payload={"learning_unit_id": unit.id},
        )
        sibling, _ = TaskService(db).create_task_record(
            workspace_id=workspace_id,
            task_type="generate_study_notes",
            resource_type="learning_unit",
            resource_id=unit.id,
            payload={"learning_unit_id": unit.id},
        )
        db.commit()
        current = TaskService(db).claim_task(current.id)
        assert current is not None
        TaskService(db).mark_succeeded(current, {"ok": True})
        reconcile_learning_unit_merge(db, current)
        db.refresh(unit)
        assert unit.merge_status == "rebuilding"

        sibling = TaskService(db).claim_task(sibling.id)
        assert sibling is not None
        TaskService(db).mark_succeeded(sibling, {"ok": True})
        reconcile_learning_unit_merge(db, sibling)
        db.refresh(unit)
        assert unit.merge_status == "completed"


def test_merge_status_fails_with_related_terminal_failure(db_sessionmaker):
    from notepatch.modules.learning.services.merge import reconcile_learning_unit_merge

    with db_sessionmaker() as db:
        workspace_id = _workspace_id(db)
        unit = LearningUnit(workspace_id=workspace_id, title="Merged", merge_status="rebuilding")
        db.add(unit)
        db.flush()
        task, _ = TaskService(db).create_task_record(
            workspace_id=workspace_id,
            task_type="build_knowledge_base",
            resource_type="learning_unit",
            resource_id=unit.id,
            payload={"learning_unit_id": unit.id},
        )
        db.commit()
        task = TaskService(db).claim_task(task.id)
        assert task is not None
        TaskService(db).mark_failed(task, "upstream failed")
        reconcile_learning_unit_merge(db, task)
        db.refresh(unit)
        assert unit.merge_status == "failed"
        assert unit.metadata_["merge_failed_task_id"] == task.id

from __future__ import annotations

from sqlalchemy import select

from notepatch.modules.learning.models.knowledge import KnowledgeChunk
from notepatch.modules.learning.models.learning import KnowledgePoint, LearningUnit, StudyNoteVersion
from notepatch.modules.learning.models.note_workflow import NoteGapSuggestion
from notepatch.modules.learning.schemas.skills import ScholarNotesResult
from notepatch.modules.learning.services.note_ir import validate_note_ir
from notepatch.modules.tasks.models.task import Task
from notepatch.modules.tasks.services.executor import process_task
from notepatch.modules.tasks.services.task import TaskService
from notepatch.platform.config import get_settings
from tests.conftest import auth_headers, first_workspace_id, register_user
from tests.test_doctr_worker import FailingDocTrClient
from tests.test_learning_workflow import (
    _create_and_complete_document,
    _latest_task,
    _process_assignment_if_present,
)


def _scholar_payload(source_blocks, point_id: str, *, reverse: bool = False, code_text: str | None = None):
    ordered = list(reversed(source_blocks)) if reverse else source_blocks
    return ScholarNotesResult.model_validate(
        {
            "title": "Faithful note",
            "note_ir": {
                "summary": "Faithful transcription",
                "blocks": [
                    {
                        "id": f"ir-{index}",
                        "type": "code" if code_text is not None and index == 0 else "text",
                        "source_block_ids": [source["id"]],
                        "source_document_id": source["document_id"],
                        "page_index": source["page_index"],
                        "bbox": source["bbox"],
                        "reading_order": source["reading_order"],
                        "knowledge_point_id": point_id,
                        "text": code_text if code_text is not None and index == 0 else source["text"],
                    }
                    for index, source in enumerate(ordered)
                ],
            },
            "corrections": [],
            "knowledge_points": [{"id": point_id, "name": "Loops", "section_id": "loops"}],
            "source_document_ids": ["document-1"],
        }
    )


def test_note_ir_enforces_order_and_exact_code_whitespace():
    blocks = [
        {
            "id": "source-1",
            "document_id": "document-1",
            "page_index": 0,
            "bbox": [0, 0, 100, 20],
            "reading_order": 1,
            "text": "while True:\n    break",
        },
        {
            "id": "source-2",
            "document_id": "document-1",
            "page_index": 0,
            "bbox": [0, 30, 100, 50],
            "reading_order": 2,
            "text": "Second annotation",
        },
    ]
    valid = _scholar_payload(blocks, "point-1", code_text=blocks[0]["text"])
    validate_note_ir(valid, blocks, content_edit_level="conceptual", layout_edit_level="minor")

    reordered = _scholar_payload(blocks, "point-1", reverse=True)
    try:
        validate_note_ir(reordered, blocks, content_edit_level="conceptual", layout_edit_level="minor")
    except ValueError as exc:
        assert "reordering" in str(exc)
    else:
        raise AssertionError("minor layout accepted reordered blocks")

    changed_code = _scholar_payload(blocks, "point-1", code_text="while True:\nbreak")
    try:
        validate_note_ir(changed_code, blocks, content_edit_level="conceptual", layout_edit_level="minor")
    except ValueError as exc:
        assert "correction record" in str(exc)
    else:
        raise AssertionError("code indentation changed without a correction record")


def test_preferences_and_note_set_upload_scope(client):
    user = register_user(client, "faithful-note-settings@example.com")
    token = user["access_token"]
    workspace_id = first_workspace_id(client, token)

    preferences = client.patch(
        "/api/v1/auth/preferences",
        headers=auth_headers(token),
        json={
            "note_content_edit_level": "verbatim",
            "note_layout_edit_level": "preserve",
            "note_history_limit": 0,
        },
    )
    assert preferences.status_code == 200, preferences.text
    assert preferences.json()["note_content_edit_level"] == "verbatim"
    assert preferences.json()["note_layout_edit_level"] == "preserve"
    assert preferences.json()["note_history_limit"] == 0

    created = client.post(
        f"/api/v1/workspaces/{workspace_id}/note-sets",
        headers=auth_headers(token),
        json={"title": "Week 1 loops", "expected_page_count": 2},
    )
    assert created.status_code == 201, created.text
    note_set = created.json()
    assert note_set["content_edit_level"] == "verbatim"
    assert note_set["layout_edit_level"] == "preserve"

    first_page = client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/upload-session",
        headers=auth_headers(token),
        json={
            "filename": "page-1.png",
            "mime_type": "image/png",
            "file_size": 10,
            "document_kind": "note",
            "note_set_id": note_set["id"],
            "page_index": 0,
        },
    )
    assert first_page.status_code == 201, first_page.text
    duplicate = client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/upload-session",
        headers=auth_headers(token),
        json={
            "filename": "duplicate.png",
            "mime_type": "image/png",
            "file_size": 10,
            "document_kind": "note",
            "note_set_id": note_set["id"],
            "page_index": 0,
        },
    )
    assert duplicate.status_code == 409
    invalid_kind = client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/upload-session",
        headers=auth_headers(token),
        json={
            "filename": "slides.png",
            "mime_type": "image/png",
            "file_size": 10,
            "document_kind": "courseware",
            "note_set_id": note_set["id"],
            "page_index": 1,
        },
    )
    assert invalid_kind.status_code == 422


def test_courseware_builds_gaps_without_auto_generating_notes(
    client, db_sessionmaker, fake_storage, monkeypatch
):
    monkeypatch.setattr(get_settings(), "auto_learning_pipeline", True)
    user = register_user(client, "courseware-gap@example.com")
    token = user["access_token"]
    workspace_id = first_workspace_id(client, token)
    upload = _create_and_complete_document(
        client,
        fake_storage,
        token,
        workspace_id,
        filename="loops-courseware.png",
        document_kind="courseware",
        metadata={"subject": "computer science", "topic": "loops"},
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
        build = _latest_task(db, workspace_id, "build_knowledge_base", document_id)
        process_task(db, build.id, storage=fake_storage)
        assert db.scalar(
            select(Task).where(
                Task.workspace_id == workspace_id,
                Task.task_type == "generate_study_notes",
            )
        ) is None
        gap_task = _latest_task(db, workspace_id, "detect_note_gaps")
        process_task(db, gap_task.id, storage=fake_storage)
        gap = db.scalar(
            select(NoteGapSuggestion).where(NoteGapSuggestion.workspace_id == workspace_id)
        )
        assert gap is not None
        assert gap.status == "no_base_note"


def test_gap_draft_acceptance_creates_a_new_note_version(
    client, db_sessionmaker, fake_storage
):
    user = register_user(client, "gap-accept@example.com")
    token = user["access_token"]
    workspace_id = first_workspace_id(client, token)
    with db_sessionmaker() as db:
        unit = LearningUnit(workspace_id=workspace_id, title="Loops")
        db.add(unit)
        db.flush()
        point = KnowledgePoint(
            workspace_id=workspace_id,
            learning_unit_id=unit.id,
            name="while loop condition",
            normalized_name="whileloopcondition",
            source_document_ids=[],
            metadata_={},
        )
        db.add(point)
        db.flush()
        note = StudyNoteVersion(
            workspace_id=workspace_id,
            learning_unit_id=unit.id,
            version_no=1,
            title="Loops",
            html_object_key="notes/loops-v1.html",
            json_object_key="notes/loops-v1.json",
            knowledge_point_ids=[],
            source_document_ids=[],
            source_mistake_ids=[],
            metadata_={},
        )
        db.add(note)
        db.flush()
        gap = NoteGapSuggestion(
            workspace_id=workspace_id,
            learning_unit_id=unit.id,
            knowledge_point_id=point.id,
            note_version_id=note.id,
            status="pending",
            source_refs=[
                {
                    "document_id": "source-document",
                    "page_index": 0,
                    "block_id": "block-1",
                    "bbox": [0, 0, 10, 10],
                    "excerpt": "while checks its condition before each iteration",
                }
            ],
            target_section_id="existing",
            target_anchor="#existing",
            metadata_={"knowledge_point_name": point.name},
        )
        db.add(gap)
        db.commit()
        unit_id, gap_id = unit.id, gap.id
        fake_storage.objects[(fake_storage.bucket, note.html_object_key)] = {
            "body": (
                '<article class="np-note"><header class="np-note-header">'
                '<h1 class="np-note-title">Loops</h1>'
                '<p class="np-note-summary">Summary</p></header>'
                '<section id="existing" class="np-note-section">'
                '<h2>Existing</h2></section></article>'
            ).encode(),
            "mime_type": "text/html",
            "metadata": {},
            "file_size": 1,
        }
        fake_storage.put_json_artifact(
            note.json_object_key,
            {"title": "Loops", "knowledge_points": []},
            bucket=fake_storage.bucket,
        )

    queued = client.post(
        f"/api/v1/workspaces/{workspace_id}/learning-units/{unit_id}/note-gaps/{gap_id}/draft",
        headers=auth_headers(token),
        json={},
    )
    assert queued.status_code == 202, queued.text
    task_id = queued.json()["id"]
    with db_sessionmaker() as db:
        process_task(db, task_id, storage=fake_storage)

    accepted = client.post(
        f"/api/v1/workspaces/{workspace_id}/learning-units/{unit_id}/note-gaps/{gap_id}/accept",
        headers=auth_headers(token),
    )
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["note"]["version_no"] == 2
    assert point.id in accepted.json()["note"]["knowledge_point_ids"]


def test_note_ir_rejects_cross_document_source_relation():
    blocks = [
        {
            "id": "source-1",
            "document_id": "document-1",
            "page_index": 0,
            "bbox": [0, 0, 100, 20],
            "reading_order": 1,
            "text": "Source text",
        }
    ]
    result = _scholar_payload(blocks, "point-1")
    result.note_ir.blocks[0].source_document_id = "another-document"
    try:
        validate_note_ir(result, blocks, content_edit_level="verbatim", layout_edit_level="preserve")
    except ValueError as exc:
        assert "source document relation" in str(exc)
    else:
        raise AssertionError("Note IR accepted a cross-document source relation")


def test_note_history_limit_keeps_latest_plus_configured_history(client, db_sessionmaker, fake_storage):
    user = register_user(client, "note-history@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    with db_sessionmaker() as db:
        unit = LearningUnit(workspace_id=workspace_id, title="History")
        db.add(unit)
        db.flush()
        for version_no in range(1, 7):
            note = StudyNoteVersion(
                workspace_id=unit.workspace_id,
                learning_unit_id=unit.id,
                version_no=version_no,
                title=f"Version {version_no}",
                html_object_key=f"notes/{version_no}.html",
                json_object_key=f"notes/{version_no}.json",
                knowledge_point_ids=[],
                source_document_ids=[],
                source_mistake_ids=[],
                metadata_={},
            )
            db.add(note)
            fake_storage.objects[(fake_storage.bucket, note.html_object_key)] = {
                "body": b"html", "mime_type": "text/html", "metadata": {}, "file_size": 4
            }
            fake_storage.put_json_artifact(note.json_object_key, {}, bucket=fake_storage.bucket)
        db.commit()
        cleanup = TaskService(db).create_task(
            workspace_id=unit.workspace_id,
            task_type="purge_study_note_history",
            resource_type="learning_unit",
            resource_id=unit.id,
            payload={"learning_unit_id": unit.id, "keep_history": 3},
            enqueue=False,
        )
        process_task(db, cleanup.id, storage=fake_storage)
        versions = db.scalars(
            select(StudyNoteVersion.version_no)
            .where(StudyNoteVersion.learning_unit_id == unit.id)
            .order_by(StudyNoteVersion.version_no)
        ).all()
        assert versions == [3, 4, 5, 6]

        cleanup = TaskService(db).create_task(
            workspace_id=unit.workspace_id,
            task_type="purge_study_note_history",
            resource_type="learning_unit",
            resource_id=unit.id,
            payload={"learning_unit_id": unit.id, "keep_history": 0},
            enqueue=False,
        )
        process_task(db, cleanup.id, storage=fake_storage)
        versions = db.scalars(
            select(StudyNoteVersion.version_no)
            .where(StudyNoteVersion.learning_unit_id == unit.id)
            .order_by(StudyNoteVersion.version_no)
        ).all()
        assert versions == [6]

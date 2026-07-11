from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from notepatch.platform.config import get_settings
from notepatch.modules.documents.models.document import Document, DocumentArtifact
from notepatch.modules.learning.models.homework import GradingResult, Homework, HomeworkReference, Mistake
from notepatch.modules.learning.models.knowledge import KnowledgeChunk
from notepatch.modules.learning.models.learning import LearningUnit, LearningUnitDocument, StudyNoteVersion
from notepatch.modules.tasks.models.task import Task, TaskEvent
from notepatch.modules.documents.models.upload import UploadSession
from notepatch.platform.errors import TaskCancelledError
from notepatch.modules.tasks.services.task import TaskService
from notepatch.modules.tasks.services.executor import process_task
from tests.conftest import FakeRedis, auth_headers, first_workspace_id, register_user
from tests.fakes import fake_ocr_pipeline
from tests.test_doctr_worker import FailingDocTrClient, PNG_BYTES


def _create_upload(
    client,
    token: str,
    workspace_id: str,
    *,
    filename: str = "source.png",
    document_kind: str = "homework",
):
    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/upload-session",
        headers=auth_headers(token),
        json={
            "filename": filename,
            "mime_type": "image/png",
            "file_size": len(PNG_BYTES),
            "document_kind": document_kind,
            "title": filename,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _complete_upload(client, fake_storage, token: str, workspace_id: str, upload: dict) -> dict:
    fake_storage.objects[(upload["bucket"], upload["object_key"])] = {
        "file_size": len(PNG_BYTES),
        "mime_type": "image/png",
        "metadata": {},
        "body": PNG_BYTES,
    }
    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/complete-upload",
        headers=auth_headers(token),
        json={"upload_session_id": upload["upload_session"]["id"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_process_requires_completed_upload_and_reuses_active_task(client, fake_storage):
    user = register_user(client, "process-consistency@example.com")
    token = user["access_token"]
    workspace_id = first_workspace_id(client, token)
    upload = _create_upload(client, token, workspace_id)
    document_id = upload["document"]["id"]

    incomplete = client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/{document_id}/process",
        headers=auth_headers(token),
        json={"options": {}},
    )
    assert incomplete.status_code == 409

    _complete_upload(client, fake_storage, token, workspace_id, upload)
    first = client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/{document_id}/process",
        headers=auth_headers(token),
        json={"options": {}},
    )
    reused = client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/{document_id}/process",
        headers=auth_headers(token),
        json={"options": {}},
    )
    forced = client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/{document_id}/process",
        headers=auth_headers(token),
        json={"options": {"force_reprocess": True}},
    )

    assert first.status_code == 201
    assert reused.status_code == 201
    assert reused.json()["id"] == first.json()["id"]
    assert forced.status_code == 409


def test_artifact_metadata_requires_configured_bucket_prefix_and_existing_object(client, fake_storage):
    user = register_user(client, "artifact-consistency@example.com")
    token = user["access_token"]
    workspace_id = first_workspace_id(client, token)
    upload = _create_upload(client, token, workspace_id)
    document_id = upload["document"]["id"]
    prefix = f"workspaces/{workspace_id}/documents/{document_id}/artifacts/a"

    missing = client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/{document_id}/artifacts",
        headers=auth_headers(token),
        json={"artifact_type": "ocr_text", "object_key": f"{prefix}/ocr.txt"},
    )
    wrong_bucket = client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/{document_id}/artifacts",
        headers=auth_headers(token),
        json={
            "artifact_type": "ocr_text",
            "bucket": "another-bucket",
            "object_key": f"{prefix}/ocr.txt",
        },
    )
    wrong_prefix = client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/{document_id}/artifacts",
        headers=auth_headers(token),
        json={"artifact_type": "ocr_text", "object_key": "workspaces/other/documents/x/ocr.txt"},
    )

    assert missing.status_code == 409
    assert wrong_bucket.status_code == 400
    assert wrong_prefix.status_code == 400


def test_grading_config_is_partial_and_configuration_changes_cancel_active_grading(
    client, db_sessionmaker
):
    user = register_user(client, "grading-config-consistency@example.com")
    token = user["access_token"]
    workspace_id = first_workspace_id(client, token)
    created = client.post(
        f"/api/v1/workspaces/{workspace_id}/homeworks",
        headers=auth_headers(token),
        json={"title": "Algebra", "rubric_text": "old rubric", "max_score": 80},
    )
    assert created.status_code == 201
    homework_id = created.json()["id"]

    with db_sessionmaker() as db:
        active = TaskService(db).create_task(
            workspace_id=workspace_id,
            task_type="grade_homework",
            resource_type="homework",
            resource_id=homework_id,
            payload={"homework_id": homework_id},
            enqueue=False,
        )
        task_id = active.id

    rubric_only = client.patch(
        f"/api/v1/workspaces/{workspace_id}/homeworks/{homework_id}/grading-config",
        headers=auth_headers(token),
        json={"rubric_text": "new rubric"},
    )
    score_only = client.patch(
        f"/api/v1/workspaces/{workspace_id}/homeworks/{homework_id}/grading-config",
        headers=auth_headers(token),
        json={"max_score": 60},
    )
    cleared = client.patch(
        f"/api/v1/workspaces/{workspace_id}/homeworks/{homework_id}/grading-config",
        headers=auth_headers(token),
        json={"rubric_text": None},
    )
    empty = client.patch(
        f"/api/v1/workspaces/{workspace_id}/homeworks/{homework_id}/grading-config",
        headers=auth_headers(token),
        json={},
    )

    assert rubric_only.status_code == 200
    assert rubric_only.json()["max_score"] == 80
    assert score_only.json()["rubric_text"] == "new rubric"
    assert score_only.json()["max_score"] == 60
    assert cleared.json()["rubric_text"] is None
    assert cleared.json()["max_score"] == 60
    assert empty.status_code == 422
    with db_sessionmaker() as db:
        assert db.get(Task, task_id).status == "cancelled"


def test_grade_rejects_other_student_and_unready_homework(client):
    owner = register_user(client, "grade-owner@example.com")
    other = register_user(client, "grade-other@example.com")
    token = owner["access_token"]
    workspace_id = first_workspace_id(client, token)
    upload = _create_upload(client, token, workspace_id)
    homework = client.post(
        f"/api/v1/workspaces/{workspace_id}/homeworks",
        headers=auth_headers(token),
        json={"title": "Unready", "document_id": upload["document"]["id"]},
    )
    assert homework.status_code == 201
    homework_id = homework.json()["id"]

    other_student = client.post(
        f"/api/v1/workspaces/{workspace_id}/homeworks/{homework_id}/grade",
        headers=auth_headers(token),
        json={"student_user_id": other["user"]["id"]},
    )
    unready = client.post(
        f"/api/v1/workspaces/{workspace_id}/homeworks/{homework_id}/grade",
        headers=auth_headers(token),
        json={},
    )
    assert other_student.status_code == 422
    assert unready.status_code == 409


def test_reference_add_and_delete_cancel_active_grading(client, db_sessionmaker):
    user = register_user(client, "reference-cancel-consistency@example.com")
    token = user["access_token"]
    workspace_id = first_workspace_id(client, token)
    homework = client.post(
        f"/api/v1/workspaces/{workspace_id}/homeworks",
        headers=auth_headers(token),
        json={"title": "Referenced homework"},
    ).json()
    reference_upload = _create_upload(
        client,
        token,
        workspace_id,
        filename="answers.png",
        document_kind="answer_key",
    )

    with db_sessionmaker() as db:
        first = TaskService(db).create_task(
            workspace_id=workspace_id,
            task_type="grade_homework",
            resource_type="homework",
            resource_id=homework["id"],
            payload={"homework_id": homework["id"]},
            enqueue=False,
        )
        first_id = first.id
    added = client.post(
        f"/api/v1/workspaces/{workspace_id}/homeworks/{homework['id']}/references",
        headers=auth_headers(token),
        json={"document_id": reference_upload["document"]["id"], "reference_type": "answer_key"},
    )
    assert added.status_code == 201
    with db_sessionmaker() as db:
        assert db.get(Task, first_id).status == "cancelled"
        second = TaskService(db).create_task(
            workspace_id=workspace_id,
            task_type="grade_homework",
            resource_type="homework",
            resource_id=homework["id"],
            payload={"homework_id": homework["id"]},
            enqueue=False,
        )
        second_id = second.id
    removed = client.delete(
        f"/api/v1/workspaces/{workspace_id}/homeworks/{homework['id']}/references/{added.json()['id']}",
        headers=auth_headers(token),
    )
    assert removed.status_code == 204
    with db_sessionmaker() as db:
        assert db.get(Task, second_id).status == "cancelled"


def test_conversation_delete_cancels_queued_chat_task(client):
    user = register_user(client, "delete-chat-consistency@example.com")
    token = user["access_token"]
    workspace_id = first_workspace_id(client, token)
    created = client.post(
        f"/api/v1/workspaces/{workspace_id}/ai/chat",
        headers=auth_headers(token),
        json={"prompt": "pending question", "input": {}, "options": {}},
    )
    assert created.status_code == 201
    task_id = created.json()["id"]
    conversation_id = created.json()["payload"]["conversation_id"]

    deleted = client.delete(
        f"/api/v1/workspaces/{workspace_id}/ai/conversations/{conversation_id}",
        headers=auth_headers(token),
    )
    task = client.get(
        f"/api/v1/workspaces/{workspace_id}/tasks/{task_id}",
        headers=auth_headers(token),
    )
    assert deleted.status_code == 204
    assert task.json()["status"] == "cancelled"
    assert task.json()["cancel_requested_at"] is not None


def test_deleted_upload_is_not_revived_by_tusd_finish(client, db_sessionmaker, tmp_path: Path):
    user = register_user(client, "delete-upload-webhook@example.com")
    token = user["access_token"]
    workspace_id = first_workspace_id(client, token)
    upload = _create_upload(client, token, workspace_id)
    document_id = upload["document"]["id"]
    deleted = client.delete(
        f"/api/v1/workspaces/{workspace_id}/documents/{document_id}",
        headers=auth_headers(token),
    )
    assert deleted.status_code == 202

    tus_upload_id = "cancelled-upload"
    (tmp_path / tus_upload_id).write_bytes(PNG_BYTES)
    webhook = client.post(
        f"/api/v1/webhooks/tusd?secret={get_settings().tusd_webhook_secret}",
        json={
            "Type": "post-finish",
            "Event": {
                "Upload": {
                    "ID": tus_upload_id,
                    "Size": len(PNG_BYTES),
                    "MetaData": upload["tus_metadata"],
                    "Storage": {"Path": f"/data/{tus_upload_id}"},
                }
            },
        },
    )
    assert webhook.status_code == 200
    assert webhook.json() == {"ok": True, "status": "cancelled", "ignored": True}
    with db_sessionmaker() as db:
        document = db.get(Document, document_id)
        session = db.get(UploadSession, upload["upload_session"]["id"])
        assert document.status == "deleted"
        assert session.status == "cancelled"
        assert db.query(DocumentArtifact).filter_by(document_id=document_id).count() == 0


def test_document_purge_is_idempotent_and_removes_content(
    client, db_sessionmaker, fake_storage, monkeypatch
):
    monkeypatch.setattr("notepatch.modules.documents.services.tusd.TusdService.terminate_upload", lambda *_args, **_kwargs: None)
    user = register_user(client, "purge-document-consistency@example.com")
    token = user["access_token"]
    workspace_id = first_workspace_id(client, token)
    upload = _create_upload(client, token, workspace_id)
    _complete_upload(client, fake_storage, token, workspace_id, upload)
    document_id = upload["document"]["id"]

    deleted = client.delete(
        f"/api/v1/workspaces/{workspace_id}/documents/{document_id}",
        headers=auth_headers(token),
    )
    assert deleted.status_code == 202
    purge_task_id = deleted.json()["purge_task_id"]
    repeated = client.delete(
        f"/api/v1/workspaces/{workspace_id}/documents/{document_id}",
        headers=auth_headers(token),
    )
    assert repeated.status_code == 202
    assert repeated.json()["purge_task_id"] == purge_task_id

    with db_sessionmaker() as db:
        result = process_task(db, purge_task_id, storage=fake_storage)
        assert result.status == "succeeded"
        document = db.get(Document, document_id)
        assert document.status == "deleted"
        assert document.purge_status == "succeeded"
        assert document.original_filename == "[deleted]"
        assert document.object_key == ""
        assert document.mime_type is None
        assert db.query(DocumentArtifact).filter_by(document_id=document_id).count() == 0
        assert db.query(UploadSession).filter_by(document_id=document_id).count() == 0
    assert not any(
        key.startswith(f"workspaces/{workspace_id}/documents/{document_id}/")
        for _bucket, key in fake_storage.objects
    )

    completed = client.delete(
        f"/api/v1/workspaces/{workspace_id}/documents/{document_id}",
        headers=auth_headers(token),
    )
    assert completed.status_code == 202
    assert completed.json()["purge_status"] == "succeeded"
    assert completed.json()["purge_task_id"] == purge_task_id


def test_purging_answer_key_clears_affected_grading_and_preserves_other_source(
    client, db_sessionmaker, fake_storage, monkeypatch
):
    monkeypatch.setattr(TaskService, "enqueue_task", lambda *_args, **_kwargs: True)
    user = register_user(client, "purge-answer-key-consistency@example.com")
    token = user["access_token"]
    workspace_id = first_workspace_id(client, token)
    source_upload = _create_upload(client, token, workspace_id, filename="homework.png")
    answer_upload = _create_upload(
        client,
        token,
        workspace_id,
        filename="answer.png",
        document_kind="answer_key",
    )
    _complete_upload(client, fake_storage, token, workspace_id, source_upload)
    _complete_upload(client, fake_storage, token, workspace_id, answer_upload)
    source_id = source_upload["document"]["id"]
    answer_id = answer_upload["document"]["id"]

    note_key = f"workspaces/{workspace_id}/learning-units/unit/notes/version/study_note.md"
    note_json_key = f"workspaces/{workspace_id}/learning-units/unit/notes/version/study_note.json"
    fake_storage.objects[(fake_storage.bucket, note_key)] = {
        "file_size": 4,
        "mime_type": "text/markdown",
        "metadata": {},
        "body": b"note",
    }
    fake_storage.objects[(fake_storage.bucket, note_json_key)] = {
        "file_size": 2,
        "mime_type": "application/json",
        "metadata": {},
        "body": b"{}",
    }

    with db_sessionmaker() as db:
        source = db.get(Document, source_id)
        source.status = "ready"
        unit = LearningUnit(workspace_id=workspace_id, title="Unit", subject="math")
        db.add(unit)
        db.flush()
        db.add_all(
            (
                LearningUnitDocument(
                    workspace_id=workspace_id,
                    learning_unit_id=unit.id,
                    document_id=source_id,
                    role="homework",
                ),
                LearningUnitDocument(
                    workspace_id=workspace_id,
                    learning_unit_id=unit.id,
                    document_id=answer_id,
                    role="answer_key",
                ),
            )
        )
        homework = Homework(
            workspace_id=workspace_id,
            title="Homework",
            document_id=source_id,
            status="graded",
            created_by_user_id=user["user"]["id"],
            metadata_={"learning_unit_id": unit.id},
        )
        db.add(homework)
        db.flush()
        reference = HomeworkReference(
            workspace_id=workspace_id,
            homework_id=homework.id,
            document_id=answer_id,
            reference_type="answer_key",
        )
        grade_task = Task(
            workspace_id=workspace_id,
            task_type="grade_homework",
            resource_type="homework",
            resource_id=homework.id,
            status="succeeded",
            payload={"homework_id": homework.id, "answer": "sensitive"},
            result={"feedback": "sensitive"},
            progress=100,
        )
        db.add_all((reference, grade_task))
        db.flush()
        grading = GradingResult(
            workspace_id=workspace_id,
            homework_id=homework.id,
            score=95,
            max_score=100,
            metadata_={"task_id": grade_task.id},
        )
        db.add(grading)
        db.flush()
        mistake = Mistake(
            workspace_id=workspace_id,
            grading_result_id=grading.id,
            description="old mistake",
        )
        source_chunk = KnowledgeChunk(
            workspace_id=workspace_id,
            document_id=source_id,
            content="source knowledge",
            metadata_={"learning_unit_id": unit.id},
        )
        mistake_chunk = KnowledgeChunk(
            workspace_id=workspace_id,
            document_id=source_id,
            source_type="mistake",
            content="old mistake knowledge",
            metadata_={"learning_unit_id": unit.id, "task_id": grade_task.id},
        )
        note = StudyNoteVersion(
            workspace_id=workspace_id,
            learning_unit_id=unit.id,
            version_no=1,
            title="Old note",
            markdown_object_key=note_key,
            json_object_key=note_json_key,
            source_document_ids=[source_id, answer_id],
            source_mistake_ids=[mistake.id],
        )
        db.add_all((mistake, source_chunk, mistake_chunk, note))
        db.commit()
        homework_id = homework.id
        unit_id = unit.id
        grade_task_id = grade_task.id

    deleted = client.delete(
        f"/api/v1/workspaces/{workspace_id}/documents/{answer_id}",
        headers=auth_headers(token),
    )
    assert deleted.status_code == 202
    with db_sessionmaker() as db:
        purge = process_task(db, deleted.json()["purge_task_id"], storage=fake_storage)
        assert purge.status == "succeeded"
        assert db.get(Document, source_id).status == "ready"
        assert db.query(HomeworkReference).filter_by(document_id=answer_id).count() == 0
        assert db.query(GradingResult).filter_by(homework_id=homework_id).count() == 0
        assert db.query(Mistake).filter_by(grading_result_id=grading.id).count() == 0
        assert db.get(Homework, homework_id).status == "draft"
        assert db.query(StudyNoteVersion).filter_by(learning_unit_id=unit_id).count() == 0
        chunks = db.query(KnowledgeChunk).filter_by(document_id=source_id).all()
        assert [chunk.content for chunk in chunks] == ["source knowledge"]
        old_task = db.get(Task, grade_task_id)
        assert old_task.payload == {"purged": True, "document_id": answer_id}
        rebuild_types = {
            task.task_type
            for task in db.query(Task).filter(
                Task.workspace_id == workspace_id,
                Task.status == "queued",
            )
        }
        assert {"generate_study_notes", "grade_homework"} <= rebuild_types
    assert (source_upload["bucket"], source_upload["object_key"]) in fake_storage.objects
    assert (fake_storage.bucket, note_key) not in fake_storage.objects


def test_worker_honors_cancellation_before_ocr_artifact_commit(
    client, db_sessionmaker, fake_storage
):
    user = register_user(client, "worker-cancel-consistency@example.com")
    token = user["access_token"]
    workspace_id = first_workspace_id(client, token)
    upload = _create_upload(client, token, workspace_id)
    _complete_upload(client, fake_storage, token, workspace_id, upload)
    document_id = upload["document"]["id"]
    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/{document_id}/process",
        headers=auth_headers(token),
        json={"options": {"auto_learning": False}},
    )
    task_id = response.json()["id"]

    with db_sessionmaker() as db:
        delegate = fake_ocr_pipeline()

        class CancellingPipeline:
            def run(self, **kwargs):
                result = delegate.run(**kwargs)
                running = db.get(Task, task_id)
                TaskService(db).request_cancel(running, "test cancellation")
                return result

            def write_outputs(self, *args, **kwargs):
                return delegate.write_outputs(*args, **kwargs)

        task = process_task(
            db,
            task_id,
            storage=fake_storage,
            doctr_client=FailingDocTrClient(),
            ocr_pipeline=CancellingPipeline(),
        )
        assert task.status == "cancelled"
        assert db.query(DocumentArtifact).filter(
            DocumentArtifact.document_id == document_id,
            DocumentArtifact.artifact_type.in_(("ocr_json", "ocr_markdown", "ocr_text")),
        ).count() == 0


def test_task_claim_is_atomic_and_queue_failure_is_visible(
    client, db_sessionmaker, monkeypatch
):
    user = register_user(client, "task-claim-consistency@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    with db_sessionmaker() as db:
        service = TaskService(db)
        task = service.create_task(
            workspace_id=workspace_id,
            task_type="generate_flashcards",
            payload={},
            enqueue=False,
        )
        assert service.claim_task(task.id) is not None
        assert service.claim_task(task.id) is None
        assert db.query(TaskEvent).filter_by(task_id=task.id, event_type="running").count() == 1

    class BrokenRedis(FakeRedis):
        def rpush(self, key: str, value: str) -> int:
            raise RuntimeError("redis write failed")

    monkeypatch.setattr("notepatch.modules.tasks.services.task.redis.from_url", lambda *_args, **_kwargs: BrokenRedis())
    with db_sessionmaker() as db, pytest.raises(HTTPException) as exc:
        TaskService(db).create_task(
            workspace_id=workspace_id,
            task_type="generate_flashcards",
            payload={},
        )
    assert exc.value.status_code == 503
    with db_sessionmaker() as db:
        failed = db.scalar(
            db.query(Task).filter(Task.workspace_id == workspace_id, Task.status == "failed").statement
        )
        assert failed is not None
        assert "Task queue unavailable" in (failed.error_message or "")


def test_cancellation_wins_task_completion_race(client, db_sessionmaker):
    user = register_user(client, "task-completion-race@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    with db_sessionmaker() as db:
        service = TaskService(db)
        task = service.create_task(
            workspace_id=workspace_id,
            task_type="generate_flashcards",
            payload={},
            enqueue=False,
        )
        task = service.claim_task(task.id)
        assert task is not None
        service.request_cancel(task, "delete won")
        with pytest.raises(TaskCancelledError):
            service.mark_succeeded(task, {"sensitive": "result"})
        db.refresh(task)
        assert task.status == "cancelled"
        assert task.result is None
        assert db.query(TaskEvent).filter_by(task_id=task.id, event_type="succeeded").count() == 0

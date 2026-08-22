from __future__ import annotations

import shutil
from datetime import timedelta, timezone
from pathlib import Path

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from notepatch.platform.config import get_settings
from notepatch.platform.database import utcnow
from notepatch.modules.ai.models.chat import ChatMessage
from notepatch.modules.documents.models.document import Document, DocumentArtifact
from notepatch.modules.learning.models.homework import GradingResult, Homework, HomeworkReference, Mistake, Question
from notepatch.modules.learning.models.knowledge import KnowledgeChunk
from notepatch.modules.learning.models.learning import LearningUnit, LearningUnitDocument, StudyNoteVersion
from notepatch.modules.tasks.models.task import Task, TaskEvent
from notepatch.modules.documents.models.upload import UploadSession
from notepatch.platform.errors import RetryableTaskError
from notepatch.modules.ai.services.runtime import OpenClawUserRuntimeService
from notepatch.platform.storage import StorageService
from notepatch.modules.tasks.services.task import TaskService
from notepatch.modules.documents.services.tusd import TusdService


class DocumentPurgeService:
    def __init__(
        self,
        db: Session,
        storage: StorageService,
        *,
        tusd: TusdService | None = None,
        runtime: OpenClawUserRuntimeService | None = None,
    ) -> None:
        self.db = db
        self.storage = storage
        self.tusd = tusd or TusdService()
        self.runtime = runtime or OpenClawUserRuntimeService()
        self.settings = get_settings()

    def request_purge(self, workspace_id: str, document_id: str) -> tuple[Document, Task]:
        document = self.db.scalar(
            select(Document)
            .where(Document.workspace_id == workspace_id, Document.id == document_id)
            .with_for_update()
        )
        if document is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

        existing = self.db.get(Task, document.purge_task_id) if document.purge_task_id else None
        if existing is not None and document.purge_status in {"queued", "running", "succeeded"}:
            return document, existing

        document.status = "deleted"
        document.purge_status = "queued"
        document.purged_at = None
        sessions = self.db.scalars(
            select(UploadSession).where(
                UploadSession.workspace_id == workspace_id,
                UploadSession.document_id == document.id,
                UploadSession.status.in_(("created", "uploading")),
            )
        ).all()
        for upload_session in sessions:
            upload_session.status = "cancelled"

        tasks = TaskService(self.db)
        for related in self.related_tasks(document):
            if related.task_type != "purge_document":
                tasks.request_cancel(related, "Source document was deleted", commit=False)

        purge_task, queue_name = tasks.create_task_record(
            workspace_id=workspace_id,
            task_type="purge_document",
            resource_type="document",
            resource_id=document.id,
            payload={"document_id": document.id},
            max_attempts=self.settings.purge_task_max_attempts,
        )
        document.purge_task_id = purge_task.id
        self.db.commit()
        self.db.refresh(document)
        self.db.refresh(purge_task)
        if not tasks.enqueue_task(purge_task.id, queue_name=queue_name):
            document.purge_status = "failed"
            self.db.commit()
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Document purge queue is unavailable")
        return document, purge_task

    def purge(self, task: Task) -> dict:
        document = self.db.scalar(
            select(Document).where(
                Document.workspace_id == task.workspace_id,
                Document.id == (task.payload.get("document_id") or task.resource_id),
            )
        )
        if document is None:
            return {"document_id": task.resource_id, "purged": True, "already_absent": True}
        if document.status != "deleted":
            raise RuntimeError("Document must be deleted before purge")

        document.purge_status = "running"
        self.db.commit()
        related_tasks = [item for item in self.related_tasks(document) if item.id != task.id]
        self._wait_for_cancellation(document, related_tasks)

        context = self._collect_context(document, related_tasks)
        previous_unit_ids = task.payload.get("rebuild_learning_unit_ids") or []
        previous_homework_ids = task.payload.get("regrade_homework_ids") or []
        context["rebuild_unit_ids"].update(item for item in previous_unit_ids if isinstance(item, str))
        context["regrade_homework_ids"].update(item for item in previous_homework_ids if isinstance(item, str))
        task.payload = {
            "document_id": document.id,
            "rebuild_learning_unit_ids": sorted(context["rebuild_unit_ids"]),
            "regrade_homework_ids": sorted(context["regrade_homework_ids"]),
        }
        self.db.commit()

        self._delete_external_data(document, context)
        self._delete_database_data(document, context)
        rebuild_tasks = self._schedule_rebuilds(task, context)

        document.title = None
        document.remark = None
        document.remark_source = None
        document.original_filename = "[deleted]"
        document.mime_type = None
        document.file_size = None
        document.file_type = "other"
        document.document_kind = "other"
        document.retention_scope = "workspace"
        document.chat_conversation_id = None
        document.object_key = ""
        document.upload_id = None
        document.tus_upload_url = None
        document.sha256 = None
        document.metadata_ = {"purged": True}
        document.purge_status = "succeeded"
        document.purged_at = utcnow()
        self.db.commit()
        return {
            "document_id": document.id,
            "purged": True,
            "rebuild_tasks": [{"id": item.id, "task_type": item.task_type} for item in rebuild_tasks],
        }

    def related_tasks(self, document: Document) -> list[Task]:
        homeworks = self.db.scalars(
            select(Homework).where(Homework.workspace_id == document.workspace_id, Homework.document_id == document.id)
        ).all()
        referenced_homework_ids = set(
            self.db.scalars(
                select(HomeworkReference.homework_id).where(
                    HomeworkReference.workspace_id == document.workspace_id,
                    HomeworkReference.document_id == document.id,
                )
            ).all()
        )
        homework_ids = {item.id for item in homeworks} | referenced_homework_ids
        unit_ids = set(
            self.db.scalars(
                select(LearningUnitDocument.learning_unit_id).where(
                    LearningUnitDocument.workspace_id == document.workspace_id,
                    LearningUnitDocument.document_id == document.id,
                )
            ).all()
        )
        unit_ids.update(self._learning_unit_ids_for_homeworks(document.workspace_id, homework_ids))
        chunks = self.db.scalars(
            select(KnowledgeChunk).where(
                KnowledgeChunk.workspace_id == document.workspace_id,
                KnowledgeChunk.document_id == document.id,
            )
        ).all()
        chunk_ids = {item.id for item in chunks}
        related: list[Task] = []
        for task in self.db.scalars(select(Task).where(Task.workspace_id == document.workspace_id)).all():
            payload = task.payload or {}
            completed_chat = (
                task.task_type == "openclaw_agent_run"
                and task.resource_type == "chat_conversation"
                and task.status in {"succeeded", "failed", "cancelled"}
            )
            if completed_chat:
                # Completed chat text is user history. Its source references are
                # redacted separately without treating the answer as a derived artifact.
                continue
            mirrored_document_ids = payload.get("mirrored_document_ids") or []
            direct = (
                (task.resource_type == "document" and task.resource_id == document.id)
                or payload.get("document_id") == document.id
                or (task.resource_type == "homework" and task.resource_id in homework_ids)
                or payload.get("homework_id") in homework_ids
                or (task.resource_type == "learning_unit" and task.resource_id in unit_ids)
                or payload.get("learning_unit_id") in unit_ids
                or (
                    task.task_type == "openclaw_agent_run"
                    and task.status in {"queued", "running"}
                    and document.id in mirrored_document_ids
                )
            )
            citations = (task.result or {}).get("citations") if isinstance(task.result, dict) else None
            cited = any(
                isinstance(item, dict)
                and (item.get("document_id") == document.id or item.get("chunk_id") in chunk_ids)
                for item in (citations or [])
            )
            if direct or cited:
                related.append(task)
        return related

    def _wait_for_cancellation(self, document: Document, tasks: list[Task]) -> None:
        now = utcnow()
        grace = timedelta(seconds=self.settings.task_cancellation_grace_seconds)
        waiting = []
        service = TaskService(self.db)
        for item in tasks:
            self.db.refresh(item)
            if item.status != "running":
                continue
            cancel_requested_at = item.cancel_requested_at
            if cancel_requested_at is not None and cancel_requested_at.tzinfo is None:
                cancel_requested_at = cancel_requested_at.replace(tzinfo=timezone.utc)
            if cancel_requested_at and now - cancel_requested_at >= grace:
                service.mark_cancelled(item, "Cancellation grace period elapsed during document purge")
            else:
                service.request_cancel(item, "Source document was deleted")
                waiting.append(item.id)
        if waiting:
            document.purge_status = "queued"
            self.db.commit()
            raise RetryableTaskError(f"Waiting for related tasks to cancel: {', '.join(waiting)}")

    def _collect_context(self, document: Document, related_tasks: list[Task]) -> dict:
        links = self.db.scalars(
            select(LearningUnitDocument).where(
                LearningUnitDocument.workspace_id == document.workspace_id,
                LearningUnitDocument.document_id == document.id,
            )
        ).all()
        unit_ids = {item.learning_unit_id for item in links}
        source_homeworks = self.db.scalars(
            select(Homework).where(Homework.workspace_id == document.workspace_id, Homework.document_id == document.id)
        ).all()
        source_homework_ids = {item.id for item in source_homeworks}
        reference_rows = self.db.scalars(
            select(HomeworkReference).where(
                HomeworkReference.workspace_id == document.workspace_id,
                HomeworkReference.document_id == document.id,
            )
        ).all()
        reference_homework_ids = {item.homework_id for item in reference_rows} - source_homework_ids
        grading_homework_ids = source_homework_ids | reference_homework_ids
        unit_ids.update(self._learning_unit_ids_for_homeworks(document.workspace_id, grading_homework_ids))
        gradings = self.db.scalars(
            select(GradingResult).where(
                GradingResult.workspace_id == document.workspace_id,
                GradingResult.homework_id.in_(grading_homework_ids),
            )
        ).all() if grading_homework_ids else []
        grading_ids = {item.id for item in gradings}
        questions = self.db.scalars(
            select(Question).where(
                Question.workspace_id == document.workspace_id,
                (Question.document_id == document.id) | (Question.homework_id.in_(source_homework_ids)),
            )
        ).all()
        question_ids = {item.id for item in questions}
        mistakes = self.db.scalars(
            select(Mistake).where(
                Mistake.workspace_id == document.workspace_id,
                (Mistake.grading_result_id.in_(grading_ids)) | (Mistake.question_id.in_(question_ids)),
            )
        ).all() if grading_ids or question_ids else []
        notes = self.db.scalars(
            select(StudyNoteVersion).where(
                StudyNoteVersion.workspace_id == document.workspace_id,
                StudyNoteVersion.learning_unit_id.in_(unit_ids),
            )
        ).all() if unit_ids else []
        artifacts = self.db.scalars(
            select(DocumentArtifact).where(
                DocumentArtifact.workspace_id == document.workspace_id,
                DocumentArtifact.document_id == document.id,
            )
        ).all()
        sessions = self.db.scalars(
            select(UploadSession).where(
                UploadSession.workspace_id == document.workspace_id,
                UploadSession.document_id == document.id,
            )
        ).all()
        rebuild_unit_ids = {
            unit_id for unit_id in unit_ids if self._unit_has_other_active_documents(unit_id, document.id)
        }
        return {
            "links": links,
            "unit_ids": unit_ids,
            "rebuild_unit_ids": rebuild_unit_ids,
            "source_homeworks": source_homeworks,
            "source_homework_ids": source_homework_ids,
            "reference_rows": reference_rows,
            "regrade_homework_ids": reference_homework_ids,
            "gradings": gradings,
            "grading_ids": grading_ids,
            "questions": questions,
            "question_ids": question_ids,
            "mistakes": mistakes,
            "notes": notes,
            "artifacts": artifacts,
            "sessions": sessions,
            "related_tasks": related_tasks,
        }

    def _delete_external_data(self, document: Document, context: dict) -> None:
        locations = {(document.bucket, document.object_key)} if document.object_key else set()
        locations.update((item.bucket, item.object_key) for item in context["artifacts"] if item.object_key)
        locations.update(
            (self.storage.bucket, key)
            for item in context["notes"]
            for key in (
                item.html_object_key,
                item.json_object_key,
                item.highlighted_html_object_key,
                item.highlight_map_object_key,
            )
            if key
        )
        locations.update(
            (self.storage.bucket, item.report_storage_key)
            for item in context["gradings"]
            if item.report_storage_key
        )
        for bucket, object_key in locations:
            self.storage.delete_object(bucket, object_key)
        self.storage.delete_prefix(f"workspaces/{document.workspace_id}/documents/{document.id}/")
        for unit_id in context["unit_ids"]:
            self.storage.delete_prefix(f"workspaces/{document.workspace_id}/learning-units/{unit_id}/")
        for related in context["related_tasks"]:
            self.storage.delete_prefix(f"workspaces/{document.workspace_id}/sandbox/tasks/{related.id}/")
        for upload_session in context["sessions"]:
            if upload_session.tus_upload_id:
                self.tusd.terminate_upload(upload_session.tus_upload_id)

        runtime = self.runtime.runtime_for_workspace(self.db, document.workspace_id)
        shutil.rmtree(Path(runtime["workspace_dir"]) / "notepatch" / "documents" / document.id, ignore_errors=True)
        for related in context["related_tasks"]:
            shutil.rmtree(
                Path(runtime["workspace_dir"]) / "notepatch" / "openclaw" / "tasks" / related.id,
                ignore_errors=True,
            )

    def _delete_database_data(self, document: Document, context: dict) -> None:
        self._redact_completed_chat_sources(document)
        related_task_ids = {item.id for item in context["related_tasks"]}
        for message in self.db.scalars(
            select(ChatMessage).where(ChatMessage.workspace_id == document.workspace_id, ChatMessage.task_id.in_(related_task_ids))
        ).all() if related_task_ids else []:
            if message.role == "assistant":
                message.content = ""
                message.status = "failed"
                message.error_message = "Source document was deleted"

        for related in context["related_tasks"]:
            if related.task_type == "purge_document":
                continue
            related.payload = {"purged": True, "document_id": document.id}
            related.result = {"purged": True, "document_id": document.id}
            related.error_message = (
                "Source document was deleted" if related.status in {"failed", "cancelled"} else None
            )
            for event in self.db.scalars(select(TaskEvent).where(TaskEvent.task_id == related.id)).all():
                event.data = {"purged": True, "document_id": document.id}

        for item in context["mistakes"]:
            self.db.delete(item)
        self.db.flush()
        for item in context["gradings"]:
            self.db.delete(item)
        self.db.flush()
        for item in context["questions"]:
            self.db.delete(item)
        self.db.flush()
        for item in context["reference_rows"]:
            self.db.delete(item)
        for item in context["source_homeworks"]:
            refs = self.db.scalars(select(HomeworkReference).where(HomeworkReference.homework_id == item.id)).all()
            for reference in refs:
                self.db.delete(reference)
            self.db.delete(item)
        for homework_id in context["regrade_homework_ids"]:
            homework = self.db.get(Homework, homework_id)
            if homework is not None:
                homework.status = "draft"
        for item in context["notes"]:
            self.db.delete(item)
        for item in context["artifacts"]:
            self.db.delete(item)
        for item in context["sessions"]:
            self.db.delete(item)
        for chunk in self.db.scalars(
            select(KnowledgeChunk).where(KnowledgeChunk.workspace_id == document.workspace_id)
        ).all():
            if chunk.document_id == document.id or (chunk.metadata_ or {}).get("task_id") in related_task_ids:
                self.db.delete(chunk)
        for item in context["links"]:
            self.db.delete(item)
        self.db.flush()

        for unit_id in context["unit_ids"] - context["rebuild_unit_ids"]:
            for chunk in self._unit_chunks(unit_id, document.workspace_id):
                self.db.delete(chunk)
            unit = self.db.get(LearningUnit, unit_id)
            if unit is not None:
                self.db.delete(unit)
        self.db.commit()

    def _redact_completed_chat_sources(self, document: Document) -> None:
        chat_tasks = self.db.scalars(
            select(Task).where(
                Task.workspace_id == document.workspace_id,
                Task.task_type == "openclaw_agent_run",
                Task.resource_type == "chat_conversation",
                Task.status == "succeeded",
            )
        ).all()
        for task in chat_tasks:
            result = dict(task.result or {})
            task_citations = [item for item in (result.get("citations") or []) if isinstance(item, dict)]
            removed_task_citations = [
                item for item in task_citations if item.get("document_id") == document.id
            ]
            remaining_task_citations = [
                item for item in task_citations if item.get("document_id") != document.id
            ]

            message = self.db.scalar(
                select(ChatMessage).where(
                    ChatMessage.workspace_id == document.workspace_id,
                    ChatMessage.task_id == task.id,
                    ChatMessage.role == "assistant",
                )
            )
            message_citations = [
                item for item in ((message.citations if message is not None else None) or task_citations)
                if isinstance(item, dict)
            ]
            removed_message_citations = [
                item for item in message_citations if item.get("document_id") == document.id
            ]
            remaining_message_citations = [
                item for item in message_citations if item.get("document_id") != document.id
            ]
            if not removed_task_citations and not removed_message_citations:
                continue

            source_status = "partially_unavailable" if remaining_message_citations else "unavailable"
            if removed_task_citations:
                result["citations"] = remaining_task_citations
                result["source_status"] = source_status
                task.result = result
            if message is not None:
                message.citations = remaining_message_citations
                message.source_status = source_status

    def _schedule_rebuilds(self, purge_task: Task, context: dict) -> list[Task]:
        created: list[Task] = []
        service = TaskService(self.db)
        for unit_id in context["rebuild_unit_ids"]:
            if self._unit_chunks(unit_id, purge_task.workspace_id):
                unit = self.db.get(LearningUnit, unit_id)
                if unit is None:
                    continue
                run_at = utcnow() + timedelta(seconds=self.settings.study_note_debounce_seconds)
                unit.note_generation_due_at = run_at
                created.append(
                    service.create_delayed_task(
                        workspace_id=purge_task.workspace_id,
                        task_type="generate_study_notes",
                        run_at=run_at,
                        resource_type="learning_unit",
                        resource_id=unit_id,
                        payload={
                            "learning_unit_id": unit_id,
                            "expected_knowledge_revision": unit.knowledge_revision,
                            "reason": "document_purged",
                        },
                    )
                )
        for homework_id in context["regrade_homework_ids"]:
            homework = self.db.get(Homework, homework_id)
            if homework is None or not homework.document_id:
                continue
            source = self.db.get(Document, homework.document_id)
            if source is None or source.status != "ready":
                continue
            created.append(
                service.create_task(
                    workspace_id=purge_task.workspace_id,
                    task_type="grade_homework",
                    resource_type="homework",
                    resource_id=homework.id,
                    payload={
                        "homework_id": homework.id,
                        "student_user_id": homework.created_by_user_id,
                        "reason": "grading_reference_purged",
                    },
                )
            )
        return created

    def _unit_has_other_active_documents(self, unit_id: str, deleted_document_id: str) -> bool:
        return self.db.scalar(
            select(Document.id)
            .join(LearningUnitDocument, LearningUnitDocument.document_id == Document.id)
            .where(
                LearningUnitDocument.learning_unit_id == unit_id,
                Document.id != deleted_document_id,
                Document.status != "deleted",
            )
            .limit(1)
        ) is not None

    def _learning_unit_ids_for_homeworks(self, workspace_id: str, homework_ids: set[str]) -> set[str]:
        if not homework_ids:
            return set()
        unit_ids: set[str] = set()
        homeworks = self.db.scalars(
            select(Homework).where(Homework.workspace_id == workspace_id, Homework.id.in_(homework_ids))
        ).all()
        for homework in homeworks:
            metadata_unit_id = (homework.metadata_ or {}).get("learning_unit_id")
            if isinstance(metadata_unit_id, str) and metadata_unit_id:
                unit_ids.add(metadata_unit_id)
            if homework.document_id:
                linked = self.db.scalar(
                    select(LearningUnitDocument.learning_unit_id).where(
                        LearningUnitDocument.workspace_id == workspace_id,
                        LearningUnitDocument.document_id == homework.document_id,
                    )
                )
                if linked:
                    unit_ids.add(linked)
        return unit_ids

    def _unit_chunks(self, unit_id: str, workspace_id: str) -> list[KnowledgeChunk]:
        chunks = self.db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.workspace_id == workspace_id)).all()
        return [item for item in chunks if (item.metadata_ or {}).get("learning_unit_id") == unit_id]

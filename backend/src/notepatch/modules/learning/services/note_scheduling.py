from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from notepatch.modules.documents.models.document import Document
from notepatch.modules.identity.models.user import User
from notepatch.modules.identity.models.workspace import Workspace
from notepatch.modules.learning.models.learning import LearningUnit
from notepatch.modules.learning.services.note_sets import NoteSetService
from notepatch.modules.tasks.models.task import Task
from notepatch.modules.tasks.services.task import TaskService
from notepatch.platform.config import get_settings
from notepatch.platform.database import utcnow


class NoteSchedulingOperations:
    def schedule_after_knowledge(
        self,
        document: Document,
        unit: LearningUnit,
        *,
        reason: str,
    ) -> Task | None:
        if document.document_kind == "note":
            ready, note_set = NoteSetService(self.db).ready_for_note_generation(
                document.workspace_id, document.id
            )
            if not ready:
                return None
            metadata = document.metadata_ or {}
            content_level = (
                note_set.content_edit_level if note_set is not None
                else metadata.get("note_content_edit_level")
            )
            layout_level = (
                note_set.layout_edit_level if note_set is not None
                else metadata.get("note_layout_edit_level")
            )
            if note_set is not None and note_set.status == "completed":
                note_set.status = "processing"
                self.db.commit()
            return self.schedule_study_notes(
                unit,
                reason=reason,
                content_edit_level=content_level,
                layout_edit_level=layout_level,
            )

        active = self.db.scalar(
            select(Task)
            .where(
                Task.workspace_id == unit.workspace_id,
                Task.task_type == "detect_note_gaps",
                Task.resource_type == "learning_unit",
                Task.resource_id == unit.id,
                Task.status.in_(("queued", "running")),
                Task.cancel_requested_at.is_(None),
            )
            .order_by(Task.created_at.desc())
        )
        if active is not None:
            return active
        return TaskService(self.db).create_task(
            workspace_id=unit.workspace_id,
            task_type="detect_note_gaps",
            resource_type="learning_unit",
            resource_id=unit.id,
            payload={
                "learning_unit_id": unit.id,
                "source_document_id": document.id,
                "reason": reason,
            },
        )

    def schedule_study_notes(
        self,
        unit: LearningUnit,
        *,
        reason: str,
        content_edit_level: str | None = None,
        layout_edit_level: str | None = None,
        history_limit: int | None = None,
    ) -> Task:
        settings = get_settings()
        workspace = self.db.scalar(select(Workspace).where(Workspace.id == unit.workspace_id))
        owner = self.db.get(User, workspace.owner_user_id) if workspace is not None else None
        content_level = content_edit_level or (
            owner.note_content_edit_level if owner is not None else "conceptual"
        )
        layout_level = layout_edit_level or (
            owner.note_layout_edit_level if owner is not None else "minor"
        )
        retained_history = (
            history_limit
            if history_limit is not None
            else owner.note_history_limit if owner is not None else 3
        )
        debounce_seconds = max(0, settings.study_note_debounce_seconds)
        run_at = utcnow() + timedelta(seconds=debounce_seconds)
        service = TaskService(self.db)
        queued = self.db.scalar(
            select(Task)
            .where(
                Task.workspace_id == unit.workspace_id,
                Task.task_type == "generate_study_notes",
                Task.resource_type == "learning_unit",
                Task.resource_id == unit.id,
                Task.status == "queued",
                Task.cancel_requested_at.is_(None),
            )
            .order_by(Task.created_at.desc())
        )
        payload = {
            "learning_unit_id": unit.id,
            "expected_knowledge_revision": unit.knowledge_revision,
            "expected_attempt_revision": unit.attempt_revision,
            "reason": reason,
            "content_edit_level": content_level,
            "layout_edit_level": layout_level,
            "history_limit": max(0, min(100, int(retained_history))),
        }
        unit.note_generation_due_at = run_at if debounce_seconds else None
        if queued is not None:
            queued.payload = payload
            if debounce_seconds == 0:
                if not service.enqueue_task_now(queued):
                    raise RuntimeError("Could not queue study note generation")
                return queued
            if not service.schedule_task_at(queued, run_at):
                raise RuntimeError("Could not reschedule study note generation")
            return queued
        if debounce_seconds == 0:
            return service.create_task(
                workspace_id=unit.workspace_id,
                task_type="generate_study_notes",
                resource_type="learning_unit",
                resource_id=unit.id,
                payload=payload,
            )
        task = service.create_delayed_task(
            workspace_id=unit.workspace_id,
            task_type="generate_study_notes",
            run_at=run_at,
            resource_type="learning_unit",
            resource_id=unit.id,
            payload=payload,
        )
        self.db.refresh(unit)
        return task

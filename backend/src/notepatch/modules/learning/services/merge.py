from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from notepatch.modules.documents.models.document import Document
from notepatch.modules.learning.models.assignment import LearningUnitAssignment
from notepatch.modules.learning.models.homework import Homework, Mistake
from notepatch.modules.learning.models.knowledge import KnowledgeChunk
from notepatch.modules.learning.models.learning import (
    FlashcardDeck,
    KnowledgePoint,
    KnowledgePointAttempt,
    LearningUnit,
    LearningUnitDocument,
    StudyNoteVersion,
)
from notepatch.modules.tasks.models.task import Task
from notepatch.modules.tasks.models.workflow import WorkflowRun
from notepatch.modules.tasks.services.task import TaskService
from notepatch.platform.errors import PermanentTaskError
from notepatch.platform.storage import StorageService


class LearningUnitMergeService:
    def __init__(self, db: Session, storage: StorageService) -> None:
        self.db = db
        self.storage = storage
        self.tasks = TaskService(db)

    def execute(self, task: Task) -> dict:
        target_id = task.payload.get("target_learning_unit_id") or task.resource_id
        source_ids = list(dict.fromkeys(task.payload.get("source_learning_unit_ids") or []))
        target = self.db.scalar(
            select(LearningUnit).where(
                LearningUnit.workspace_id == task.workspace_id,
                LearningUnit.id == target_id,
                LearningUnit.merged_into_id.is_(None),
            ).with_for_update()
        )
        if target is None:
            raise PermanentTaskError("Target learning unit not found")
        sources = self.db.scalars(
            select(LearningUnit).where(
                LearningUnit.workspace_id == task.workspace_id,
                LearningUnit.id.in_(source_ids),
            ).with_for_update()
        ).all()
        if (
            len(sources) != len(source_ids)
            or target.id in source_ids
            or any(source.merged_into_id not in {None, target.id} for source in sources)
        ):
            raise PermanentTaskError("One or more source learning units are unavailable")
        unit_ids = [target.id, *source_ids]
        target.merge_status = "rebuilding"
        for source in sources:
            source.merge_status = "merging"
        self.tasks.add_event(task, "merge_started", "Learning unit merge started", progress=10)
        self.db.commit()

        links = self.db.scalars(
            select(LearningUnitDocument).where(
                LearningUnitDocument.workspace_id == task.workspace_id,
                LearningUnitDocument.learning_unit_id.in_(unit_ids),
            )
        ).all()
        document_roles: dict[str, str] = {}
        for link in links:
            document_roles.setdefault(link.document_id, link.role)
        document_ids = list(document_roles)
        self._cancel_related_tasks(task, unit_ids, document_ids)
        self._merge_knowledge_points(task.workspace_id, target.id, source_ids)
        self._retarget_homeworks(task.workspace_id, target.id, unit_ids, document_ids)
        target.attempt_revision = sum(unit.attempt_revision for unit in (target, *sources))
        for source in sources:
            source.attempt_revision = 0

        decks = self.db.scalars(
            select(FlashcardDeck).where(
                FlashcardDeck.workspace_id == task.workspace_id,
                FlashcardDeck.learning_unit_id.in_(unit_ids),
            )
        ).all()
        for deck in decks:
            self.db.delete(deck)
        notes = self.db.scalars(
            select(StudyNoteVersion).where(
                StudyNoteVersion.workspace_id == task.workspace_id,
                StudyNoteVersion.learning_unit_id.in_(unit_ids),
            )
        ).all()
        for note in notes:
            self.db.delete(note)
        chunks = self.db.scalars(
            select(KnowledgeChunk).where(KnowledgeChunk.workspace_id == task.workspace_id)
        ).all()
        for chunk in chunks:
            if (chunk.metadata_ or {}).get("learning_unit_id") in unit_ids:
                self.db.delete(chunk)

        existing_target_docs = {
            row.document_id
            for row in self.db.scalars(
                select(LearningUnitDocument).where(
                    LearningUnitDocument.workspace_id == task.workspace_id,
                    LearningUnitDocument.learning_unit_id == target.id,
                )
            ).all()
        }
        for link in links:
            if link.learning_unit_id != target.id:
                self.db.delete(link)
        self.db.flush()
        for document_id, role in document_roles.items():
            if document_id not in existing_target_docs:
                self.db.add(
                    LearningUnitDocument(
                        workspace_id=task.workspace_id,
                        learning_unit_id=target.id,
                        document_id=document_id,
                        role=role,
                    )
                )
            document = self.db.scalar(
                select(Document).where(
                    Document.workspace_id == task.workspace_id,
                    Document.id == document_id,
                )
            )
            if document is not None:
                metadata = dict(document.metadata_ or {})
                metadata["learning_unit_id"] = target.id
                document.metadata_ = metadata

        assignments = self.db.scalars(
            select(LearningUnitAssignment).where(
                LearningUnitAssignment.workspace_id == task.workspace_id,
                LearningUnitAssignment.learning_unit_id.in_(source_ids),
            )
        ).all()
        for assignment in assignments:
            evidence = dict(assignment.evidence or {})
            evidence["merged_from_learning_unit_id"] = assignment.learning_unit_id
            evidence["merge_task_id"] = task.id
            assignment.learning_unit_id = target.id
            assignment.status = "reassigned"
            assignment.evidence = evidence
        workflows = self.db.scalars(
            select(WorkflowRun).where(
                WorkflowRun.workspace_id == task.workspace_id,
                WorkflowRun.learning_unit_id.in_(source_ids),
            )
        ).all()
        for workflow in workflows:
            workflow.learning_unit_id = target.id

        for source in sources:
            source.merged_into_id = target.id
            source.merge_status = "merged"
        target.knowledge_revision = 0
        target.notes_generated_revision = 0
        target.note_generation_due_at = None
        target.merge_status = "rebuilding"
        self.db.commit()

        for unit_id in unit_ids:
            self.storage.delete_prefix(f"workspaces/{task.workspace_id}/learning-units/{unit_id}/notes/")

        downstream = []
        for document_id in document_ids:
            document = self.db.scalar(
                select(Document).where(
                    Document.workspace_id == task.workspace_id,
                    Document.id == document_id,
                    Document.status.in_(("uploaded", "ready", "failed")),
                )
            )
            if document is None:
                continue
            child = self.tasks.create_task(
                workspace_id=task.workspace_id,
                task_type="document_processing_pipeline",
                resource_type="document",
                resource_id=document.id,
                payload={
                    "document_id": document.id,
                    "learning_unit_id": target.id,
                    "options": {"auto_learning": True, "force_reprocess": True},
                    "merge_task_id": task.id,
                },
            )
            downstream.append(child)
        target.merge_status = "rebuilding" if downstream else "completed"
        self.db.commit()
        return {
            "target_learning_unit_id": target.id,
            "source_learning_unit_ids": source_ids,
            "document_ids": document_ids,
            "downstream_tasks": [{"id": item.id, "task_type": item.task_type} for item in downstream],
        }

    def _cancel_related_tasks(self, current: Task, unit_ids: list[str], document_ids: list[str]) -> None:
        active = self.db.scalars(
            select(Task).where(
                Task.workspace_id == current.workspace_id,
                Task.status.in_(("queued", "running")),
                Task.id != current.id,
            )
        ).all()
        for task in active:
            payload_unit = (task.payload or {}).get("learning_unit_id")
            if task.resource_id in unit_ids or task.resource_id in document_ids or payload_unit in unit_ids:
                self.tasks.request_cancel(task, "Learning unit merge requested", commit=False)
        self.db.commit()

    def _merge_knowledge_points(self, workspace_id: str, target_id: str, source_ids: list[str]) -> None:
        target_points = {
            point.normalized_name: point
            for point in self.db.scalars(
                select(KnowledgePoint).where(
                    KnowledgePoint.workspace_id == workspace_id,
                    KnowledgePoint.learning_unit_id == target_id,
                )
            ).all()
        }
        source_points = self.db.scalars(
            select(KnowledgePoint).where(
                KnowledgePoint.workspace_id == workspace_id,
                KnowledgePoint.learning_unit_id.in_(source_ids),
            )
        ).all()
        for point in source_points:
            duplicate = target_points.get(point.normalized_name)
            attempts = self.db.scalars(
                select(KnowledgePointAttempt).where(
                    KnowledgePointAttempt.workspace_id == workspace_id,
                    KnowledgePointAttempt.knowledge_point_id == point.id,
                )
            ).all()
            mistakes = self.db.scalars(
                select(Mistake).where(
                    Mistake.workspace_id == workspace_id,
                    Mistake.knowledge_point_id == point.id,
                )
            ).all()
            if duplicate is None:
                point.learning_unit_id = target_id
                for attempt in attempts:
                    attempt.learning_unit_id = target_id
                for mistake in mistakes:
                    metadata = dict(mistake.metadata_ or {})
                    metadata["learning_unit_id"] = target_id
                    mistake.metadata_ = metadata
                target_points[point.normalized_name] = point
                continue
            duplicate.source_document_ids = sorted(
                set(duplicate.source_document_ids or []) | set(point.source_document_ids or [])
            )
            for attempt in attempts:
                collision = self.db.scalar(
                    select(KnowledgePointAttempt.id).where(
                        KnowledgePointAttempt.grading_result_id == attempt.grading_result_id,
                        KnowledgePointAttempt.question_id == attempt.question_id,
                        KnowledgePointAttempt.knowledge_point_id == duplicate.id,
                    )
                )
                if collision:
                    self.db.delete(attempt)
                else:
                    attempt.knowledge_point_id = duplicate.id
                    attempt.learning_unit_id = target_id
            for mistake in mistakes:
                mistake.knowledge_point_id = duplicate.id
                metadata = dict(mistake.metadata_ or {})
                metadata["learning_unit_id"] = target_id
                mistake.metadata_ = metadata
            self.db.delete(point)

        stray_attempts = self.db.scalars(
            select(KnowledgePointAttempt).where(
                KnowledgePointAttempt.workspace_id == workspace_id,
                KnowledgePointAttempt.learning_unit_id.in_(source_ids),
            )
        ).all()
        for attempt in stray_attempts:
            attempt.learning_unit_id = target_id
        mistakes = self.db.scalars(
            select(Mistake).where(Mistake.workspace_id == workspace_id)
        ).all()
        source_id_set = set(source_ids)
        for mistake in mistakes:
            metadata = dict(mistake.metadata_ or {})
            if metadata.get("learning_unit_id") in source_id_set:
                metadata["learning_unit_id"] = target_id
                mistake.metadata_ = metadata

    def _retarget_homeworks(
        self,
        workspace_id: str,
        target_id: str,
        unit_ids: list[str],
        document_ids: list[str],
    ) -> None:
        homeworks = self.db.scalars(
            select(Homework).where(Homework.workspace_id == workspace_id)
        ).all()
        document_id_set = set(document_ids)
        unit_id_set = set(unit_ids)
        for homework in homeworks:
            metadata = dict(homework.metadata_ or {})
            if metadata.get("learning_unit_id") in unit_id_set or homework.document_id in document_id_set:
                metadata["learning_unit_id"] = target_id
                homework.metadata_ = metadata


def reconcile_learning_unit_merge(db: Session, completed_task: Task) -> None:
    """Close a merge only after its last related rebuild task reaches a terminal state."""
    if completed_task.status in {"queued", "running"}:
        return
    unit_ids = _task_learning_unit_ids(db, completed_task)
    if not unit_ids:
        return
    for unit_id in unit_ids:
        unit = db.scalar(
            select(LearningUnit).where(
                LearningUnit.workspace_id == completed_task.workspace_id,
                LearningUnit.id == unit_id,
                LearningUnit.merge_status.in_(("rebuilding", "failed")),
            )
        )
        if unit is None:
            continue
        metadata = dict(unit.metadata_ or {})
        retry_of_task_id = (completed_task.payload or {}).get("retry_of_task_id")
        if unit.merge_status == "failed":
            if (
                completed_task.status != "succeeded"
                or not retry_of_task_id
                or retry_of_task_id != metadata.get("merge_failed_task_id")
            ):
                continue
            unit.merge_status = "rebuilding"
            metadata.pop("merge_failed_task_id", None)
            metadata.pop("merge_failed_task_type", None)
            unit.metadata_ = metadata
        if completed_task.status in {"failed", "cancelled"}:
            unit.merge_status = "failed"
            metadata["merge_failed_task_id"] = completed_task.id
            metadata["merge_failed_task_type"] = completed_task.task_type
            unit.metadata_ = metadata
            continue

        document_ids = set(
            db.scalars(
                select(LearningUnitDocument.document_id).where(
                    LearningUnitDocument.workspace_id == unit.workspace_id,
                    LearningUnitDocument.learning_unit_id == unit.id,
                )
            ).all()
        )
        active_tasks = db.scalars(
            select(Task).where(
                Task.workspace_id == unit.workspace_id,
                Task.id != completed_task.id,
                Task.status.in_(("queued", "running")),
                Task.cancel_requested_at.is_(None),
            )
        ).all()
        if any(_task_is_related_to_unit(item, unit.id, document_ids) for item in active_tasks):
            continue
        unit.merge_status = "completed"
        metadata["merge_completed_by_task_id"] = completed_task.id
        metadata["merge_completed_by_task_type"] = completed_task.task_type
        unit.metadata_ = metadata
    db.commit()


def _task_learning_unit_ids(db: Session, task: Task) -> set[str]:
    payload = task.payload or {}
    values = {
        value
        for value in (payload.get("learning_unit_id"), payload.get("target_learning_unit_id"))
        if isinstance(value, str) and value
    }
    if task.resource_type == "learning_unit" and task.resource_id:
        values.add(task.resource_id)
    if task.resource_type == "document" and task.resource_id:
        values.update(
            db.scalars(
                select(LearningUnitDocument.learning_unit_id).where(
                    LearningUnitDocument.workspace_id == task.workspace_id,
                    LearningUnitDocument.document_id == task.resource_id,
                )
            ).all()
        )
    return values


def _task_is_related_to_unit(task: Task, unit_id: str, document_ids: set[str]) -> bool:
    payload = task.payload or {}
    if payload.get("learning_unit_id") == unit_id or payload.get("target_learning_unit_id") == unit_id:
        return True
    if task.resource_type == "learning_unit" and task.resource_id == unit_id:
        return True
    return task.resource_type == "document" and task.resource_id in document_ids

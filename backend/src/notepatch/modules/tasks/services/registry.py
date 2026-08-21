from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from notepatch.modules.ai.services.gateway import OpenClawRunner
from notepatch.modules.admin.services.operations import UserPurgeExecutor
from notepatch.modules.ai.services.task_handler import process_openclaw_chat
from notepatch.modules.documents.ocr import OcrPipeline
from notepatch.modules.documents.models.document import Document
from notepatch.modules.documents.services.doctr import DocTrClient
from notepatch.modules.documents.services.purge import DocumentPurgeService
from notepatch.modules.documents.services.task_handlers import (
    process_document_pipeline,
    process_ocr_document,
    process_scan_document,
)
from notepatch.modules.learning.services.assignment import LearningUnitAssignmentService
from notepatch.modules.learning.services.embedding import EmbeddingClient
from notepatch.modules.learning.services.task_handlers import run_learning_task
from notepatch.modules.learning.services.workflow import LearningWorkflowService
from notepatch.modules.learning.services.merge import LearningUnitMergeService
from notepatch.modules.identity.services.profile import AvatarService
from notepatch.modules.tasks.models.task import Task
from notepatch.modules.tasks.services.task import TaskService
from notepatch.modules.tasks.services.workflow import WorkflowTracker
from notepatch.platform.errors import PermanentTaskError, RetryableTaskError
from notepatch.platform.gpu_lease import GpuLeaseService
from notepatch.platform.storage import StorageService


LEARNING_TASK_TYPES = {
    "extract_questions",
    "build_knowledge_base",
    "generate_study_notes",
    "generate_flashcards",
    "grade_homework",
    "highlight_study_notes",
}

REGISTERED_TASK_TYPES = {
    "scan_document",
    "document_processing_pipeline",
    "ocr_document",
    "assign_learning_unit",
    *LEARNING_TASK_TYPES,
    "merge_learning_units",
    "purge_document",
    "purge_avatar_object",
    "purge_user",
    "openclaw_agent_run",
}


@dataclass(slots=True)
class TaskExecutionContext:
    db: Session
    tasks: TaskService
    task: Task
    storage: StorageService
    openclaw_runner: OpenClawRunner
    doctr_client: DocTrClient
    ocr_pipeline: OcrPipeline | None
    gpu_lease: GpuLeaseService
    embedding_client: EmbeddingClient
    learning: LearningWorkflowService


def execute_registered_task(context: TaskExecutionContext) -> None:
    task = context.task
    if task.task_type == "scan_document":
        process_scan_document(context.db, context.tasks, task, context.storage)
    elif task.task_type == "document_processing_pipeline":
        process_document_pipeline(
            context.db,
            context.tasks,
            task,
            context.storage,
            context.doctr_client,
            context.ocr_pipeline,
            context.gpu_lease,
            context.learning,
        )
    elif task.task_type == "ocr_document":
        process_ocr_document(
            context.db,
            context.tasks,
            task,
            context.storage,
            context.ocr_pipeline,
            context.gpu_lease,
            context.learning,
        )
    elif task.task_type == "assign_learning_unit":
        context.tasks.add_event(
            task,
            "learning_unit_assignment_started",
            "Learning unit assignment started",
            progress=75,
        )
        context.db.commit()
        document = context.db.scalar(
            select(Document).where(
                Document.workspace_id == task.workspace_id,
                Document.id == (task.payload.get("document_id") or task.resource_id),
                Document.status != "deleted",
            )
        )
        if document is None:
            raise PermanentTaskError("Document not found")
        unit, assignment, warning = LearningUnitAssignmentService(
            context.db,
            storage=context.storage,
            embedding_client=context.embedding_client,
        ).assign_after_ocr(document)
        context.db.commit()
        for run in WorkflowTracker(context.db).runs_for_task(task.id):
            run.learning_unit_id = unit.id
        if warning:
            context.tasks.add_event(
                task,
                "learning_unit_assignment_warning",
                "Semantic grouping was unavailable; a new learning unit was created",
                level="warning",
                data={"reason": warning},
            )
            context.db.commit()
        downstream = context.learning.schedule_after_assignment(
            document=document,
            learning_unit=unit,
            source_ocr_run_id=task.payload.get("source_ocr_run_id"),
            force_reprocess=bool(task.payload.get("force_reprocess")),
        )
        context.tasks.mark_succeeded(
            task,
            {
                "document_id": document.id,
                "learning_unit_id": unit.id,
                "assignment_id": assignment.id,
                "method": assignment.method,
                "confidence": assignment.confidence,
                "downstream_tasks": [
                    {"id": child.id, "task_type": child.task_type} for child in downstream
                ],
            },
        )
    elif task.task_type in LEARNING_TASK_TYPES:
        run_learning_task(context.tasks, task, context.learning, context.storage)
    elif task.task_type == "merge_learning_units":
        context.tasks.add_event(task, "merge_claimed", "Learning unit merge task claimed", progress=5)
        context.db.commit()
        result = LearningUnitMergeService(context.db, context.storage).execute(task)
        context.tasks.mark_succeeded(task, result)
    elif task.task_type == "purge_document":
        context.tasks.add_event(task, "document_purge_started", "Document purge started", progress=10)
        context.db.commit()
        result = DocumentPurgeService(context.db, context.storage).purge(task)
        context.tasks.mark_succeeded(task, result)
    elif task.task_type == "purge_avatar_object":
        context.tasks.add_event(task, "avatar_cleanup_started", "Obsolete avatar cleanup started", progress=10)
        context.db.commit()
        try:
            AvatarService(context.db, context.storage).cleanup_object(
                user_id=str(task.payload.get("user_id") or ""),
                backend=str(task.payload.get("storage_backend") or ""),
                object_key=str(task.payload.get("object_key") or ""),
            )
        except ValueError:
            raise
        except Exception as exc:
            raise RetryableTaskError("Avatar cleanup storage is unavailable") from exc
        context.tasks.mark_succeeded(task, {"object_deleted": True})
    elif task.task_type == "purge_user":
        context.tasks.add_event(task, "user_purge_started", "User purge started", progress=10)
        context.db.commit()
        result = UserPurgeExecutor(context.db, context.storage).execute(task)
        context.tasks.mark_succeeded(task, result)
    elif task.task_type == "openclaw_agent_run":
        process_openclaw_chat(
            context.db,
            context.tasks,
            task,
            context.storage,
            context.openclaw_runner,
            context.embedding_client,
        )
    else:
        raise PermanentTaskError(f"No processor is registered for task type {task.task_type}")

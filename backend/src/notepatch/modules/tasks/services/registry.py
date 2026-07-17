from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from notepatch.modules.ai.services.gateway import OpenClawRunner
from notepatch.modules.admin.services.operations import UserPurgeExecutor
from notepatch.modules.ai.services.task_handler import process_openclaw_chat
from notepatch.modules.documents.ocr import OcrPipeline
from notepatch.modules.documents.services.doctr import DocTrClient
from notepatch.modules.documents.services.purge import DocumentPurgeService
from notepatch.modules.documents.services.task_handlers import (
    process_document_pipeline,
    process_ocr_document,
)
from notepatch.modules.learning.services.embedding import EmbeddingClient
from notepatch.modules.learning.services.task_handlers import run_learning_task
from notepatch.modules.learning.services.workflow import LearningWorkflowService
from notepatch.modules.tasks.models.task import Task
from notepatch.modules.tasks.services.task import TaskService
from notepatch.platform.errors import PermanentTaskError
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
    if task.task_type == "document_processing_pipeline":
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
    elif task.task_type in LEARNING_TASK_TYPES:
        run_learning_task(context.tasks, task, context.learning, context.storage)
    elif task.task_type == "purge_document":
        context.tasks.add_event(task, "document_purge_started", "Document purge started", progress=10)
        context.db.commit()
        result = DocumentPurgeService(context.db, context.storage).purge(task)
        context.tasks.mark_succeeded(task, result)
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

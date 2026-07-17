from __future__ import annotations

from sqlalchemy import update
from sqlalchemy.orm import Session

from notepatch.modules.ai.services.chat import ChatService
from notepatch.modules.admin.models.admin import AdminOperation
from notepatch.modules.ai.services.gateway import OpenClawRunner, get_openclaw_runner
from notepatch.modules.ai.services.skill_runner import OpenClawSkillRunner
from notepatch.modules.documents.models.document import Document
from notepatch.modules.documents.ocr import OcrPipeline
from notepatch.modules.documents.services.doctr import DocTrClient, get_doctr_client
from notepatch.modules.documents.services.task_support import _task_document
from notepatch.modules.learning.services.embedding import EmbeddingClient
from notepatch.modules.learning.services.workflow import LearningWorkflowService
from notepatch.modules.tasks.models.task import Task
from notepatch.modules.tasks.services.registry import TaskExecutionContext, execute_registered_task
from notepatch.modules.tasks.services.task import TaskService
from notepatch.platform.database import utcnow
from notepatch.platform.errors import RetryableTaskError, TaskCancelledError
from notepatch.platform.gpu_lease import GpuLeaseService
from notepatch.platform.storage import StorageService


def process_task(
    db: Session,
    task_id: str,
    *,
    storage: StorageService | None = None,
    openclaw_runner: OpenClawRunner | None = None,
    doctr_client: DocTrClient | None = None,
    ocr_pipeline: OcrPipeline | None = None,
    gpu_lease: GpuLeaseService | None = None,
    skill_runner: OpenClawSkillRunner | None = None,
    embedding_client: EmbeddingClient | None = None,
) -> Task | None:
    tasks = TaskService(db)
    task = tasks.claim_task(task_id)
    if task is None:
        return db.get(Task, task_id)

    storage = storage or StorageService()
    openclaw_runner = openclaw_runner or get_openclaw_runner()
    gpu_lease = gpu_lease or GpuLeaseService()
    embedding_client = embedding_client or EmbeddingClient(gpu_lease=gpu_lease)
    skill_runner = skill_runner or OpenClawSkillRunner(
        db=db,
        storage=storage,
        gateway_runner=openclaw_runner,
    )
    learning = LearningWorkflowService(
        db,
        storage,
        skill_runner=skill_runner,
        embedding_client=embedding_client,
    )
    context = TaskExecutionContext(
        db=db,
        tasks=tasks,
        task=task,
        storage=storage,
        openclaw_runner=openclaw_runner,
        doctr_client=doctr_client or get_doctr_client(),
        ocr_pipeline=ocr_pipeline or OcrPipeline(),
        gpu_lease=gpu_lease,
        embedding_client=embedding_client,
        learning=learning,
    )
    try:
        if task.task_type == "openclaw_agent_run":
            ChatService(db).mark_assistant_running(task)
        execute_registered_task(context)
    except TaskCancelledError as exc:
        db.rollback()
        task = db.get(Task, task_id)
        if task is not None:
            if task.task_type == "grade_homework":
                learning.cleanup_cancelled_grading(task, storage)
            TaskService(db).mark_cancelled(task, str(exc))
    except Exception as exc:
        _handle_task_failure(db, task_id, learning, storage, exc)
    return db.get(Task, task_id)


def _handle_task_failure(
    db: Session,
    task_id: str,
    learning: LearningWorkflowService,
    storage: StorageService,
    exc: Exception,
) -> None:
    db.rollback()
    task = db.get(Task, task_id)
    if task is None:
        return
    tasks = TaskService(db)
    if isinstance(exc, RetryableTaskError) and tasks.schedule_retry(task, str(exc)):
        if task.task_type == "purge_document":
            document = _task_document(db, task)
            if document is not None:
                document.purge_status = "queued"
        elif task.task_type == "purge_user":
            operation = db.get(AdminOperation, task.payload.get("admin_operation_id"))
            if operation is not None:
                operation.status = "queued"
                operation.error_message = str(exc)
        ChatService(db).mark_assistant_queued(task)
        db.commit()
        return

    if task.task_type in {"document_processing_pipeline", "ocr_document"}:
        document = _task_document(db, task)
        if document is not None and document.status != "deleted":
            db.execute(
                update(Document)
                .where(
                    Document.workspace_id == task.workspace_id,
                    Document.id == document.id,
                    Document.status != "deleted",
                )
                .values(status="failed", updated_at=utcnow())
            )
    elif task.task_type == "purge_document":
        document = _task_document(db, task)
        if document is not None:
            document.purge_status = "failed"
    elif task.task_type == "purge_user":
        operation = db.get(AdminOperation, task.payload.get("admin_operation_id"))
        if operation is not None:
            operation.status = "failed"
            operation.error_message = str(exc)
            operation.finished_at = utcnow()
    ChatService(db).mark_assistant_failed(task, str(exc))
    tasks.mark_failed(task, str(exc))


__all__ = ["process_task"]

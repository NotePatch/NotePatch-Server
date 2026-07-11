from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from notepatch.modules.documents.models.document import Document, DocumentArtifact
from notepatch.modules.tasks.models.task import Task
from notepatch.modules.tasks.services.task import TaskService
from notepatch.platform.config import get_settings
from notepatch.platform.database import utcnow
from notepatch.platform.errors import PermanentTaskError, TaskCancelledError
from notepatch.platform.storage import StorageService


OCR_ARTIFACT_TYPES = (
    "ocr_json",
    "ocr_markdown",
    "ocr_text",
    "layout_json",
    "formula_json",
    "tables_json",
)


def _task_document(db: Session, task: Task) -> Document | None:
    document_id = task.payload.get("document_id") or task.resource_id
    return db.scalar(
        select(Document).where(Document.workspace_id == task.workspace_id, Document.id == document_id)
    )


def _required_task_document(db: Session, task: Task) -> Document:
    document = _task_document(db, task)
    if document is None or document.status == "deleted":
        raise PermanentTaskError("Document not found")
    return document


def _set_document_status(
    db: Session,
    tasks: TaskService,
    task: Task,
    document: Document,
    status_value: str,
) -> None:
    tasks.ensure_active(task)
    changed = db.execute(
        update(Document)
        .where(
            Document.workspace_id == task.workspace_id,
            Document.id == document.id,
            Document.status != "deleted",
        )
        .values(status=status_value, updated_at=utcnow())
    )
    if changed.rowcount != 1:
        db.rollback()
        raise TaskCancelledError("Document was deleted while processing")
    db.commit()
    db.refresh(document)


def _force_reprocess(task: Task) -> bool:
    options = task.payload.get("options") if isinstance(task.payload.get("options"), dict) else {}
    return bool(task.payload.get("force_reprocess") or options.get("force_reprocess"))


def _auto_learning_enabled(task: Task) -> bool:
    options = task.payload.get("options") if isinstance(task.payload.get("options"), dict) else {}
    return get_settings().auto_learning_pipeline and options.get("auto_learning") is not False


def _document_extension(document: Document) -> str:
    suffix = Path(document.original_filename or "").suffix.lower().lstrip(".")
    return suffix or ("pdf" if document.file_type == "pdf" else "img" if document.file_type == "image" else "bin")


def _latest_artifact(
    db: Session,
    workspace_id: str,
    document_id: str,
    artifact_type: str,
) -> DocumentArtifact | None:
    return db.scalar(
        select(DocumentArtifact)
        .where(
            DocumentArtifact.workspace_id == workspace_id,
            DocumentArtifact.document_id == document_id,
            DocumentArtifact.artifact_type == artifact_type,
        )
        .order_by(DocumentArtifact.created_at.desc())
    )


def _complete_ocr_artifacts(
    db: Session,
    workspace_id: str,
    document_id: str,
) -> dict[str, DocumentArtifact] | None:
    rows = db.scalars(
        select(DocumentArtifact)
        .where(
            DocumentArtifact.workspace_id == workspace_id,
            DocumentArtifact.document_id == document_id,
            DocumentArtifact.artifact_type.in_(OCR_ARTIFACT_TYPES),
        )
        .order_by(DocumentArtifact.created_at.desc())
    ).all()
    groups: dict[str, dict[str, DocumentArtifact]] = {}
    for artifact in rows:
        run_id = (artifact.metadata_ or {}).get("ocr_run_id")
        if run_id:
            groups.setdefault(str(run_id), {}).setdefault(artifact.artifact_type, artifact)
    complete = [group for group in groups.values() if all(name in group for name in OCR_ARTIFACT_TYPES)]
    if not complete:
        return None
    return max(complete, key=lambda group: max(item.created_at for item in group.values()))


def _load_ocr_json(storage: StorageService, artifacts: dict[str, DocumentArtifact], workdir: Path) -> dict:
    artifact = artifacts["ocr_json"]
    path = workdir / "existing-ocr.json"
    storage.download_file(artifact.bucket, artifact.object_key, path)
    return json.loads(path.read_text(encoding="utf-8"))


def _progress(
    db: Session,
    tasks: TaskService,
    task: Task,
    event_type: str,
    message: str,
    progress: int,
    data: dict | None = None,
) -> None:
    tasks.add_event(task, event_type, message, progress=progress, data=data or {})
    db.commit()


def _warning(db: Session, tasks: TaskService, task: Task, reason: str, error: str) -> None:
    tasks.add_event(
        task,
        "warning",
        "Pipeline continued with an allowed degradation",
        level="warning",
        data={"reason": reason, "error": error},
    )
    db.commit()


def _gpu_event(db: Session, tasks: TaskService, task: Task, event: str, data: dict) -> None:
    messages = {
        "gpu_lease_waiting": "Waiting for shared GPU lease",
        "gpu_lease_acquired": "Shared GPU lease acquired",
        "gpu_lease_released": "Shared GPU lease released",
        "gpu_lease_timeout": "Timed out waiting for shared GPU lease",
    }
    tasks.add_event(
        task,
        event,
        messages.get(event, event.replace("_", " ").title()),
        level="error" if event.endswith("timeout") else "info",
        data=data,
    )
    db.commit()


def _release_paddle_gpu_cache() -> None:
    try:
        import paddle

        if paddle.device.is_compiled_with_cuda():
            paddle.device.cuda.empty_cache()
    except Exception:
        return

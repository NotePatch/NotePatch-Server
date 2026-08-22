from __future__ import annotations

import re
import tempfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from notepatch.modules.ai.services.gateway import OpenClawRunner
from notepatch.modules.ai.services.locale import normalize_client_locale
from notepatch.modules.ai.services.runtime import OpenClawUserRuntimeService
from notepatch.modules.documents.models.document import Document, DocumentArtifact
from notepatch.modules.identity.models.user import User
from notepatch.modules.tasks.models.task import Task
from notepatch.modules.tasks.services.task import TaskService
from notepatch.platform.config import get_settings
from notepatch.platform.database import utcnow
from notepatch.platform.errors import PermanentTaskError, RetryableTaskError
from notepatch.platform.storage import StorageService


def schedule_image_remark(
    db: Session,
    tasks: TaskService,
    document: Document,
    *,
    ocr_text_artifact: DocumentArtifact,
) -> Task | None:
    """Queue an OCR-based remark unless the user has supplied one."""
    settings = get_settings()
    if (
        not settings.ai_image_remark_enabled
        or document.file_type != "image"
        or document.status in {"created", "uploading", "scanning", "failed", "deleted"}
        or document.remark_source == "user"
    ):
        return None
    user = db.scalar(select(User).where(User.id == document.uploaded_by))
    if user is None or not user.auto_image_remark_enabled:
        _use_original_filename(document, status="disabled")
        db.commit()
        return None
    if ocr_text_artifact.workspace_id != document.workspace_id or ocr_text_artifact.document_id != document.id:
        raise ValueError("OCR artifact does not belong to the image document")

    active = tasks.find_active_task(
        workspace_id=document.workspace_id,
        task_type="generate_image_remark",
        resource_type="document",
        resource_id=document.id,
    )
    if active is not None:
        _update_remark_state(document, status=active.status, task_id=active.id)
        db.commit()
        return active

    task, queue_name = tasks.create_task_record(
        workspace_id=document.workspace_id,
        task_type="generate_image_remark",
        resource_type="document",
        resource_id=document.id,
        payload={
            "document_id": document.id,
            "ocr_text_artifact_id": ocr_text_artifact.id,
            "ocr_run_id": (ocr_text_artifact.metadata_ or {}).get("ocr_run_id"),
            "ai_model": settings.ai_image_remark_model,
            "thinking_effort": "minimal",
            **(
                {"client_locale": client_locale}
                if (client_locale := normalize_client_locale(_remark_state(document).get("client_locale")))
                else {}
            ),
        },
    )
    _update_remark_state(
        document,
        status="queued",
        task_id=task.id,
        model=settings.ai_image_remark_model,
        ocr_text_artifact_id=ocr_text_artifact.id,
        error=None,
    )
    db.commit()
    db.refresh(task)
    if not tasks.enqueue_task(task.id, queue_name=queue_name):
        _update_remark_state(
            document,
            status="failed",
            task_id=task.id,
            error="Image remark task queue is unavailable",
        )
        db.commit()
    return task


def schedule_image_remark_ocr(db: Session, tasks: TaskService, document: Document) -> Task | None:
    """Ensure images without a learning pipeline still receive OCR for auto remarks."""
    state = _remark_state(document)
    if (
        document.file_type != "image"
        or document.remark_source == "user"
        or state.get("status") != "waiting_ocr"
    ):
        return None
    for task_type in ("document_processing_pipeline", "ocr_document"):
        active = tasks.find_active_task(
            workspace_id=document.workspace_id,
            task_type=task_type,
            resource_type="document",
            resource_id=document.id,
        )
        if active is not None:
            return active
    task, queue_name = tasks.create_task_record(
        workspace_id=document.workspace_id,
        task_type="ocr_document",
        resource_type="document",
        resource_id=document.id,
        payload={
            "document_id": document.id,
            "options": {"auto_learning": False},
            "purpose": "image_remark",
        },
    )
    db.commit()
    db.refresh(task)
    tasks.enqueue_task(task.id, queue_name=queue_name)
    return task


def process_image_remark(
    db: Session,
    tasks: TaskService,
    task: Task,
    storage: StorageService,
    runner: OpenClawRunner,
) -> None:
    settings = get_settings()
    tasks.ensure_active(task)
    document = db.scalar(
        select(Document).where(
            Document.workspace_id == task.workspace_id,
            Document.id == (task.payload.get("document_id") or task.resource_id),
            Document.status != "deleted",
        )
    )
    if document is None:
        raise PermanentTaskError("Image remark document not found")
    if document.file_type != "image":
        raise PermanentTaskError("Image remarks only support image documents")
    if document.remark_source == "user":
        tasks.mark_cancelled(task, "A user-provided remark superseded AI generation")
        return

    artifact_id = task.payload.get("ocr_text_artifact_id")
    artifact = db.scalar(
        select(DocumentArtifact).where(
            DocumentArtifact.workspace_id == task.workspace_id,
            DocumentArtifact.document_id == document.id,
            DocumentArtifact.id == artifact_id,
            DocumentArtifact.artifact_type == "ocr_text",
        )
    )
    if artifact is None:
        raise PermanentTaskError("OCR text artifact for image remark is unavailable")

    model = str(task.payload.get("ai_model") or settings.ai_image_remark_model)
    language_payload = dict(task.payload or {})
    if "client_locale" not in language_payload:
        client_locale = normalize_client_locale(_remark_state(document).get("client_locale"))
        if client_locale:
            language_payload["client_locale"] = client_locale
    output_language = resolve_image_remark_language(language_payload)
    _update_remark_state(document, status="running", task_id=task.id, model=model, error=None)
    tasks.add_event(
        task,
        "image_remark_started",
        "OCR-based image remark generation started",
        progress=10,
        data={
            "document_id": document.id,
            "ocr_text_artifact_id": artifact.id,
            "provider_model": model,
            "thinking_effort": "minimal",
            "output_language": output_language,
        },
    )
    db.commit()

    with tempfile.TemporaryDirectory(prefix=f"notepatch-image-remark-{task.id}-") as tmpdir:
        source = Path(tmpdir) / "ocr.txt"
        try:
            storage.download_file(artifact.bucket, artifact.object_key, source)
        except Exception as exc:
            if StorageService.is_object_not_found_error(exc):
                raise PermanentTaskError("OCR text object for image remark is missing") from exc
            raise RetryableTaskError("Could not download OCR text for image remark") from exc
        ocr_text = source.read_text(encoding="utf-8", errors="replace").strip()

    if not ocr_text:
        _use_original_filename(document, status="empty_ocr", task_id=task.id)
        tasks.add_event(
            task,
            "image_remark_fallback",
            "OCR contained no text; original filename retained as the remark",
            progress=95,
            data={"document_id": document.id, "reason": "empty_ocr"},
        )
        db.commit()
        tasks.mark_succeeded(
            task,
            {"document_id": document.id, "remark": document.remark, "source": "original_filename"},
        )
        return

    runtime = OpenClawUserRuntimeService().runtime_for_workspace(
        db,
        task.workspace_id,
        model_ids=(model,),
    )
    tasks.ensure_active(task)
    generated = runner.generate_image_remark(
        task.workspace_id,
        document.id,
        ocr_text[:20_000],
        original_filename=document.original_filename,
        runtime=runtime,
        provider_model=model,
        output_language=output_language,
        max_length=settings.ai_image_remark_max_length,
        timeout_seconds=settings.ai_image_remark_timeout_seconds,
    )
    remark = normalize_image_remark(generated, max_length=settings.ai_image_remark_max_length)
    if not remark:
        raise RetryableTaskError("Image remark model returned no usable text")

    tasks.ensure_active(task)
    document = db.scalar(
        select(Document).where(
            Document.workspace_id == task.workspace_id,
            Document.id == document.id,
            Document.status != "deleted",
        ).with_for_update()
    )
    if document is None:
        raise PermanentTaskError("Image remark document was deleted")
    if document.remark_source == "user":
        tasks.mark_cancelled(task, "A user-provided remark superseded AI generation")
        return
    document.remark = remark
    document.remark_source = "ai_ocr"
    _update_remark_state(
        document,
        status="succeeded",
        task_id=task.id,
        model=model,
        remark=remark,
        source_variant="ocr_text",
        ocr_text_artifact_id=artifact.id,
        generated_at=utcnow().isoformat(),
        error=None,
    )
    tasks.add_event(
        task,
        "image_remark_succeeded",
        "OCR-based image remark generated",
        progress=95,
        data={
            "document_id": document.id,
            "remark": remark,
            "provider_model": model,
            "thinking_effort": "minimal",
            "output_language": output_language,
            "source_variant": "ocr_text",
            "ocr_text_artifact_id": artifact.id,
        },
    )
    db.commit()
    tasks.mark_succeeded(
        task,
        {
            "document_id": document.id,
            "remark": remark,
            "provider_model": model,
            "gateway_model": settings.openclaw_gateway_model,
            "thinking_effort": "minimal",
            "output_language": output_language,
            "source_variant": "ocr_text",
            "ocr_text_artifact_id": artifact.id,
        },
    )


def mark_image_remark_failure(db: Session, task: Task, message: str, *, retrying: bool) -> None:
    document = db.scalar(
        select(Document).where(
            Document.workspace_id == task.workspace_id,
            Document.id == (task.payload.get("document_id") or task.resource_id),
            Document.status != "deleted",
        )
    )
    if document is None or document.remark_source == "user":
        return
    _update_remark_state(
        document,
        status="queued" if retrying else "failed",
        task_id=task.id,
        model=str(task.payload.get("ai_model") or get_settings().ai_image_remark_model),
        error=str(message)[:500],
    )


def normalize_image_remark(value: object, *, max_length: int) -> str | None:
    if not isinstance(value, str):
        return None
    first_line = next((line.strip() for line in value.splitlines() if line.strip()), "")
    normalized = re.sub(r"^[#>*\-\s]+", "", first_line)
    normalized = normalized.strip().strip(chr(96) + chr(39) + chr(34))
    normalized = re.sub(r"\s+", " ", normalized).rstrip(".!?;:。！？；：")
    normalized = re.sub(
        r"^(?:备注|主题|标签|remark|subject|label)\s*[:：]\s*",
        "",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(r"(?:课堂|学习|复习)?(?:笔记|内容|概述|总结)$", "", normalized).strip()
    normalized = re.sub(
        r"\s+(?:(?:class|study|review)\s+)?(?:notes?|overview|summary|content)$",
        "",
        normalized,
        flags=re.IGNORECASE,
    ).strip()

    ascii_words = re.findall(r"[A-Za-z0-9][A-Za-z0-9+#.-]*", normalized)
    if len(normalized) > max_length or len(ascii_words) > 4:
        first_topic = re.split(r"[,，;；、|]|\s+(?:and|with|&)\s+", normalized, maxsplit=1, flags=re.IGNORECASE)[0]
        if first_topic.strip():
            normalized = first_topic.strip()

    words = normalized.split()
    if len(words) > 4:
        normalized = " ".join(words[:4])
    if len(normalized) > max_length:
        shortened = normalized[:max_length].rstrip()
        if " " in shortened and max_length < len(normalized) and normalized[max_length] != " ":
            shortened = shortened.rsplit(" ", 1)[0]
        normalized = shortened.rstrip(" ,，、;；.!?：:")
    return normalized or None


def resolve_image_remark_language(payload: object) -> str:
    if not isinstance(payload, dict):
        return "ocr"
    snapshot = payload.get("ai_preferences")
    answers = snapshot.get("answers") if isinstance(snapshot, dict) else None
    preference = answers.get("response_language") if isinstance(answers, dict) else None
    if preference in {"zh-CN", "en-US", "pt-BR"}:
        return preference
    if preference == "client_locale":
        return normalize_client_locale(payload.get("client_locale")) or "ocr"
    return "ocr"


def _remark_state(document: Document) -> dict:
    value = (document.metadata_ or {}).get("image_remark_generation")
    return dict(value) if isinstance(value, dict) else {}


def _update_remark_state(document: Document, **updates) -> None:
    metadata = dict(document.metadata_ or {})
    state = _remark_state(document)
    for key, value in updates.items():
        if value is None:
            state.pop(key, None)
        else:
            state[key] = value
    metadata.pop("ai_image_naming", None)
    metadata["image_remark_generation"] = state
    document.metadata_ = metadata


def _use_original_filename(document: Document, *, status: str, task_id: str | None = None) -> None:
    if document.remark_source != "user":
        document.remark = document.original_filename
        document.remark_source = "original_filename"
    _update_remark_state(document, status=status, task_id=task_id, error=None)


__all__ = [
    "mark_image_remark_failure",
    "normalize_image_remark",
    "process_image_remark",
    "resolve_image_remark_language",
    "schedule_image_remark",
    "schedule_image_remark_ocr",
]

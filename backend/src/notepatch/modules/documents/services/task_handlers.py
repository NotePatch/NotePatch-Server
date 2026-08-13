from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from notepatch.modules.documents.models.document import Document, DocumentArtifact
from notepatch.modules.documents.ocr import OcrPipeline
from notepatch.modules.documents.services.doctr import DocTrClient
from notepatch.modules.documents.services.converter import DocumentConverterClient
from notepatch.modules.documents.services.task_support import (
    _auto_learning_enabled,
    _complete_ocr_artifacts,
    _document_extension,
    _force_reprocess,
    _gpu_event,
    _latest_artifact,
    _load_ocr_json,
    _progress,
    _release_paddle_gpu_cache,
    _required_task_document,
    _set_document_status,
    _warning,
)
from notepatch.modules.learning.services.workflow import LearningWorkflowService
from notepatch.modules.tasks.models.task import Task
from notepatch.modules.tasks.services.task import TaskService
from notepatch.platform.config import get_settings
from notepatch.platform.errors import RetryableTaskError
from notepatch.platform.gpu_lease import GpuLeaseService
from notepatch.platform.storage import StorageService


def process_document_pipeline(
    db: Session,
    tasks: TaskService,
    task: Task,
    storage: StorageService,
    doctr_client: DocTrClient,
    ocr_pipeline: OcrPipeline | None,
    gpu_lease: GpuLeaseService,
    learning: LearningWorkflowService,
) -> None:
    tasks.ensure_active(task)
    document = _required_task_document(db, task)
    _set_document_status(db, tasks, task, document, "processing")
    force = _force_reprocess(task)
    artifacts = None if force else _complete_ocr_artifacts(db, task.workspace_id, document.id)
    deskewed_key = None
    with tempfile.TemporaryDirectory(prefix=f"notepatch-pipeline-{task.id}-") as tmpdir:
        workdir = Path(tmpdir)
        if artifacts is not None:
            try:
                _load_ocr_json(storage, artifacts, workdir)
                _progress(db, tasks, task, "ocr_reused", "Existing OCR artifacts reused", 70)
            except Exception as exc:
                if not StorageService.is_object_not_found_error(exc):
                    raise
                artifacts = None
                _warning(db, tasks, task, "ocr_metadata_stale", str(exc))
        if artifacts is None:
            original = workdir / f"original.{_document_extension(document)}"
            storage.download_file(document.bucket, document.object_key, original)
            source_path = original
            source_artifact = None
            if document.file_type in {"docx", "pptx"}:
                source_artifact = _prepare_converted_pdf(db, tasks, task, document, storage, original, workdir, force)
                source_path = workdir / "converted.pdf"
                storage.download_file(source_artifact.bucket, source_artifact.object_key, source_path)
            elif document.file_type == "image" and get_settings().doctr_enabled:
                try:
                    source_artifact = _doctr_preprocess(
                        db, tasks, task, document, storage, doctr_client, gpu_lease
                    )
                    deskewed_key = source_artifact.object_key
                    source_path = workdir / "deskewed_image.png"
                    storage.download_file(source_artifact.bucket, source_artifact.object_key, source_path)
                except Exception as exc:
                    # DocTr is an enhancement. A real OCR run on the original image is an allowed degradation.
                    _warning(db, tasks, task, "doctr_degraded_to_original", str(exc))
                    source_artifact = None
                    source_path = original
            _, artifacts = _run_and_store_ocr(
                db,
                tasks,
                task,
                document,
                storage,
                source_path,
                source_artifact,
                workdir,
                ocr_pipeline,
                gpu_lease,
            )
    downstream = []
    tasks.ensure_active(task)
    if _auto_learning_enabled(task):
        downstream = learning.schedule_after_ocr(
            document=document,
            ocr_artifacts=artifacts,
            force_reprocess=force,
        )
    _set_document_status(db, tasks, task, document, "ready")
    tasks.mark_succeeded(
        task,
        {
            "document_id": document.id,
            "deskewed_object_key": deskewed_key,
            "ocr_artifacts": {name: artifact.id for name, artifact in artifacts.items()},
            "downstream_tasks": [{"id": item.id, "task_type": item.task_type} for item in downstream],
        },
    )


def process_ocr_document(
    db: Session,
    tasks: TaskService,
    task: Task,
    storage: StorageService,
    ocr_pipeline: OcrPipeline | None,
    gpu_lease: GpuLeaseService,
    learning: LearningWorkflowService,
) -> None:
    tasks.ensure_active(task)
    document = _required_task_document(db, task)
    _set_document_status(db, tasks, task, document, "processing")
    force = _force_reprocess(task)
    artifacts = None if force else _complete_ocr_artifacts(db, task.workspace_id, document.id)
    with tempfile.TemporaryDirectory(prefix=f"notepatch-ocr-{task.id}-") as tmpdir:
        workdir = Path(tmpdir)
        if artifacts is not None:
            try:
                _load_ocr_json(storage, artifacts, workdir)
                _progress(db, tasks, task, "ocr_reused", "Existing OCR artifacts reused", 90)
            except Exception as exc:
                if not StorageService.is_object_not_found_error(exc):
                    raise
                artifacts = None
                _warning(db, tasks, task, "ocr_metadata_stale", str(exc))
        if artifacts is None:
            source_artifact = None
            if document.file_type in {"docx", "pptx"}:
                original = workdir / f"original.{_document_extension(document)}"
                storage.download_file(document.bucket, document.object_key, original)
                source_artifact = _prepare_converted_pdf(db, tasks, task, document, storage, original, workdir, force)
                source = workdir / "converted.pdf"
                storage.download_file(source_artifact.bucket, source_artifact.object_key, source)
            else:
                source_artifact = _latest_artifact(db, task.workspace_id, document.id, "deskewed_image")
                if source_artifact is not None:
                    source = workdir / "deskewed_image.png"
                    try:
                        storage.download_file(source_artifact.bucket, source_artifact.object_key, source)
                    except Exception as exc:
                        if not StorageService.is_object_not_found_error(exc):
                            raise
                        source_artifact = None
                        _warning(db, tasks, task, "deskewed_object_missing", str(exc))
                if source_artifact is None:
                    source = workdir / f"original.{_document_extension(document)}"
                    storage.download_file(document.bucket, document.object_key, source)
            _, artifacts = _run_and_store_ocr(
                db,
                tasks,
                task,
                document,
                storage,
                source,
                source_artifact,
                workdir,
                ocr_pipeline,
                gpu_lease,
            )
    downstream = []
    tasks.ensure_active(task)
    if _auto_learning_enabled(task):
        downstream = learning.schedule_after_ocr(
            document=document,
            ocr_artifacts=artifacts,
            force_reprocess=force,
        )
    _set_document_status(db, tasks, task, document, "ready")
    tasks.mark_succeeded(
        task,
        {
            "document_id": document.id,
            "ocr_artifacts": {name: artifact.id for name, artifact in artifacts.items()},
            "downstream_tasks": [{"id": item.id, "task_type": item.task_type} for item in downstream],
        },
    )


def _run_and_store_ocr(
    db: Session,
    tasks: TaskService,
    task: Task,
    document: Document,
    storage: StorageService,
    input_path: Path,
    source_artifact: DocumentArtifact | None,
    workdir: Path,
    pipeline: OcrPipeline | None,
    gpu_lease: GpuLeaseService,
) -> tuple[dict, dict[str, DocumentArtifact]]:
    pipeline = pipeline or OcrPipeline()
    pages = 1

    def ocr_event(event_type: str, message: str, data: dict) -> None:
        nonlocal pages
        progress = None
        if event_type == "ocr_started":
            progress = 50
        elif event_type == "ocr_rendered":
            pages = max(int(data.get("pages") or 1), 1)
            progress = 55
        elif event_type == "ocr_page_completed":
            progress = min(68, 56 + int(((int(data.get("page_index") or 0) + 1) / pages) * 12))
        tasks.add_event(task, event_type, message, progress=progress, data=data)
        db.commit()

    source = {
        "filename": document.original_filename,
        "mime_type": document.mime_type,
        "file_size": document.file_size,
        "bucket": document.bucket,
        "object_key": document.object_key,
        "ocr_input": {
            "kind": source_artifact.artifact_type if source_artifact else "original",
            "artifact_id": source_artifact.id if source_artifact else None,
            "object_key": source_artifact.object_key if source_artifact else document.object_key,
        },
    }
    with gpu_lease.lease(
        owner=f"task:{task.id}:ocr",
        event_callback=lambda event, data: _gpu_event(db, tasks, task, event, data),
    ):
        result = pipeline.run(
            input_path=input_path,
            document_id=document.id,
            workspace_id=task.workspace_id,
            source=source,
            mime_type=source_artifact.mime_type if source_artifact else document.mime_type,
            file_type=(
                "image" if source_artifact and source_artifact.artifact_type == "deskewed_image"
                else "pdf" if source_artifact and source_artifact.artifact_type == "converted_pdf"
                else document.file_type
            ),
            event_callback=ocr_event,
        )
    tasks.ensure_active(task)
    _release_paddle_gpu_cache()
    output = pipeline.write_outputs(result, workdir / "ocr-output")
    run_id = str(uuid.uuid4())
    specs = {
        "ocr_json": ("json", "application/json", output["json"]),
        "ocr_markdown": ("md", "text/markdown; charset=utf-8", output["markdown"]),
        "ocr_text": ("txt", "text/plain; charset=utf-8", output["text"]),
        "layout_json": ("json", "application/json", output["layout"]),
        "formula_json": ("json", "application/json", output["formula"]),
        "tables_json": ("json", "application/json", output["tables"]),
    }
    created = {}
    for artifact_type, (extension, mime_type, path) in specs.items():
        tasks.ensure_active(task)
        artifact_id = str(uuid.uuid4())
        key = storage.document_artifact_key(
            task.workspace_id, document.id, artifact_id, artifact_type, extension
        )
        metadata = {
            "processor": "paddleocr",
            "task_id": task.id,
            "ocr_run_id": run_id,
            "engine": result.engine,
            "source_artifact_id": source_artifact.id if source_artifact else None,
        }
        storage.put_file(document.bucket, key, path, content_type=mime_type, metadata=metadata)
        tasks.ensure_active(task)
        artifact = DocumentArtifact(
            id=artifact_id,
            workspace_id=task.workspace_id,
            document_id=document.id,
            artifact_type=artifact_type,
            bucket=document.bucket,
            object_key=key,
            mime_type=mime_type,
            file_size=path.stat().st_size,
            metadata_=metadata,
        )
        db.add(artifact)
        created[artifact_type] = artifact
    db.commit()
    _progress(
        db,
        tasks,
        task,
        "ocr_artifacts_uploaded",
        "OCR, layout, formula, and table artifacts uploaded",
        70,
        {name: artifact.id for name, artifact in created.items()},
    )
    return result.to_dict(), created


def _doctr_preprocess(
    db: Session,
    tasks: TaskService,
    task: Task,
    document: Document,
    storage: StorageService,
    client: DocTrClient,
    gpu_lease: GpuLeaseService,
) -> DocumentArtifact:
    _progress(db, tasks, task, "doctr_health", "DocTr health check started", 10)
    health = client.health()
    tasks.ensure_active(task)
    if not health.get("weights_ready", False):
        raise RetryableTaskError("DocTr model weights are not ready")
    with tempfile.TemporaryDirectory(prefix=f"notepatch-doctr-{task.id}-") as tmpdir:
        workdir = Path(tmpdir)
        source = workdir / f"original{Path(document.original_filename).suffix or '.img'}"
        output = workdir / "deskewed_image.png"
        storage.download_file(document.bucket, document.object_key, source)
        _progress(db, tasks, task, "doctr_running", "DocTr image rectification running", 30)
        with gpu_lease.lease(
            owner=f"task:{task.id}:doctr",
            event_callback=lambda event, data: _gpu_event(db, tasks, task, event, data),
        ):
            client.rectify_image(
                source,
                output,
                filename=document.original_filename,
                content_type=document.mime_type,
                ill_rec=get_settings().doctr_ill_rec,
            )
        tasks.ensure_active(task)
        artifact_id = str(uuid.uuid4())
        key = storage.document_artifact_key(
            task.workspace_id, document.id, artifact_id, "deskewed_image", "png"
        )
        storage.put_file(
            document.bucket,
            key,
            output,
            content_type="image/png",
            metadata={"processor": "doctr", "task_id": task.id},
        )
        tasks.ensure_active(task)
        artifact = DocumentArtifact(
            id=artifact_id,
            workspace_id=task.workspace_id,
            document_id=document.id,
            artifact_type="deskewed_image",
            bucket=document.bucket,
            object_key=key,
            mime_type="image/png",
            file_size=output.stat().st_size,
            metadata_={"processor": "doctr", "task_id": task.id, "health": health},
        )
        db.add(artifact)
        db.commit()
    _progress(db, tasks, task, "doctr_succeeded", "DocTr image rectification completed", 45)
    return artifact



def _prepare_converted_pdf(
    db: Session,
    tasks: TaskService,
    task: Task,
    document: Document,
    storage: StorageService,
    original_path: Path,
    workdir: Path,
    force: bool,
) -> DocumentArtifact:
    existing = None if force else _latest_artifact(
        db, task.workspace_id, document.id, "converted_pdf"
    )
    if existing is not None and storage.object_exists(existing.bucket, existing.object_key):
        tasks.add_event(
            task, "conversion_reused", "Existing converted PDF reused", progress=20,
            data={"artifact_id": existing.id},
        )
        db.commit()
        return existing
    tasks.add_event(task, "conversion_started", "Office document conversion started", progress=15)
    db.commit()
    output = workdir / "converted-output.pdf"
    DocumentConverterClient().convert_to_pdf(
        original_path,
        output,
        filename=document.original_filename,
        mime_type=document.mime_type,
    )
    tasks.ensure_active(task)
    artifact_id = str(uuid.uuid4())
    key = storage.document_artifact_key(
        task.workspace_id, document.id, artifact_id, "converted_pdf", "pdf"
    )
    metadata = {"processor": "libreoffice", "task_id": task.id, "source_document_id": document.id}
    storage.put_file(document.bucket, key, output, content_type="application/pdf", metadata=metadata)
    tasks.ensure_active(task)
    artifact = DocumentArtifact(
        id=artifact_id,
        workspace_id=task.workspace_id,
        document_id=document.id,
        artifact_type="converted_pdf",
        bucket=document.bucket,
        object_key=key,
        mime_type="application/pdf",
        file_size=output.stat().st_size,
        metadata_=metadata,
    )
    db.add(artifact)
    tasks.add_event(
        task, "conversion_succeeded", "Office document converted to PDF", progress=30,
        data={"artifact_id": artifact.id},
    )
    db.commit()
    return artifact

def process_scan_document(
    db: Session,
    tasks: TaskService,
    task: Task,
    storage: StorageService,
) -> None:
    from sqlalchemy import delete, select

    from notepatch.modules.documents.services.scanner import (
        DocumentScanError,
        DocumentScanner,
        MalwareDetectedError,
    )
    from notepatch.platform.database import utcnow
    from notepatch.platform.errors import PermanentTaskError
    from notepatch.platform.metrics import SCAN_RESULTS

    tasks.ensure_active(task)
    document = _required_task_document(db, task)
    if document.scan_status == "clean" and document.sha256 and document.detected_mime_type:
        tasks.mark_succeeded(task, {"document_id": document.id, "reused": True})
        return
    tasks.add_event(task, "scan_started", "Document security scan started", progress=10)
    db.commit()
    with tempfile.TemporaryDirectory(prefix=f"notepatch-scan-{task.id}-") as tmpdir:
        source = Path(tmpdir) / "upload.bin"
        try:
            storage.download_file(document.bucket, document.object_key, source)
            result = DocumentScanner().scan(source, document.mime_type)
        except Exception as exc:
            scan_status = "infected" if isinstance(exc, MalwareDetectedError) else "failed"
            SCAN_RESULTS.labels(scan_status).inc()
            document.status = "failed"
            document.scan_status = scan_status
            document.scan_message = str(exc)[:500]
            document.scanned_at = utcnow()
            try:
                storage.delete_object(document.bucket, document.object_key)
            except Exception:
                pass
            db.execute(
                delete(DocumentArtifact).where(
                    DocumentArtifact.workspace_id == task.workspace_id,
                    DocumentArtifact.document_id == document.id,
                    DocumentArtifact.artifact_type == "original",
                )
            )
            tasks.add_event(
                task,
                "scan_rejected",
                "Document failed security validation",
                level="error",
                progress=100,
                data={"scan_status": scan_status, "reason": str(exc)[:500]},
            )
            db.commit()
            raise PermanentTaskError(str(exc)) from exc

    SCAN_RESULTS.labels("clean").inc()
    document.sha256 = result.sha256
    document.detected_mime_type = result.detected_mime_type
    document.mime_type = result.detected_mime_type
    document.file_size = result.file_size
    document.scan_status = "clean"
    document.scan_message = None
    document.scanned_at = utcnow()
    document.status = "uploaded"
    artifact = db.scalar(
        select(DocumentArtifact).where(
            DocumentArtifact.workspace_id == task.workspace_id,
            DocumentArtifact.document_id == document.id,
            DocumentArtifact.artifact_type == "original",
        )
    )
    if artifact is not None:
        artifact.mime_type = result.detected_mime_type
        artifact.file_size = result.file_size
        artifact.metadata_ = {
            **(artifact.metadata_ or {}),
            "sha256": result.sha256,
            "scan_status": "clean",
            "scanner": "clamav" if get_settings().clamav_enabled else "disabled",
        }
    tasks.add_event(
        task,
        "scan_succeeded",
        "Document security scan completed",
        progress=90,
        data={
            "sha256": result.sha256,
            "detected_mime_type": result.detected_mime_type,
            "file_size": result.file_size,
        },
    )
    db.commit()
    downstream = None
    if get_settings().auto_learning_pipeline:
        downstream = LearningWorkflowService(db, storage).schedule_after_upload(document)
    tasks.mark_succeeded(
        task,
        {
            "document_id": document.id,
            "scan_status": "clean",
            "sha256": result.sha256,
            "detected_mime_type": result.detected_mime_type,
            "downstream_task_id": downstream.id if downstream else None,
        },
    )

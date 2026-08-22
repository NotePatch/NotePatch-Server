from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from notepatch.modules.documents.models.document import Document, DocumentArtifact
from notepatch.modules.documents.services.doctr import DocTrClient
from notepatch.modules.tasks.models.task import Task
from notepatch.modules.tasks.services.task import TaskService
from notepatch.platform.config import get_settings
from notepatch.platform.errors import PermanentTaskError, RetryableTaskError, TaskCancelledError
from notepatch.platform.gpu_lease import GpuLeaseService
from notepatch.platform.storage import StorageService


class DocumentVisualPreparationService:
    """Ensure document AI receives only real DocTr rectified image artifacts."""

    def __init__(
        self,
        *,
        db: Session,
        tasks: TaskService,
        storage: StorageService,
        doctr_client: DocTrClient,
        gpu_lease: GpuLeaseService,
    ) -> None:
        self.db = db
        self.tasks = tasks
        self.storage = storage
        self.doctr_client = doctr_client
        self.gpu_lease = gpu_lease

    def ensure_for_ai(self, task: Task, document_ids: list[str]) -> dict[str, DocumentArtifact]:
        requested_ids = list(dict.fromkeys(document_ids))
        if not requested_ids:
            return {}
        documents = self.db.scalars(
            select(Document).where(
                Document.workspace_id == task.workspace_id,
                Document.id.in_(requested_ids),
                Document.status != "deleted",
            )
        ).all()
        by_id = {document.id: document for document in documents}
        if set(by_id) != set(requested_ids):
            raise PermanentTaskError("One or more visual reference documents were not found")

        prepared: dict[str, DocumentArtifact] = {}
        for document_id in requested_ids:
            document = by_id[document_id]
            if document.file_type != "image":
                raise PermanentTaskError(f"Visual reference document {document.id} is not an image")
            artifact = self.latest_valid_artifact(task, document)
            if artifact is not None:
                self._event(
                    task,
                    "ai_visual_deskewed_reused",
                    "Existing DocTr visual reference reused",
                    data={
                        "document_id": document.id,
                        "artifact_id": artifact.id,
                        "source_variant": "doctr_deskewed",
                    },
                )
            else:
                artifact = self._regenerate_for_ai(task, document)
            prepared[document.id] = artifact
        return prepared

    def latest_valid_artifact(self, task: Task, document: Document) -> DocumentArtifact | None:
        expected_illumination = get_settings().doctr_ill_rec
        artifacts = self.db.scalars(
            select(DocumentArtifact)
            .where(
                DocumentArtifact.workspace_id == task.workspace_id,
                DocumentArtifact.document_id == document.id,
                DocumentArtifact.artifact_type == "deskewed_image",
            )
            .order_by(DocumentArtifact.created_at.desc())
        ).all()
        artifact = next(
            (
                item
                for item in artifacts
                if (item.metadata_ or {}).get("processor") == "doctr"
                and self._uses_illumination_rectification(item) == expected_illumination
            ),
            None,
        )
        if artifact is None:
            return None
        try:
            exists = self.storage.object_exists(artifact.bucket, artifact.object_key)
        except Exception as exc:
            raise RetryableTaskError("Could not verify the DocTr visual artifact") from exc
        if exists:
            return artifact
        self._event(
            task,
            "ai_visual_deskewed_stale",
            "Latest DocTr visual metadata points to a missing object",
            level="warning",
            data={
                "document_id": document.id,
                "artifact_id": artifact.id,
                "source_variant": "doctr_deskewed",
            },
        )
        return None

    def rectify_document(
        self,
        task: Task,
        document: Document,
        *,
        ai_visual: bool = False,
    ) -> DocumentArtifact:
        if not get_settings().doctr_enabled:
            raise PermanentTaskError("DocTr is required for document AI visual references")
        self._event(task, "doctr_health", "DocTr health check started", progress=10)
        try:
            health = self.doctr_client.health()
        except (PermanentTaskError, RetryableTaskError, TaskCancelledError):
            raise
        except Exception as exc:
            raise RetryableTaskError(f"DocTr health check failed: {exc}") from exc
        self.tasks.ensure_active(task)
        if not health.get("weights_ready", False):
            raise RetryableTaskError("DocTr model weights are not ready")

        with self.gpu_lease.lease(
            owner=f"task:{task.id}:doctr",
            event_callback=lambda event, data: self._gpu_event(task, event, data),
        ):
            if ai_visual:
                existing = self.latest_valid_artifact(task, document)
                if existing is not None:
                    self._event(
                        task,
                        "ai_visual_deskewed_reused",
                        "DocTr visual reference was produced by another task",
                        data={
                            "document_id": document.id,
                            "artifact_id": existing.id,
                            "source_variant": "doctr_deskewed",
                        },
                    )
                    return existing
            self.tasks.ensure_active(task)
            if not self._original_exists(document):
                if ai_visual:
                    self._event(
                        task,
                        "ai_visual_deskewed_original_missing",
                        "The original image required for DocTr regeneration is missing",
                        level="error",
                        data={"document_id": document.id, "source_variant": "doctr_deskewed"},
                    )
                raise PermanentTaskError(
                    f"Original image is missing for visual reference document {document.id}"
                )
            self._event(task, "doctr_running", "DocTr image rectification running", progress=30)
            try:
                artifact = self._rectify_and_store(task, document, health, ai_visual=ai_visual)
            except (PermanentTaskError, RetryableTaskError, TaskCancelledError):
                raise
            except Exception as exc:
                raise RetryableTaskError(f"DocTr visual preparation failed: {exc}") from exc
            self._event(task, "doctr_succeeded", "DocTr image rectification completed", progress=45)
            return artifact

    def _regenerate_for_ai(self, task: Task, document: Document) -> DocumentArtifact:
        self._event(
            task,
            "ai_visual_deskewed_regeneration_started",
            "Regenerating DocTr visual reference from the original image",
            data={"document_id": document.id, "source_variant": "doctr_deskewed"},
        )
        artifact = self.rectify_document(task, document, ai_visual=True)
        if (artifact.metadata_ or {}).get("task_id") == task.id:
            self._event(
                task,
                "ai_visual_deskewed_regenerated",
                "DocTr visual reference regenerated",
                data={
                    "document_id": document.id,
                    "artifact_id": artifact.id,
                    "source_variant": "doctr_deskewed",
                },
            )
        return artifact

    def _rectify_and_store(
        self,
        task: Task,
        document: Document,
        health: dict,
        *,
        ai_visual: bool,
    ) -> DocumentArtifact:
        with tempfile.TemporaryDirectory(prefix=f"notepatch-doctr-{task.id}-") as tmpdir:
            workdir = Path(tmpdir)
            source = workdir / f"original{Path(document.original_filename).suffix or '.img'}"
            output = workdir / "deskewed_image.png"
            try:
                self.storage.download_file(document.bucket, document.object_key, source)
            except Exception as exc:
                if StorageService.is_object_not_found_error(exc):
                    if ai_visual:
                        self._event(
                            task,
                            "ai_visual_deskewed_original_missing",
                            "The original image required for DocTr regeneration disappeared",
                            level="error",
                            data={"document_id": document.id, "source_variant": "doctr_deskewed"},
                        )
                    raise PermanentTaskError(
                        f"Original image is missing for visual reference document {document.id}"
                    ) from exc
                raise RetryableTaskError("Could not download the original image for DocTr") from exc
            self.tasks.ensure_active(task)
            self.doctr_client.rectify_image(
                source,
                output,
                filename=document.original_filename,
                content_type=document.mime_type,
                ill_rec=get_settings().doctr_ill_rec,
            )
            self.tasks.ensure_active(task)
            artifact_id = str(uuid.uuid4())
            key = self.storage.document_artifact_key(
                task.workspace_id,
                document.id,
                artifact_id,
                "deskewed_image",
                "png",
            )
            uploaded = False
            try:
                self.storage.put_file(
                    document.bucket,
                    key,
                    output,
                    content_type="image/png",
                    metadata={
                        "processor": "doctr",
                        "task_id": task.id,
                        "illumination_rectification": str(get_settings().doctr_ill_rec).lower(),
                    },
                )
                uploaded = True
                self.tasks.ensure_active(task)
                artifact = DocumentArtifact(
                    id=artifact_id,
                    workspace_id=task.workspace_id,
                    document_id=document.id,
                    artifact_type="deskewed_image",
                    bucket=document.bucket,
                    object_key=key,
                    mime_type="image/png",
                    file_size=output.stat().st_size,
                    metadata_={
                        "processor": "doctr",
                        "task_id": task.id,
                        "health": health,
                        "illumination_rectification": get_settings().doctr_ill_rec,
                    },
                )
                self.db.add(artifact)
                self.db.commit()
                self.db.refresh(artifact)
                return artifact
            except Exception:
                self.db.rollback()
                if uploaded:
                    try:
                        self.storage.delete_object(document.bucket, key)
                    except Exception:
                        pass
                raise

    @staticmethod
    def _uses_illumination_rectification(artifact: DocumentArtifact) -> bool:
        metadata = artifact.metadata_ or {}
        if "illumination_rectification" not in metadata:
            # Artifacts created before this flag was recorded used the historical true default.
            return True
        return bool(metadata["illumination_rectification"])

    def _original_exists(self, document: Document) -> bool:
        try:
            return self.storage.object_exists(document.bucket, document.object_key)
        except Exception as exc:
            raise RetryableTaskError("Could not verify the original image object") from exc

    def _gpu_event(self, task: Task, event: str, data: dict) -> None:
        messages = {
            "gpu_lease_waiting": "Waiting for shared GPU lease",
            "gpu_lease_acquired": "Shared GPU lease acquired",
            "gpu_lease_released": "Shared GPU lease released",
            "gpu_lease_timeout": "Timed out waiting for shared GPU lease",
        }
        self._event(
            task,
            event,
            messages.get(event, event.replace("_", " ").title()),
            level="error" if event.endswith("timeout") else "info",
            data=data,
        )

    def _event(
        self,
        task: Task,
        event_type: str,
        message: str,
        *,
        level: str = "info",
        progress: int | None = None,
        data: dict | None = None,
    ) -> None:
        self.tasks.add_event(
            task, event_type, message, level=level, progress=progress, data=data or {}
        )
        self.db.commit()


__all__ = ["DocumentVisualPreparationService"]

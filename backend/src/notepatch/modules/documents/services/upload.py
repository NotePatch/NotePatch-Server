import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from notepatch.platform.config import get_settings
from notepatch.modules.documents.models.document import Document, DocumentArtifact
from notepatch.modules.documents.models.upload import UploadSession
from notepatch.modules.identity.models.user import User
from notepatch.modules.learning.services.workflow import LearningWorkflowService
from notepatch.modules.tasks.services.task import TaskService
from notepatch.platform.storage import StorageService
from notepatch.modules.documents.services.tusd import TusdService
from notepatch.shared.filenames import infer_file_type, sanitize_filename


class UploadService:
    def __init__(self, db: Session, storage: StorageService, tusd: TusdService | None = None) -> None:
        self.db = db
        self.storage = storage
        self.tusd = tusd or TusdService()
        self.settings = get_settings()

    def create_upload_session(
        self,
        *,
        workspace_id: str,
        user: User,
        filename: str,
        mime_type: str | None,
        file_size: int | None,
        document_kind: str,
        title: str | None,
        metadata: dict | None = None,
    ) -> tuple[Document, UploadSession, dict[str, str], str]:
        if file_size is not None and file_size > self.settings.upload_max_file_size_mb * 1024 * 1024:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"File exceeds the {self.settings.upload_max_file_size_mb} MB limit",
            )
        document_id = str(uuid.uuid4())
        safe_filename = sanitize_filename(filename)
        object_key = self.storage.document_original_key(workspace_id, document_id, safe_filename)
        bucket = self.storage.bucket
        document = Document(
            id=document_id,
            workspace_id=workspace_id,
            uploaded_by=user.id,
            title=title,
            original_filename=safe_filename,
            mime_type=mime_type,
            file_size=file_size,
            file_type=infer_file_type(safe_filename, mime_type),
            document_kind=document_kind,
            storage_backend="seaweedfs",
            bucket=bucket,
            object_key=object_key,
            status="created",
            metadata_=metadata or {},
        )
        self.db.add(document)
        self.db.flush()

        upload_session = UploadSession(
            workspace_id=workspace_id,
            user_id=user.id,
            document_id=document.id,
            bucket=bucket,
            object_key=object_key,
            status="created",
        )
        self.db.add(upload_session)
        self.db.flush()

        tus_metadata = self.tusd.build_metadata(
            upload_session_id=upload_session.id,
            document_id=document.id,
            object_key=object_key,
            filename=safe_filename,
            mime_type=mime_type,
        )
        tus_metadata_header = self.tusd.metadata_header(tus_metadata)
        self.db.commit()
        self.db.refresh(document)
        self.db.refresh(upload_session)
        return document, upload_session, tus_metadata, tus_metadata_header

    def get_session(self, workspace_id: str, upload_session_id: str) -> UploadSession:
        upload_session = self.db.scalar(
            select(UploadSession).where(
                UploadSession.workspace_id == workspace_id,
                UploadSession.id == upload_session_id,
            )
        )
        if upload_session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload session not found")
        return upload_session

    def mark_tusd_created(
        self,
        upload_session: UploadSession,
        *,
        tus_upload_id: str,
        tus_upload_url: str | None,
    ) -> UploadSession:
        document = self.db.scalar(
            select(Document).where(
                Document.workspace_id == upload_session.workspace_id,
                Document.id == upload_session.document_id,
            ).with_for_update()
        )
        if upload_session.status in {"cancelled", "completed"} or document is None or document.status == "deleted":
            return upload_session
        upload_session.tus_upload_id = tus_upload_id
        upload_session.tus_upload_url = tus_upload_url or self.tusd.build_upload_url(tus_upload_id)
        upload_session.status = "uploading"
        document.upload_id = tus_upload_id
        document.tus_upload_url = upload_session.tus_upload_url
        document.status = "uploading"
        self.db.commit()
        self.db.refresh(upload_session)
        return upload_session

    def complete_upload(
        self,
        *,
        upload_session: UploadSession,
        tus_upload_id: str | None = None,
        tus_upload_url: str | None = None,
        local_file_path: Path | None = None,
        file_size: int | None = None,
        mime_type: str | None = None,
    ) -> Document:
        document = self.db.scalar(
            select(Document).where(
                Document.workspace_id == upload_session.workspace_id,
                Document.id == upload_session.document_id,
            ).with_for_update()
        )
        if document is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
        if upload_session.status == "cancelled" or document.status == "deleted":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Upload was cancelled")

        task_service = TaskService(self.db)
        if upload_session.status == "completed":
            if self.settings.clamav_enabled:
                if document.scan_status in {"clean", "infected", "failed"}:
                    return document
                existing_scan = task_service.find_active_task(
                    workspace_id=document.workspace_id,
                    task_type="scan_document",
                    resource_type="document",
                    resource_id=document.id,
                )
                if existing_scan is not None:
                    return document
            elif document.status in {"uploaded", "processing", "ready"}:
                return document

        if tus_upload_id:
            upload_session.tus_upload_id = tus_upload_id
            document.upload_id = tus_upload_id
        if tus_upload_url:
            upload_session.tus_upload_url = tus_upload_url
            document.tus_upload_url = tus_upload_url

        if self.storage.object_exists(upload_session.bucket, upload_session.object_key):
            object_metadata = self.storage.get_object_metadata(upload_session.bucket, upload_session.object_key)
            file_size = file_size or object_metadata.get("content_length")
            mime_type = mime_type or object_metadata.get("content_type")
        else:
            if local_file_path is None:
                if not upload_session.tus_upload_id:
                    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Upload not finished")
                local_file_path = self.tusd.local_file_path(upload_session.tus_upload_id)
            if not local_file_path.exists():
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Upload not finished")
            self.storage.put_file(
                upload_session.bucket,
                upload_session.object_key,
                local_file_path,
                content_type=mime_type or document.mime_type,
                metadata={
                    "workspace_id": upload_session.workspace_id,
                    "document_id": document.id,
                    "upload_session_id": upload_session.id,
                },
            )
            file_size = file_size if file_size is not None else local_file_path.stat().st_size

        actual_size = file_size if file_size is not None else document.file_size
        if actual_size is not None and actual_size > self.settings.upload_max_file_size_mb * 1024 * 1024:
            self.storage.delete_object(upload_session.bucket, upload_session.object_key)
            upload_session.status = "failed"
            document.status = "failed"
            document.scan_status = "failed"
            document.scan_message = f"File exceeds the {self.settings.upload_max_file_size_mb} MB limit"
            self.db.commit()
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=document.scan_message)

        document.status = "scanning" if self.settings.clamav_enabled else "uploaded"
        document.scan_status = "pending" if self.settings.clamav_enabled else "skipped"
        document.scan_message = None
        document.bucket = upload_session.bucket
        document.object_key = upload_session.object_key
        document.mime_type = mime_type or document.mime_type
        document.file_size = file_size if file_size is not None else document.file_size
        upload_session.status = "completed"

        existing_artifact = self.db.scalar(
            select(DocumentArtifact).where(
                DocumentArtifact.workspace_id == document.workspace_id,
                DocumentArtifact.document_id == document.id,
                DocumentArtifact.artifact_type == "original",
            )
        )
        if existing_artifact is None:
            self.db.add(
                DocumentArtifact(
                    workspace_id=document.workspace_id,
                    document_id=document.id,
                    artifact_type="original",
                    bucket=document.bucket,
                    object_key=document.object_key,
                    mime_type=document.mime_type,
                    file_size=document.file_size,
                    metadata_={"source": "tusd"},
                )
            )
        scan_task = None
        scan_queue = None
        if self.settings.clamav_enabled:
            scan_task = task_service.find_active_task(
                workspace_id=document.workspace_id,
                task_type="scan_document",
                resource_type="document",
                resource_id=document.id,
            )
            if scan_task is None:
                scan_task, scan_queue = task_service.create_task_record(
                    workspace_id=document.workspace_id,
                    task_type="scan_document",
                    resource_type="document",
                    resource_id=document.id,
                    payload={"document_id": document.id},
                )

        self.db.commit()
        self.db.refresh(document)
        if scan_task is not None and scan_queue is not None:
            self.db.refresh(scan_task)
            if not task_service.enqueue_task(scan_task.id, queue_name=scan_queue):
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Task queue is unavailable",
                )
        elif not self.settings.clamav_enabled and self.settings.auto_learning_pipeline:
            LearningWorkflowService(self.db, self.storage).schedule_after_upload(document)
        return document

    def fail_or_cancel_upload(self, upload_session: UploadSession, status_value: str) -> None:
        upload_session.status = status_value
        document = self.db.scalar(
            select(Document).where(
                Document.workspace_id == upload_session.workspace_id,
                Document.id == upload_session.document_id,
            ).with_for_update()
        )
        if document is not None and document.status != "deleted":
            document.status = "failed"
        self.db.commit()


def coerce_tusd_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)

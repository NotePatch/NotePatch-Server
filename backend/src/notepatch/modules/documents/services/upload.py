import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from notepatch.platform.config import get_settings
from notepatch.modules.ai.services.image_naming import schedule_image_remark_ocr
from notepatch.modules.documents.models.document import (
    AUTO_LEARNING_DOCUMENT_KINDS,
    CHAT_ATTACHMENT_KIND,
    Document,
    DocumentArtifact,
)
from notepatch.modules.documents.models.upload import UploadSession
from notepatch.modules.identity.models.user import User
from notepatch.modules.learning.services.assignment import LearningUnitAssignmentService
from notepatch.modules.learning.services.note_sets import NoteSetService
from notepatch.modules.learning.services.workflow import LearningWorkflowService
from notepatch.modules.tasks.services.task import TaskService
from notepatch.modules.tasks.services.workflow import WorkflowTracker
from notepatch.platform.storage import StorageService
from notepatch.modules.documents.services.tusd import TusdService
from notepatch.shared.filenames import infer_file_type, sanitize_filename


LEARNING_PIPELINE_FILE_TYPES = {"image", "pdf", "docx", "pptx"}
EXTENDED_UPLOAD_EXTENSIONS = {
    "jpg", "jpeg", "png", "webp", "tif", "tiff", "gif", "bmp", "heic",
    "pdf", "doc", "docx", "odt", "rtf", "ppt", "pptx", "odp",
    "xls", "xlsx", "ods", "txt", "md", "csv", "tsv", "json", "jsonl",
    "yaml", "yml", "xml", "html", "htm", "epub", "eml", "msg", "ipynb",
    "mp3", "wav", "m4a", "aac", "ogg", "flac", "mp4", "mov", "mkv",
    "avi", "webm", "zip", "7z", "rar", "tar", "tgz", "gz", "bz2", "xz", "zst",
}


def _filename_extension(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _validate_upload_format(
    *,
    filename: str,
    mime_type: str | None,
    file_type: str,
    document_kind: str,
    allowed_mime_types: set[str],
) -> None:
    extension = _filename_extension(filename)
    declared = (mime_type or "").split(";", 1)[0].strip().lower()
    known_extension = extension in EXTENDED_UPLOAD_EXTENSIONS
    declared_allowed = declared in allowed_mime_types
    generic_declared = declared in {"", "application/octet-stream"}
    if (
        (extension and not known_extension)
        or (declared and not generic_declared and not declared_allowed)
        or (not extension and not declared_allowed)
    ):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail={
                "code": "unsupported_file_format",
                "message": f"Unsupported upload format: {declared or extension or 'unknown'}",
            },
        )
    if document_kind in AUTO_LEARNING_DOCUMENT_KINDS and file_type not in LEARNING_PIPELINE_FILE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "unsupported_learning_format",
                "message": "Learning documents must be an image, PDF, DOCX, or PPTX file",
            },
        )


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
        remark: str | None = None,
        save_to_documents: bool = True,
        metadata: dict | None = None,
        note_set_id: str | None = None,
        page_index: int | None = None,
    ) -> tuple[Document, UploadSession, dict[str, str], str]:
        if file_size is not None and file_size > self.settings.upload_max_file_size_mb * 1024 * 1024:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"File exceeds the {self.settings.upload_max_file_size_mb} MB limit",
            )
        note_set = None
        if note_set_id is not None:
            if page_index is None:
                raise HTTPException(status_code=422, detail="page_index is required with note_set_id")
            note_set = NoteSetService(self.db).validate_upload(
                workspace_id, note_set_id, page_index, document_kind
            )
        document_id = str(uuid.uuid4())
        safe_filename = sanitize_filename(filename)
        object_key = self.storage.document_original_key(workspace_id, document_id, safe_filename)
        bucket = self.storage.bucket
        file_type = infer_file_type(safe_filename, mime_type)
        _validate_upload_format(
            filename=safe_filename,
            mime_type=mime_type,
            file_type=file_type,
            document_kind=document_kind,
            allowed_mime_types=self.settings.upload_allowed_mime_type_set,
        )
        document_metadata = dict(metadata or {})
        document_metadata.pop("ai_image_naming", None)
        document_metadata.pop("image_remark_generation", None)
        if title and title.strip():
            document_metadata["title_source"] = "user"
        user_remark = " ".join(remark.split()) if remark else None
        auto_remark = (
            file_type == "image"
            and self.settings.ai_image_remark_enabled
            and user.auto_image_remark_enabled
            and user_remark is None
        )
        document_metadata["image_remark_generation"] = {
            "status": "waiting_upload" if auto_remark else "user" if user_remark else "disabled",
            **({"model": self.settings.ai_image_remark_model} if auto_remark else {}),
            **(
                {"client_locale": document_metadata["client_locale"]}
                if auto_remark and isinstance(document_metadata.get("client_locale"), str)
                else {}
            ),
        }
        document = Document(
            id=document_id,
            workspace_id=workspace_id,
            uploaded_by=user.id,
            title=title,
            remark=user_remark or safe_filename,
            remark_source="user" if user_remark else "original_filename",
            original_filename=safe_filename,
            mime_type=mime_type,
            file_size=file_size,
            file_type=file_type,
            document_kind=document_kind,
            retention_scope="workspace" if save_to_documents else "conversation",
            storage_backend="seaweedfs",
            bucket=bucket,
            object_key=object_key,
            status="created",
            metadata_=document_metadata,
        )
        self.db.add(document)
        self.db.flush()
        if note_set is not None:
            NoteSetService(self.db).attach(note_set, document, page_index)
        if document.document_kind != CHAT_ATTACHMENT_KIND:
            LearningUnitAssignmentService(self.db, storage=self.storage).preassign(document)
        workflow = WorkflowTracker(self.db).create_for_document(
            document,
            user_id=user.id,
            trigger_type="upload",
            waiting_upload=True,
        )

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
                schedule_image_remark_ocr(self.db, task_service, document)
                self.db.refresh(document)
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
        workflow = WorkflowTracker(self.db).latest_for_document(document.workspace_id, document.id)
        if workflow is not None:
            WorkflowTracker(self.db).mark_upload_completed(workflow, document)
        document.scan_status = "pending" if self.settings.clamav_enabled else "skipped"
        document.scan_message = None
        document.bucket = upload_session.bucket
        document.object_key = upload_session.object_key
        document.mime_type = mime_type or document.mime_type
        document.file_size = file_size if file_size is not None else document.file_size
        upload_session.status = "completed"
        remark_state = dict((document.metadata_ or {}).get("image_remark_generation") or {})
        if remark_state.get("status") == "waiting_upload":
            remark_state["status"] = "waiting_ocr"
            document.metadata_ = {**(document.metadata_ or {}), "image_remark_generation": remark_state}

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
                    payload={
                        "document_id": document.id,
                        "workflow_run_id": document.latest_workflow_run_id,
                    },
                )
                WorkflowTracker(self.db).link_task(scan_task)

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
            if document.document_kind not in AUTO_LEARNING_DOCUMENT_KINDS:
                document.status = "ready"
                workflow = WorkflowTracker(self.db).latest_for_document(document.workspace_id, document.id)
                if workflow is not None:
                    WorkflowTracker(self.db).mark_ready_without_tasks(workflow, document)
                self.db.commit()
                self.db.refresh(document)

        if not self.settings.clamav_enabled:
            schedule_image_remark_ocr(self.db, task_service, document)
            self.db.refresh(document)

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
            WorkflowTracker(self.db).mark_upload_failed(document, f"Upload {status_value}")
        self.db.commit()


def coerce_tusd_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from notepatch.entrypoints.deps import get_current_user, get_storage_service, get_task_service, get_workspace_member
from notepatch.platform.database import get_db
from notepatch.modules.documents.models.document import CHAT_ATTACHMENT_KIND, Document, DocumentArtifact
from notepatch.modules.tasks.models.task import Task
from notepatch.modules.documents.models.upload import UploadSession
from notepatch.modules.identity.models.user import User
from notepatch.modules.identity.models.workspace import WorkspaceMember
from notepatch.modules.documents.schemas.document import (
    CompleteUploadRequest,
    DownloadUrlResponse,
    DocumentDeleteResponse,
    DocumentRead,
    OcrArtifactRead,
    OcrArtifactsResponse,
    ProcessDocumentRequest,
    UploadSessionRequest,
    UploadSessionResponse,
)
from notepatch.modules.tasks.schemas.task import TaskRead
from notepatch.modules.documents.services.document import DocumentService
from notepatch.platform.storage import StorageService
from notepatch.platform.config import get_settings
from notepatch.platform.rate_limit import RateLimiter
from notepatch.modules.tasks.services.task import TaskService
from notepatch.modules.documents.services.tusd import TusdService
from notepatch.modules.documents.services.upload import UploadService

router = APIRouter(prefix="/workspaces/{workspace_id}/documents", tags=["documents"])

OCR_ARTIFACT_TYPES = ("ocr_json", "ocr_markdown", "ocr_text")
OCR_ARTIFACT_ORDER = {artifact_type: index for index, artifact_type in enumerate(OCR_ARTIFACT_TYPES)}


def _role_name(member: WorkspaceMember) -> str:
    return member.role.name if member.role is not None else ""


def _can_write_documents(member: WorkspaceMember) -> bool:
    return _role_name(member) == "owner"


def _require_document_write(member: WorkspaceMember) -> None:
    if not _can_write_documents(member):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Missing workspace permission: documents.write")


def _require_can_delete_document(member: WorkspaceMember, document: Document) -> None:
    if _role_name(member) == "owner":
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot delete this document")


@router.post("/upload-session", response_model=UploadSessionResponse, status_code=status.HTTP_201_CREATED)
def create_upload_session(
    workspace_id: str,
    payload: UploadSessionRequest,
    member: WorkspaceMember = Depends(get_workspace_member),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    storage: StorageService = Depends(get_storage_service),
) -> UploadSessionResponse:
    _require_document_write(member)
    RateLimiter().check("upload", current_user.id, get_settings().upload_rate_limit_per_minute)
    document, upload_session, tus_metadata, tus_metadata_header = UploadService(db, storage).create_upload_session(
        workspace_id=workspace_id,
        user=current_user,
        filename=payload.filename,
        mime_type=payload.mime_type,
        file_size=payload.file_size,
        document_kind=payload.document_kind,
        title=payload.title,
        metadata=payload.metadata,
    )
    return UploadSessionResponse(
        document=document,
        upload_session=upload_session,
        tus_endpoint=TusdService().base_url,
        tus_metadata=tus_metadata,
        tus_metadata_header=tus_metadata_header,
        bucket=document.bucket,
        object_key=document.object_key,
    )


@router.post("/complete-upload", response_model=DocumentRead)
def complete_upload(
    workspace_id: str,
    payload: CompleteUploadRequest,
    member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
    storage: StorageService = Depends(get_storage_service),
) -> Document:
    _require_document_write(member)
    query = select(UploadSession).where(UploadSession.workspace_id == workspace_id)
    if payload.upload_session_id:
        query = query.where(UploadSession.id == payload.upload_session_id)
    elif payload.document_id:
        query = query.where(UploadSession.document_id == payload.document_id)
    else:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="upload_session_id or document_id is required")
    upload_session = db.scalar(query)
    if upload_session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload session not found")
    return UploadService(db, storage).complete_upload(
        upload_session=upload_session,
        tus_upload_id=payload.tus_upload_id,
        tus_upload_url=payload.tus_upload_url,
        file_size=payload.file_size,
        mime_type=payload.mime_type,
    )


@router.get("", response_model=list[DocumentRead])
def list_documents(
    workspace_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    status_filter: str | None = Query(default=None, alias="status"),
    document_kind: str | None = None,
    file_type: str | None = None,
    _member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
) -> list[Document]:
    query = select(Document).where(Document.workspace_id == workspace_id, Document.status != "deleted")
    if status_filter:
        query = query.where(Document.status == status_filter)
    if document_kind:
        query = query.where(Document.document_kind == document_kind)
    if file_type:
        query = query.where(Document.file_type == file_type)
    return db.scalars(
        query.order_by(Document.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()


@router.get("/{document_id}", response_model=DocumentRead)
def get_document(
    workspace_id: str,
    document_id: str,
    _member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
    storage: StorageService = Depends(get_storage_service),
) -> Document:
    return DocumentService(db, storage).get_document(workspace_id, document_id)


@router.delete(
    "/{document_id}",
    response_model=DocumentDeleteResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def delete_document(
    workspace_id: str,
    document_id: str,
    member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
    storage: StorageService = Depends(get_storage_service),
) -> DocumentDeleteResponse:
    document = DocumentService(db, storage).get_document(workspace_id, document_id, include_deleted=True)
    _require_can_delete_document(member, document=document)
    document, purge_task = DocumentService(db, storage).request_delete(workspace_id, document_id)
    return DocumentDeleteResponse(
        document_id=document.id,
        purge_status=document.purge_status or "queued",
        purge_task_id=purge_task.id,
    )


@router.get("/{document_id}/download-url", response_model=DownloadUrlResponse)
def get_download_url(
    workspace_id: str,
    document_id: str,
    expires_seconds: int = Query(default=900, ge=60, le=86400),
    _member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
    storage: StorageService = Depends(get_storage_service),
) -> DownloadUrlResponse:
    document = DocumentService(db, storage).get_document(workspace_id, document_id)
    return DownloadUrlResponse(
        download_url=storage.create_presigned_download_url(document.bucket, document.object_key, expires_seconds),
        expires_seconds=expires_seconds,
    )


@router.get("/{document_id}/ocr", response_model=OcrArtifactsResponse)
def get_document_ocr_artifacts(
    workspace_id: str,
    document_id: str,
    include_download_url: bool = Query(default=False),
    expires_seconds: int = Query(default=900, ge=60, le=86400),
    _member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
    storage: StorageService = Depends(get_storage_service),
) -> OcrArtifactsResponse:
    document = DocumentService(db, storage).get_document(workspace_id, document_id)
    artifacts = _latest_ocr_artifacts(db, workspace_id, document.id)
    return OcrArtifactsResponse(
        document_id=document.id,
        artifacts=[
            OcrArtifactRead(
                id=artifact.id,
                artifact_type=artifact.artifact_type,
                mime_type=artifact.mime_type,
                file_size=artifact.file_size,
                created_at=artifact.created_at,
                download_url=(
                    storage.create_presigned_artifact_download_url(
                        artifact.bucket,
                        artifact.object_key,
                        expires_seconds,
                    )
                    if include_download_url
                    else None
                ),
            )
            for artifact in artifacts
        ],
    )


@router.post("/{document_id}/process", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
def process_document(
    workspace_id: str,
    document_id: str,
    payload: ProcessDocumentRequest,
    member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
    storage: StorageService = Depends(get_storage_service),
    task_service: TaskService = Depends(get_task_service),
) -> Task:
    _require_document_write(member)
    document = DocumentService(db, storage).get_document(workspace_id, document_id)
    if document.document_kind == CHAT_ATTACHMENT_KIND:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Chat attachments are available to AI chat but cannot enter the learning pipeline",
        )
    force = bool(payload.options.get("force_reprocess"))
    active = task_service.find_active_task(
        workspace_id=workspace_id,
        task_type="document_processing_pipeline",
        resource_type="document",
        resource_id=document.id,
    )
    if active is not None:
        if force:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Document processing is already active")
        return active
    if document.status not in {"uploaded", "ready", "failed"}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Document upload is not complete")
    if not document.bucket or not document.object_key or not storage.object_exists(document.bucket, document.object_key):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Document object is not available")
    return task_service.create_task(
        workspace_id=workspace_id,
        task_type="document_processing_pipeline",
        resource_type="document",
        resource_id=document.id,
        payload={"document_id": document.id, "pipeline": payload.pipeline, "options": payload.options},
    )


def _latest_ocr_artifacts(db: Session, workspace_id: str, document_id: str) -> list[DocumentArtifact]:
    artifacts = db.scalars(
        select(DocumentArtifact)
        .where(
            DocumentArtifact.workspace_id == workspace_id,
            DocumentArtifact.document_id == document_id,
            DocumentArtifact.artifact_type.in_(OCR_ARTIFACT_TYPES),
        )
        .order_by(DocumentArtifact.created_at.desc())
    ).all()
    groups: dict[str, dict[str, DocumentArtifact]] = {}
    for artifact in artifacts:
        run_id = (artifact.metadata_ or {}).get("ocr_run_id")
        if run_id:
            groups.setdefault(str(run_id), {}).setdefault(artifact.artifact_type, artifact)
    complete_groups = [
        group
        for group in groups.values()
        if all(artifact_type in group for artifact_type in OCR_ARTIFACT_TYPES)
    ]
    if complete_groups:
        latest_group = max(
            complete_groups,
            key=lambda group: max(artifact.created_at for artifact in group.values()),
        )
        return sorted(latest_group.values(), key=lambda artifact: OCR_ARTIFACT_ORDER.get(artifact.artifact_type, 99))

    latest_by_type: dict[str, DocumentArtifact] = {}
    for artifact in artifacts:
        latest_by_type.setdefault(artifact.artifact_type, artifact)
    return [
        latest_by_type[artifact_type]
        for artifact_type in OCR_ARTIFACT_TYPES
        if artifact_type in latest_by_type
    ]

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from notepatch.entrypoints.deps import get_current_user, get_storage_service
from notepatch.platform.config import get_settings
from notepatch.platform.database import get_db
from notepatch.modules.documents.models.document import Document, DocumentArtifact
from notepatch.modules.tasks.models.task import Task, TaskEvent
from notepatch.modules.identity.models.user import User
from notepatch.modules.identity.models.workspace import Workspace
from notepatch.modules.admin.schemas.admin import (
    AdminArtifactRead,
    AdminDocumentDetailResponse,
    AdminDocumentListItem,
    AdminDocumentListResponse,
    AdminDownloadUrlResponse,
    AdminMeResponse,
    AdminOverviewResponse,
    AdminQueuesResponse,
    AdminServicesResponse,
    AdminTaskDetailResponse,
    AdminTaskEventRead,
    AdminTaskListItem,
    AdminTaskListResponse,
    AdminUserDetailResponse,
    AdminUserListItem,
    AdminUserListResponse,
    AdminUserRead,
    AdminWorkspaceRead,
)
from notepatch.platform.storage import StorageService
from notepatch.modules.admin.services.health import queue_statuses, service_statuses

router = APIRouter(prefix="/admin", tags=["admin"])


def require_admin_user(current_user: User = Depends(get_current_user)) -> User:
    settings = get_settings()
    admin_emails = settings.admin_email_set
    if not admin_emails:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin API is disabled")
    if current_user.email.lower() not in admin_emails:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return current_user


def _total(db: Session, query) -> int:
    return int(db.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0)


def _offset(page: int, page_size: int) -> int:
    return (page - 1) * page_size


def _count(db: Session, column) -> int:
    return int(db.scalar(select(func.count(column))) or 0)


def _status_counts(db: Session, model, workspace_id: str | None = None) -> dict[str, int]:
    query = select(model.status, func.count()).group_by(model.status)
    if workspace_id is not None:
        query = query.where(model.workspace_id == workspace_id)
    return {str(status): int(count) for status, count in db.execute(query).all()}


def _workspace_for_user(db: Session, user_id: str) -> Workspace | None:
    return db.scalar(select(Workspace).where(Workspace.owner_user_id == user_id, Workspace.type == "personal"))


def _user_read(user: User) -> AdminUserRead:
    return AdminUserRead(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        username=user.username,
        phone=user.phone,
        is_active=user.is_active,
        created_at=user.created_at,
    )


def _workspace_read(workspace: Workspace | None) -> AdminWorkspaceRead | None:
    if workspace is None:
        return None
    return AdminWorkspaceRead(
        id=workspace.id,
        name=workspace.name,
        type=workspace.type,
        owner_user_id=workspace.owner_user_id,
        created_at=workspace.created_at,
    )


def _document_item(db: Session, document: Document) -> AdminDocumentListItem:
    uploader = db.get(User, document.uploaded_by)
    artifacts_count = int(
        db.scalar(select(func.count(DocumentArtifact.id)).where(DocumentArtifact.document_id == document.id)) or 0
    )
    return AdminDocumentListItem(
        id=document.id,
        workspace_id=document.workspace_id,
        uploaded_by=document.uploaded_by,
        uploaded_by_email=uploader.email if uploader else None,
        title=document.title,
        original_filename=document.original_filename,
        mime_type=document.mime_type,
        file_size=document.file_size,
        file_type=document.file_type,
        document_kind=document.document_kind,
        status=document.status,
        artifacts_count=artifacts_count,
        created_at=document.created_at,
        updated_at=document.updated_at,
    )


def _task_item(task: Task) -> AdminTaskListItem:
    return AdminTaskListItem(
        id=task.id,
        workspace_id=task.workspace_id,
        task_type=task.task_type,
        status=task.status,
        resource_type=task.resource_type,
        resource_id=task.resource_id,
        progress=task.progress,
        error_message=task.error_message,
        created_at=task.created_at,
        updated_at=task.updated_at,
        started_at=task.started_at,
        finished_at=task.finished_at,
    )


def _artifact_read(artifact: DocumentArtifact) -> AdminArtifactRead:
    return AdminArtifactRead(
        id=artifact.id,
        workspace_id=artifact.workspace_id,
        document_id=artifact.document_id,
        artifact_type=artifact.artifact_type,
        bucket=artifact.bucket,
        object_key=artifact.object_key,
        mime_type=artifact.mime_type,
        file_size=artifact.file_size,
        metadata=artifact.metadata_ or {},
        created_at=artifact.created_at,
    )


@router.get("/me", response_model=AdminMeResponse)
def admin_me(current_user: User = Depends(require_admin_user)) -> AdminMeResponse:
    return AdminMeResponse(user=_user_read(current_user), admin=True)


@router.get("/overview", response_model=AdminOverviewResponse)
def overview(
    _admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> AdminOverviewResponse:
    return AdminOverviewResponse(
        users_count=_count(db, User.id),
        documents_count=_count(db, Document.id),
        uploaded_documents_count=int(
            db.scalar(select(func.count(Document.id)).where(Document.status == "uploaded")) or 0
        ),
        ready_documents_count=int(db.scalar(select(func.count(Document.id)).where(Document.status == "ready")) or 0),
        tasks_count=_count(db, Task.id),
        failed_tasks_count=int(db.scalar(select(func.count(Task.id)).where(Task.status == "failed")) or 0),
        queued_tasks_count=int(db.scalar(select(func.count(Task.id)).where(Task.status == "queued")) or 0),
        running_tasks_count=int(db.scalar(select(func.count(Task.id)).where(Task.status == "running")) or 0),
        ocr_artifacts_count=int(
            db.scalar(
                select(func.count(DocumentArtifact.id)).where(
                    DocumentArtifact.artifact_type.in_(("ocr_json", "ocr_markdown", "ocr_text"))
                )
            )
            or 0
        ),
        queue_lengths=queue_statuses(),
    )


@router.get("/users", response_model=AdminUserListResponse)
def list_users(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    search: str | None = Query(default=None),
    _admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> AdminUserListResponse:
    query = select(User)
    if search:
        needle = f"%{search.strip()}%"
        query = query.where(
            or_(
                User.email.ilike(needle),
                User.full_name.ilike(needle),
                User.username.ilike(needle),
                User.phone.ilike(needle),
            )
        )
    total = _total(db, query)
    users = db.scalars(query.order_by(User.created_at.desc()).offset(_offset(page, page_size)).limit(page_size)).all()
    items: list[AdminUserListItem] = []
    for user in users:
        workspace = _workspace_for_user(db, user.id)
        workspace_id = workspace.id if workspace else None
        documents_count = (
            int(db.scalar(select(func.count(Document.id)).where(Document.workspace_id == workspace_id)) or 0)
            if workspace_id
            else 0
        )
        tasks_count = (
            int(db.scalar(select(func.count(Task.id)).where(Task.workspace_id == workspace_id)) or 0)
            if workspace_id
            else 0
        )
        items.append(
            AdminUserListItem(
                **_user_read(user).model_dump(),
                workspace_id=workspace_id,
                workspace_name=workspace.name if workspace else None,
                documents_count=documents_count,
                tasks_count=tasks_count,
            )
        )
    return AdminUserListResponse(page=page, page_size=page_size, total=total, items=items)


@router.get("/users/{user_id}", response_model=AdminUserDetailResponse)
def get_user_detail(
    user_id: str,
    _admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> AdminUserDetailResponse:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    workspace = _workspace_for_user(db, user.id)
    workspace_id = workspace.id if workspace else None
    counts = {
        "documents": int(db.scalar(select(func.count(Document.id)).where(Document.workspace_id == workspace_id)) or 0)
        if workspace_id
        else 0,
        "tasks": int(db.scalar(select(func.count(Task.id)).where(Task.workspace_id == workspace_id)) or 0)
        if workspace_id
        else 0,
        "artifacts": int(
            db.scalar(select(func.count(DocumentArtifact.id)).where(DocumentArtifact.workspace_id == workspace_id))
            or 0
        )
        if workspace_id
        else 0,
    }
    return AdminUserDetailResponse(
        user=_user_read(user),
        workspace=_workspace_read(workspace),
        counts=counts,
        document_status_counts=_status_counts(db, Document, workspace_id) if workspace_id else {},
        task_status_counts=_status_counts(db, Task, workspace_id) if workspace_id else {},
    )


@router.get("/documents", response_model=AdminDocumentListResponse)
def list_documents(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    search: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    file_type: str | None = None,
    document_kind: str | None = None,
    user_id: str | None = None,
    workspace_id: str | None = None,
    _admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> AdminDocumentListResponse:
    query = select(Document)
    if search:
        needle = f"%{search.strip()}%"
        query = query.where(or_(Document.title.ilike(needle), Document.original_filename.ilike(needle)))
    if status_filter:
        query = query.where(Document.status == status_filter)
    if file_type:
        query = query.where(Document.file_type == file_type)
    if document_kind:
        query = query.where(Document.document_kind == document_kind)
    if user_id:
        query = query.where(Document.uploaded_by == user_id)
    if workspace_id:
        query = query.where(Document.workspace_id == workspace_id)
    total = _total(db, query)
    documents = db.scalars(
        query.order_by(Document.created_at.desc()).offset(_offset(page, page_size)).limit(page_size)
    ).all()
    return AdminDocumentListResponse(
        page=page,
        page_size=page_size,
        total=total,
        items=[_document_item(db, document) for document in documents],
    )


@router.get("/documents/{document_id}", response_model=AdminDocumentDetailResponse)
def get_document_detail(
    document_id: str,
    _admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> AdminDocumentDetailResponse:
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return AdminDocumentDetailResponse(
        document=_document_item(db, document),
        bucket=document.bucket,
        object_key=document.object_key,
        storage_backend=document.storage_backend,
        upload_id=document.upload_id,
        tus_upload_url=document.tus_upload_url,
        sha256=document.sha256,
        metadata=document.metadata_ or {},
    )


@router.get("/documents/{document_id}/artifacts", response_model=list[AdminArtifactRead])
def list_document_artifacts(
    document_id: str,
    _admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> list[AdminArtifactRead]:
    if db.get(Document, document_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    artifacts = db.scalars(
        select(DocumentArtifact).where(DocumentArtifact.document_id == document_id).order_by(DocumentArtifact.created_at.asc())
    ).all()
    return [_artifact_read(artifact) for artifact in artifacts]


@router.get("/documents/{document_id}/download-url", response_model=AdminDownloadUrlResponse)
def get_document_download_url(
    document_id: str,
    expires_seconds: int = Query(default=900, ge=60, le=86400),
    _admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
    storage: StorageService = Depends(get_storage_service),
) -> AdminDownloadUrlResponse:
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return AdminDownloadUrlResponse(
        id=document.id,
        resource_type="document",
        filename=document.original_filename,
        mime_type=document.mime_type,
        expires_in=expires_seconds,
        download_url=storage.create_presigned_download_url(document.bucket, document.object_key, expires_seconds),
    )


@router.get("/artifacts/{artifact_id}/download-url", response_model=AdminDownloadUrlResponse)
def get_artifact_download_url(
    artifact_id: str,
    expires_seconds: int = Query(default=900, ge=60, le=86400),
    _admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
    storage: StorageService = Depends(get_storage_service),
) -> AdminDownloadUrlResponse:
    artifact = db.get(DocumentArtifact, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found")
    return AdminDownloadUrlResponse(
        id=artifact.id,
        resource_type="artifact",
        filename=storage.filename_for_object_key(artifact.object_key),
        mime_type=artifact.mime_type,
        expires_in=expires_seconds,
        download_url=storage.create_presigned_artifact_download_url(artifact.bucket, artifact.object_key, expires_seconds),
    )


@router.get("/tasks", response_model=AdminTaskListResponse)
def list_tasks(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    status_filter: str | None = Query(default=None, alias="status"),
    task_type: str | None = None,
    workspace_id: str | None = None,
    resource_id: str | None = None,
    _admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> AdminTaskListResponse:
    query = select(Task)
    if status_filter:
        query = query.where(Task.status == status_filter)
    if task_type:
        query = query.where(Task.task_type == task_type)
    if workspace_id:
        query = query.where(Task.workspace_id == workspace_id)
    if resource_id:
        query = query.where(Task.resource_id == resource_id)
    total = _total(db, query)
    tasks = db.scalars(query.order_by(Task.created_at.desc()).offset(_offset(page, page_size)).limit(page_size)).all()
    return AdminTaskListResponse(page=page, page_size=page_size, total=total, items=[_task_item(task) for task in tasks])


@router.get("/tasks/{task_id}", response_model=AdminTaskDetailResponse)
def get_task_detail(
    task_id: str,
    _admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> AdminTaskDetailResponse:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return AdminTaskDetailResponse(task=_task_item(task), payload=task.payload or {}, result=task.result)


@router.get("/tasks/{task_id}/events", response_model=list[AdminTaskEventRead])
def get_task_events(
    task_id: str,
    _admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> list[AdminTaskEventRead]:
    if db.get(Task, task_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    events = db.scalars(select(TaskEvent).where(TaskEvent.task_id == task_id).order_by(TaskEvent.created_at.asc())).all()
    return [
        AdminTaskEventRead(
            id=event.id,
            task_id=event.task_id,
            workspace_id=event.workspace_id,
            event_type=event.event_type,
            level=event.level,
            message=event.message,
            progress=event.progress,
            data=event.data or {},
            created_at=event.created_at,
        )
        for event in events
    ]


@router.get("/queues", response_model=AdminQueuesResponse)
def get_queues(_admin: User = Depends(require_admin_user)) -> AdminQueuesResponse:
    return AdminQueuesResponse(queues=queue_statuses())


@router.get("/services", response_model=AdminServicesResponse)
def get_services(
    _admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
    storage: StorageService = Depends(get_storage_service),
) -> AdminServicesResponse:
    return AdminServicesResponse(services=service_statuses(db, storage))

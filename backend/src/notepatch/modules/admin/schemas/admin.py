from datetime import datetime

from pydantic import BaseModel


class AdminUserRead(BaseModel):
    id: str
    email: str
    full_name: str | None = None
    username: str | None = None
    phone: str | None = None
    is_active: bool
    created_at: datetime


class AdminWorkspaceRead(BaseModel):
    id: str
    name: str
    type: str
    owner_user_id: str
    created_at: datetime


class AdminPageMeta(BaseModel):
    page: int
    page_size: int
    total: int


class AdminUserListItem(AdminUserRead):
    workspace_id: str | None = None
    workspace_name: str | None = None
    documents_count: int = 0
    tasks_count: int = 0


class AdminUserListResponse(AdminPageMeta):
    items: list[AdminUserListItem]


class AdminUserDetailResponse(BaseModel):
    user: AdminUserRead
    workspace: AdminWorkspaceRead | None = None
    counts: dict[str, int]
    document_status_counts: dict[str, int]
    task_status_counts: dict[str, int]


class AdminDocumentListItem(BaseModel):
    id: str
    workspace_id: str
    uploaded_by: str
    uploaded_by_email: str | None = None
    title: str | None = None
    original_filename: str
    mime_type: str | None = None
    file_size: int | None = None
    file_type: str
    document_kind: str
    status: str
    artifacts_count: int = 0
    created_at: datetime
    updated_at: datetime


class AdminDocumentListResponse(AdminPageMeta):
    items: list[AdminDocumentListItem]


class AdminArtifactRead(BaseModel):
    id: str
    workspace_id: str
    document_id: str
    artifact_type: str
    bucket: str
    object_key: str
    mime_type: str | None = None
    file_size: int | None = None
    metadata: dict
    created_at: datetime


class AdminDocumentDetailResponse(BaseModel):
    document: AdminDocumentListItem
    bucket: str
    object_key: str
    storage_backend: str
    upload_id: str | None = None
    tus_upload_url: str | None = None
    sha256: str | None = None
    metadata: dict


class AdminTaskListItem(BaseModel):
    id: str
    workspace_id: str
    task_type: str
    status: str
    resource_type: str | None = None
    resource_id: str | None = None
    progress: int
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class AdminTaskListResponse(AdminPageMeta):
    items: list[AdminTaskListItem]


class AdminTaskDetailResponse(BaseModel):
    task: AdminTaskListItem
    payload: dict
    result: dict | None = None


class AdminTaskEventRead(BaseModel):
    id: str
    task_id: str
    workspace_id: str
    event_type: str
    level: str
    message: str
    progress: int | None = None
    data: dict
    created_at: datetime


class AdminDownloadUrlResponse(BaseModel):
    id: str
    resource_type: str
    filename: str
    mime_type: str | None = None
    expires_in: int
    download_url: str


class AdminQueueStatus(BaseModel):
    name: str
    redis_key: str
    length: int | None = None
    status: str
    error: str | None = None


class AdminQueuesResponse(BaseModel):
    queues: list[AdminQueueStatus]


class AdminServiceStatus(BaseModel):
    name: str
    status: str
    detail: str | None = None
    latency_ms: float | None = None


class AdminServicesResponse(BaseModel):
    services: list[AdminServiceStatus]


class AdminOverviewResponse(BaseModel):
    users_count: int
    documents_count: int
    uploaded_documents_count: int
    ready_documents_count: int
    tasks_count: int
    failed_tasks_count: int
    queued_tasks_count: int
    running_tasks_count: int
    ocr_artifacts_count: int
    queue_lengths: list[AdminQueueStatus]


class AdminMeResponse(BaseModel):
    user: AdminUserRead
    admin: bool

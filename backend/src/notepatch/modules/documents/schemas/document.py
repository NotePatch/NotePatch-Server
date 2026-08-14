from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from notepatch.shared.schemas import ORMModel, metadata_field

DocumentStatus = Literal["created", "uploading", "uploaded", "scanning", "processing", "ready", "failed", "deleted"]
FileType = Literal["image", "pdf", "docx", "pptx", "audio", "video", "other"]
DocumentKind = Literal[
    "homework",
    "corrected_homework",
    "courseware",
    "note",
    "exam",
    "answer_key",
    "rubric",
    "chat_attachment",
    "other",
]
ArtifactType = Literal[
    "original",
    "converted_pdf",
    "deskewed_image",
    "binary_image",
    "ocr_json",
    "ocr_markdown",
    "ocr_text",
    "layout_json",
    "formula_json",
    "tables_json",
    "questions_json",
    "grading_report",
    "summary",
    "flashcards",
    "other",
]
UploadSessionStatus = Literal["created", "uploading", "completed", "failed", "cancelled"]


class DocumentArtifactRead(ORMModel):
    id: str
    workspace_id: str
    document_id: str
    artifact_type: str
    bucket: str
    object_key: str
    mime_type: str | None = None
    file_size: int | None = None
    metadata: dict = metadata_field()
    created_at: datetime


class DocumentRead(ORMModel):
    id: str
    workspace_id: str
    uploaded_by: str
    title: str | None = None
    original_filename: str
    mime_type: str | None = None
    file_size: int | None = None
    file_type: str
    document_kind: str
    storage_backend: str
    bucket: str
    object_key: str
    upload_id: str | None = None
    tus_upload_url: str | None = None
    sha256: str | None = None
    scan_status: str = "pending"
    scan_message: str | None = None
    scanned_at: datetime | None = None
    detected_mime_type: str | None = None
    status: str
    purge_status: str | None = None
    purge_task_id: str | None = None
    purged_at: datetime | None = None
    metadata: dict = metadata_field()
    created_at: datetime
    updated_at: datetime
    artifacts: list[DocumentArtifactRead] = Field(default_factory=list)


class UploadSessionRead(ORMModel):
    id: str
    workspace_id: str
    user_id: str
    document_id: str
    tus_upload_id: str | None = None
    tus_upload_url: str | None = None
    bucket: str
    object_key: str
    status: str
    expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class UploadSessionRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=512)
    mime_type: str | None = Field(default=None, max_length=255)
    file_size: int | None = Field(default=None, ge=0)
    document_kind: DocumentKind = "other"
    title: str | None = Field(default=None, max_length=255)
    metadata: dict = Field(default_factory=dict)


class UploadSessionResponse(BaseModel):
    document: DocumentRead
    upload_session: UploadSessionRead
    tus_endpoint: str
    tus_metadata: dict[str, str]
    tus_metadata_header: str
    bucket: str
    object_key: str


class CompleteUploadRequest(BaseModel):
    upload_session_id: str | None = None
    document_id: str | None = None
    tus_upload_id: str | None = None
    tus_upload_url: str | None = None
    file_size: int | None = Field(default=None, ge=0)
    mime_type: str | None = None
    metadata: dict = Field(default_factory=dict)


class DownloadUrlResponse(BaseModel):
    download_url: str
    expires_seconds: int


class DocumentDeleteResponse(BaseModel):
    ok: bool = True
    document_id: str
    status: Literal["deleted"] = "deleted"
    purge_status: Literal["queued", "running", "succeeded", "failed"]
    purge_task_id: str


class ArtifactDownloadUrlResponse(BaseModel):
    artifact_id: str
    document_id: str
    artifact_type: str
    filename: str
    mime_type: str | None = None
    expires_in: int
    download_url: str


class OcrArtifactRead(BaseModel):
    id: str
    artifact_type: str
    mime_type: str | None = None
    file_size: int | None = None
    created_at: datetime
    download_url: str | None = None


class OcrArtifactsResponse(BaseModel):
    document_id: str
    artifacts: list[OcrArtifactRead]


class ArtifactCreate(BaseModel):
    artifact_type: ArtifactType
    bucket: str | None = None
    object_key: str
    mime_type: str | None = None
    file_size: int | None = Field(default=None, ge=0)
    metadata: dict = Field(default_factory=dict)


class ProcessDocumentRequest(BaseModel):
    pipeline: list[str] | None = None
    options: dict = Field(default_factory=dict)

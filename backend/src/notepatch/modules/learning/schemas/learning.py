from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from notepatch.shared.schemas import ORMModel, metadata_field


class LearningUnitRead(ORMModel):
    id: str
    workspace_id: str
    title: str
    subject: str | None = None
    grade_level: str | None = None
    topic: str | None = None
    metadata: dict = metadata_field()
    created_at: datetime
    updated_at: datetime


class LearningUnitDocumentRead(BaseModel):
    id: str
    document_id: str
    role: str
    title: str | None = None
    original_filename: str
    document_kind: str
    file_type: str
    status: str
    created_at: datetime


class KnowledgeChunkRead(ORMModel):
    id: str
    workspace_id: str
    document_id: str | None = None
    subject: str | None = None
    grade_level: str | None = None
    source_type: str | None = None
    content: str
    embedding: list[float] | None = None
    metadata: dict = metadata_field()
    created_at: datetime


class StudyNoteVersionRead(ORMModel):
    id: str
    workspace_id: str
    learning_unit_id: str
    task_id: str | None = None
    version_no: int
    title: str
    markdown_object_key: str
    json_object_key: str
    highlighted_object_key: str | None = None
    highlight_map_object_key: str | None = None
    source_document_ids: list = Field(default_factory=list)
    source_mistake_ids: list = Field(default_factory=list)
    metadata: dict = metadata_field()
    created_at: datetime
    download_urls: dict[str, str] | None = None


class LearningUnitDetailResponse(BaseModel):
    learning_unit: LearningUnitRead
    documents: list[LearningUnitDocumentRead]
    latest_note: StudyNoteVersionRead | None = None


class StudyNoteDownloadUrlResponse(BaseModel):
    note_version_id: str
    learning_unit_id: str
    kind: Literal["markdown", "json", "highlighted", "highlight_map"]
    filename: str
    expires_in: int
    download_url: str

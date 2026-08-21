from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from notepatch.modules.learning.services.note_themes import (
    DEFAULT_NOTE_THEME_ID,
    NOTE_THEME_WRAPPER_CLASS,
    normalize_note_theme_id,
    note_theme_css_url,
)
from notepatch.shared.schemas import ORMModel, metadata_field


class LearningUnitRead(ORMModel):
    id: str
    workspace_id: str
    title: str
    subject: str | None = None
    grade_level: str | None = None
    topic: str | None = None
    metadata: dict = metadata_field()
    knowledge_revision: int = 0
    attempt_revision: int = 0
    notes_generated_revision: int = 0
    note_generation_due_at: datetime | None = None
    merge_status: str | None = None
    merged_into_id: str | None = None
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


class StudyNoteRendering(BaseModel):
    theme_id: str = DEFAULT_NOTE_THEME_ID
    css_url: str = Field(default_factory=lambda: note_theme_css_url(DEFAULT_NOTE_THEME_ID))
    wrapper_class: str = NOTE_THEME_WRAPPER_CLASS


class StudyNoteVersionRead(ORMModel):
    id: str
    workspace_id: str
    learning_unit_id: str
    task_id: str | None = None
    version_no: int
    title: str
    html_object_key: str
    json_object_key: str
    note_ir_object_key: str | None = None
    content_edit_level: str = "conceptual"
    layout_edit_level: str = "minor"
    highlighted_html_object_key: str | None = None
    highlight_map_object_key: str | None = None
    knowledge_point_ids: list = Field(default_factory=list)
    source_document_ids: list = Field(default_factory=list)
    source_mistake_ids: list = Field(default_factory=list)
    source_version_id: str | None = None
    edited_by_user_id: str | None = None
    edit_origin: str | None = None
    edit_summary: str | None = None
    metadata: dict = metadata_field()
    created_at: datetime
    download_urls: dict[str, str] | None = None
    rendering: StudyNoteRendering = Field(default_factory=StudyNoteRendering)

    @model_validator(mode="after")
    def resolve_rendering_theme(self) -> "StudyNoteVersionRead":
        theme_id = normalize_note_theme_id(self.metadata.get("theme_id"))
        self.rendering = StudyNoteRendering(
            theme_id=theme_id,
            css_url=note_theme_css_url(theme_id),
            wrapper_class=NOTE_THEME_WRAPPER_CLASS,
        )
        return self


class LearningUnitDetailResponse(BaseModel):
    learning_unit: LearningUnitRead
    documents: list[LearningUnitDocumentRead]
    latest_note: StudyNoteVersionRead | None = None


class StudyNoteDownloadUrlResponse(BaseModel):
    note_version_id: str
    learning_unit_id: str
    kind: Literal["html", "json", "highlighted_html", "highlight_map", "rendered_html"]
    filename: str
    expires_in: int
    download_url: str


class StudyNoteRevisionCreate(BaseModel):
    html: str = Field(min_length=1, max_length=2_000_000)
    title: str | None = Field(default=None, min_length=1, max_length=255)
    edit_summary: str | None = Field(default=None, max_length=500)


class StudyNoteRevisionResponse(BaseModel):
    note: StudyNoteVersionRead
    downstream_tasks: list[dict] = Field(default_factory=list)


class LearningUnitMergeRequest(BaseModel):
    source_learning_unit_ids: list[str] = Field(min_length=1, max_length=50)


class FlashcardRead(ORMModel):
    id: str
    knowledge_point_id: str
    front: str
    back: str
    priority_score: float
    priority_factors: dict = Field(default_factory=dict)
    source_refs: list = Field(default_factory=list)
    difficulty: str | None = None
    rank: int
    created_at: datetime


class FlashcardDeckRead(ORMModel):
    id: str
    workspace_id: str
    learning_unit_id: str
    study_note_version_id: str
    task_id: str | None = None
    version_no: int
    attempt_revision: int
    weighting_config: dict = Field(default_factory=dict)
    metadata: dict = metadata_field()
    created_at: datetime


class FlashcardDeckDetail(BaseModel):
    deck: FlashcardDeckRead
    cards: list[FlashcardRead]

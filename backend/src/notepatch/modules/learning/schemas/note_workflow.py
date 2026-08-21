from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from notepatch.shared.schemas import ORMModel, metadata_field

ContentEditLevel = Literal["verbatim", "spelling", "conceptual", "rewrite"]
LayoutEditLevel = Literal["preserve", "minor", "reorder", "reflow"]


class NoteSetCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    expected_page_count: int = Field(ge=1, le=200)
    learning_unit_id: str | None = None
    subject: str | None = Field(default=None, max_length=64)
    grade_level: str | None = Field(default=None, max_length=64)
    topic: str | None = Field(default=None, max_length=255)
    content_edit_level: ContentEditLevel | None = None
    layout_edit_level: LayoutEditLevel | None = None


class NoteSetDocumentRead(ORMModel):
    id: str
    document_id: str
    page_index: int
    created_at: datetime


class NoteSetRead(ORMModel):
    id: str
    workspace_id: str
    user_id: str
    learning_unit_id: str | None = None
    title: str
    expected_page_count: int
    status: str
    content_edit_level: str
    layout_edit_level: str
    metadata: dict = metadata_field()
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    documents: list[NoteSetDocumentRead] = Field(default_factory=list)


class StudyNoteGenerateRequest(BaseModel):
    content_edit_level: ContentEditLevel | None = None
    layout_edit_level: LayoutEditLevel | None = None
    force_reprocess: bool = False


class StudyNoteCorrectionRead(ORMModel):
    id: str
    source_block_id: str | None = None
    correction_type: str
    original_text: str
    corrected_text: str
    reason: str | None = None
    confidence: float | None = None
    source_refs: list = Field(default_factory=list)
    created_at: datetime


class NoteGapRead(ORMModel):
    id: str
    workspace_id: str
    learning_unit_id: str
    knowledge_point_id: str
    note_version_id: str | None = None
    accepted_version_id: str | None = None
    status: str
    coverage_score: float
    source_refs: list = Field(default_factory=list)
    target_section_id: str | None = None
    target_anchor: str | None = None
    insert_position: str
    metadata: dict = metadata_field()
    created_at: datetime
    updated_at: datetime


class NoteDraftCreate(BaseModel):
    selected_source_refs: list[dict] = Field(default_factory=list)
    target_section_id: str | None = Field(default=None, max_length=255)
    insert_position: Literal["before", "after", "inside"] = "after"
    instruction: str | None = Field(default=None, max_length=2000)


class NoteDraftUpdate(BaseModel):
    html: str | None = Field(default=None, max_length=500_000)
    target_section_id: str | None = Field(default=None, max_length=255)
    insert_position: Literal["before", "after", "inside"] | None = None


class NoteDraftRegenerate(BaseModel):
    feedback: str = Field(min_length=1, max_length=2000)


class NoteSupplementDraftRead(ORMModel):
    id: str
    workspace_id: str
    learning_unit_id: str
    gap_suggestion_id: str
    base_note_version_id: str | None = None
    generated_by_task_id: str | None = None
    version_no: int
    status: str
    html: str
    selected_source_refs: list = Field(default_factory=list)
    target_section_id: str | None = None
    target_anchor: str | None = None
    insert_position: str
    instruction: str | None = None
    feedback: str | None = None
    created_at: datetime
    updated_at: datetime


class NoteGapDetail(BaseModel):
    suggestion: NoteGapRead
    drafts: list[NoteSupplementDraftRead] = Field(default_factory=list)


class NotesFromGapsRequest(BaseModel):
    gap_ids: list[str] = Field(min_length=1, max_length=100)
    title: str | None = Field(default=None, max_length=255)
    content_edit_level: ContentEditLevel | None = None
    layout_edit_level: LayoutEditLevel | None = None

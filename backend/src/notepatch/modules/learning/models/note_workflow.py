import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from notepatch.platform.database import Base, utcnow


class NoteSet(Base):
    __tablename__ = "note_sets"
    __table_args__ = (
        Index("ix_note_sets_workspace_status", "workspace_id", "status"),
        Index("ix_note_sets_workspace_unit", "workspace_id", "learning_unit_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id: Mapped[str] = mapped_column(String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    learning_unit_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("learning_units.id", ondelete="SET NULL"), nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    expected_page_count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="open", nullable=False)
    content_edit_level: Mapped[str] = mapped_column(String(32), nullable=False)
    layout_edit_level: Mapped[str] = mapped_column(String(32), nullable=False)
    metadata_: Mapped[dict] = mapped_column("metadata", JSON, default=dict, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class NoteSetDocument(Base):
    __tablename__ = "note_set_documents"
    __table_args__ = (
        UniqueConstraint("note_set_id", "page_index", name="uq_note_set_documents_page"),
        UniqueConstraint("workspace_id", "document_id", name="uq_note_set_documents_document"),
        Index("ix_note_set_documents_workspace_set", "workspace_id", "note_set_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id: Mapped[str] = mapped_column(String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    note_set_id: Mapped[str] = mapped_column(String(36), ForeignKey("note_sets.id", ondelete="CASCADE"), nullable=False)
    document_id: Mapped[str] = mapped_column(String(36), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    page_index: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class StudyNoteCorrection(Base):
    __tablename__ = "study_note_corrections"
    __table_args__ = (
        Index("ix_study_note_corrections_workspace_version", "workspace_id", "note_version_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id: Mapped[str] = mapped_column(String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    learning_unit_id: Mapped[str] = mapped_column(String(36), ForeignKey("learning_units.id", ondelete="CASCADE"), nullable=False)
    note_version_id: Mapped[str] = mapped_column(String(36), ForeignKey("study_note_versions.id", ondelete="CASCADE"), nullable=False)
    source_block_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    correction_type: Mapped[str] = mapped_column(String(32), nullable=False)
    original_text: Mapped[str] = mapped_column(Text, nullable=False)
    corrected_text: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_refs: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class NoteGapSuggestion(Base):
    __tablename__ = "note_gap_suggestions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "learning_unit_id", "knowledge_point_id", "note_version_id", name="uq_note_gap_source_version"),
        Index("ix_note_gaps_workspace_unit_status", "workspace_id", "learning_unit_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id: Mapped[str] = mapped_column(String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    learning_unit_id: Mapped[str] = mapped_column(String(36), ForeignKey("learning_units.id", ondelete="CASCADE"), nullable=False)
    knowledge_point_id: Mapped[str] = mapped_column(String(36), ForeignKey("knowledge_points.id", ondelete="CASCADE"), nullable=False)
    note_version_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("study_note_versions.id", ondelete="SET NULL"), nullable=True)
    detected_by_task_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    accepted_version_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("study_note_versions.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    coverage_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    source_refs: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    target_section_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    target_anchor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    insert_position: Mapped[str] = mapped_column(String(16), default="after", nullable=False)
    metadata_: Mapped[dict] = mapped_column("metadata", JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class NoteSupplementDraft(Base):
    __tablename__ = "note_supplement_drafts"
    __table_args__ = (
        UniqueConstraint("gap_suggestion_id", "version_no", name="uq_note_supplement_gap_version"),
        Index("ix_note_supplement_workspace_gap", "workspace_id", "gap_suggestion_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id: Mapped[str] = mapped_column(String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    learning_unit_id: Mapped[str] = mapped_column(String(36), ForeignKey("learning_units.id", ondelete="CASCADE"), nullable=False)
    gap_suggestion_id: Mapped[str] = mapped_column(String(36), ForeignKey("note_gap_suggestions.id", ondelete="CASCADE"), nullable=False)
    base_note_version_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("study_note_versions.id", ondelete="SET NULL"), nullable=True)
    generated_by_task_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)
    html: Mapped[str] = mapped_column(Text, default="", nullable=False)
    selected_source_refs: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    target_section_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    target_anchor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    insert_position: Mapped[str] = mapped_column(String(16), default="after", nullable=False)
    instruction: Mapped[str | None] = mapped_column(Text, nullable=True)
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

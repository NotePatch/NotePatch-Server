import uuid
from datetime import datetime

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from notepatch.platform.database import Base, utcnow


class LearningUnit(Base):
    __tablename__ = "learning_units"
    __table_args__ = (
        Index("ix_learning_units_workspace_id", "workspace_id"),
        Index("ix_learning_units_workspace_id_id", "workspace_id", "id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(64), nullable=True)
    grade_level: Mapped[str | None] = mapped_column(String(64), nullable=True)
    topic: Mapped[str | None] = mapped_column(String(255), nullable=True)
    metadata_: Mapped[dict] = mapped_column("metadata", JSON, default=dict, nullable=False)
    knowledge_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    attempt_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    notes_generated_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    note_generation_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    merge_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    merged_into_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("learning_units.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class LearningUnitDocument(Base):
    __tablename__ = "learning_unit_documents"
    __table_args__ = (
        UniqueConstraint("learning_unit_id", "document_id", name="uq_learning_unit_documents_unit_document"),
        Index("ix_learning_unit_documents_workspace_id", "workspace_id"),
        Index("ix_learning_unit_documents_learning_unit_id", "learning_unit_id"),
        Index("ix_learning_unit_documents_document_id", "document_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    learning_unit_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("learning_units.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[str] = mapped_column(String(36), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class StudyNoteVersion(Base):
    __tablename__ = "study_note_versions"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "learning_unit_id",
            "version_no",
            name="uq_study_note_versions_workspace_unit_version",
        ),
        Index("ix_study_note_versions_workspace_id", "workspace_id"),
        Index("ix_study_note_versions_learning_unit_id", "learning_unit_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    learning_unit_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("learning_units.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("tasks.id"), nullable=True)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    html_object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    json_object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    note_ir_object_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    content_edit_level: Mapped[str] = mapped_column(String(32), default="conceptual", nullable=False)
    layout_edit_level: Mapped[str] = mapped_column(String(32), default="minor", nullable=False)
    highlighted_html_object_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    highlight_map_object_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    knowledge_point_ids: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    source_document_ids: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    source_mistake_ids: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    source_version_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("study_note_versions.id", ondelete="SET NULL"), nullable=True
    )
    edited_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    edit_origin: Mapped[str | None] = mapped_column(String(32), nullable=True)
    edit_summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    metadata_: Mapped[dict] = mapped_column("metadata", JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class KnowledgePoint(Base):
    __tablename__ = "knowledge_points"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "learning_unit_id",
            "normalized_name",
            name="uq_knowledge_points_workspace_unit_name",
        ),
        Index("ix_knowledge_points_workspace_unit", "workspace_id", "learning_unit_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    learning_unit_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("learning_units.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(255), nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(VECTOR(1024), nullable=True)
    source_document_ids: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    metadata_: Mapped[dict] = mapped_column("metadata", JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class KnowledgePointAttempt(Base):
    __tablename__ = "knowledge_point_attempts"
    __table_args__ = (
        UniqueConstraint(
            "grading_result_id",
            "question_id",
            "knowledge_point_id",
            name="uq_knowledge_point_attempt_grade_question_point",
        ),
        Index("ix_knowledge_point_attempts_workspace_unit", "workspace_id", "learning_unit_id"),
        Index("ix_knowledge_point_attempts_point_time", "knowledge_point_id", "occurred_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    learning_unit_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("learning_units.id", ondelete="CASCADE"), nullable=False
    )
    knowledge_point_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_points.id", ondelete="CASCADE"), nullable=False
    )
    student_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    homework_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("homeworks.id", ondelete="CASCADE"), nullable=True
    )
    grading_result_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("grading_results.id", ondelete="CASCADE"), nullable=True
    )
    question_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("questions.id", ondelete="SET NULL"), nullable=True
    )
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    score_ratio: Mapped[float] = mapped_column(Float, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    metadata_: Mapped[dict] = mapped_column("metadata", JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class FlashcardDeck(Base):
    __tablename__ = "flashcard_decks"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "learning_unit_id",
            "version_no",
            name="uq_flashcard_decks_workspace_unit_version",
        ),
        UniqueConstraint(
            "workspace_id",
            "learning_unit_id",
            "study_note_version_id",
            "attempt_revision",
            name="uq_flashcard_decks_source_revision",
        ),
        Index("ix_flashcard_decks_workspace_unit", "workspace_id", "learning_unit_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    learning_unit_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("learning_units.id", ondelete="CASCADE"), nullable=False
    )
    study_note_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("study_note_versions.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    weighting_config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    metadata_: Mapped[dict] = mapped_column("metadata", JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Flashcard(Base):
    __tablename__ = "flashcards"
    __table_args__ = (
        Index("ix_flashcards_workspace_deck", "workspace_id", "deck_id"),
        Index("ix_flashcards_knowledge_point", "knowledge_point_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    deck_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("flashcard_decks.id", ondelete="CASCADE"), nullable=False
    )
    knowledge_point_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_points.id", ondelete="CASCADE"), nullable=False
    )
    front: Mapped[str] = mapped_column(Text, nullable=False)
    back: Mapped[str] = mapped_column(Text, nullable=False)
    priority_score: Mapped[float] = mapped_column(Float, nullable=False)
    priority_factors: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    source_refs: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    difficulty: Mapped[str | None] = mapped_column(String(32), nullable=True)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

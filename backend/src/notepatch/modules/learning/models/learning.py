import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
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
    markdown_object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    json_object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    highlighted_object_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    highlight_map_object_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    source_document_ids: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    source_mistake_ids: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    metadata_: Mapped[dict] = mapped_column("metadata", JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

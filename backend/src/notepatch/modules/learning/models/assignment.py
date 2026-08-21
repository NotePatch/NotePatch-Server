import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from notepatch.platform.database import Base, utcnow


class LearningUnitAssignment(Base):
    __tablename__ = "learning_unit_assignments"
    __table_args__ = (
        UniqueConstraint("workspace_id", "document_id", name="uq_learning_unit_assignments_document"),
        Index("ix_learning_unit_assignments_workspace_unit", "workspace_id", "learning_unit_id"),
        Index("ix_learning_unit_assignments_workspace_status", "workspace_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    learning_unit_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("learning_units.id", ondelete="CASCADE"), nullable=False
    )
    method: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="assigned", nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    candidate_scores: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

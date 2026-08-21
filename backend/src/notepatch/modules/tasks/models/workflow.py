import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from notepatch.platform.database import Base, utcnow


WORKFLOW_STATUSES = {
    "waiting_upload",
    "queued",
    "running",
    "waiting",
    "succeeded",
    "partially_succeeded",
    "failed",
    "cancelled",
}
WORKFLOW_PHASE_STATUSES = {
    "not_started",
    "queued",
    "running",
    "waiting",
    "succeeded",
    "failed",
    "cancelled",
    "not_applicable",
}


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"
    __table_args__ = (
        Index("ix_workflow_runs_workspace_id", "workspace_id"),
        Index("ix_workflow_runs_workspace_document", "workspace_id", "document_id"),
        Index("ix_workflow_runs_workspace_unit", "workspace_id", "learning_unit_id"),
        Index("ix_workflow_runs_workspace_status", "workspace_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    document_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("documents.id", ondelete="SET NULL"), nullable=True
    )
    learning_unit_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("learning_units.id", ondelete="SET NULL"), nullable=True
    )
    trigger_type: Mapped[str] = mapped_column(String(32), default="upload", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="waiting_upload", nullable=False)
    core_status: Mapped[str] = mapped_column(String(32), default="not_started", nullable=False)
    enrichment_status: Mapped[str] = mapped_column(String(32), default="not_applicable", nullable=False)
    current_stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    waiting_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    metadata_: Mapped[dict] = mapped_column("metadata", JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkflowTaskLink(Base):
    __tablename__ = "workflow_task_links"
    __table_args__ = (
        UniqueConstraint("workflow_run_id", "task_id", name="uq_workflow_task_links_run_task"),
        Index("ix_workflow_task_links_task_id", "task_id"),
        Index("ix_workflow_task_links_workflow_run_id", "workflow_run_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workflow_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    phase: Mapped[str] = mapped_column(String(16), nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class WorkflowEvent(Base):
    __tablename__ = "workflow_events"
    __table_args__ = (
        UniqueConstraint("workflow_run_id", "sequence_no", name="uq_workflow_events_run_sequence"),
        Index("ix_workflow_events_workspace_run", "workspace_id", "workflow_run_id"),
        Index("ix_workflow_events_task_id", "task_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    workflow_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    task_event_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("task_events.id", ondelete="SET NULL"), nullable=True
    )
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    level: Mapped[str] = mapped_column(String(16), default="info", nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    progress: Mapped[int | None] = mapped_column(Integer, nullable=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

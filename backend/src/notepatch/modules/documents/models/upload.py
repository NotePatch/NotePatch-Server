import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from notepatch.platform.database import Base, utcnow


UPLOAD_SESSION_STATUSES = {"created", "uploading", "completed", "failed", "cancelled"}


class UploadSession(Base):
    __tablename__ = "upload_sessions"
    __table_args__ = (
        Index("ix_upload_sessions_workspace_id_id", "workspace_id", "id"),
        Index("ix_upload_sessions_workspace_id_document_id", "workspace_id", "document_id"),
        Index("ix_upload_sessions_tus_upload_id", "tus_upload_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    document_id: Mapped[str] = mapped_column(String(36), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    tus_upload_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tus_upload_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="created", nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    document: Mapped["Document"] = relationship()
    user: Mapped["User"] = relationship()

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from notepatch.platform.database import Base, utcnow


DOCUMENT_STATUSES = {"created", "uploading", "uploaded", "scanning", "processing", "ready", "failed", "deleted"}
FILE_TYPES = {"image", "pdf", "docx", "pptx", "audio", "video", "other"}
CHAT_ATTACHMENT_KIND = "chat_attachment"
AUTO_LEARNING_DOCUMENT_KINDS = {
    "homework",
    "corrected_homework",
    "courseware",
    "note",
    "exam",
    "answer_key",
    "rubric",
}
DOCUMENT_KINDS = AUTO_LEARNING_DOCUMENT_KINDS | {CHAT_ATTACHMENT_KIND, "other"}
DOCUMENT_RETENTION_SCOPES = {"workspace", "conversation"}
ARTIFACT_TYPES = {
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
}


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_documents_workspace_id", "workspace_id"),
        Index("ix_documents_workspace_id_id", "workspace_id", "id"),
        Index("ix_documents_purge_status", "purge_status"),
        Index("ix_documents_workspace_retention", "workspace_id", "retention_scope", "status"),
        Index("ix_documents_chat_conversation_id", "chat_conversation_id"),
        Index("ix_documents_latest_workflow_run_id", "latest_workflow_run_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    uploaded_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    file_type: Mapped[str] = mapped_column(String(32), default="other", nullable=False)
    document_kind: Mapped[str] = mapped_column(String(64), default="other", nullable=False)
    retention_scope: Mapped[str] = mapped_column(String(32), default="workspace", nullable=False)
    chat_conversation_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("chat_conversations.id", ondelete="SET NULL"), nullable=True
    )
    storage_backend: Mapped[str] = mapped_column(String(64), default="seaweedfs", nullable=False)
    bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    upload_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tus_upload_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scan_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    scan_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    detected_mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="created", nullable=False)
    purge_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    purge_task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latest_workflow_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("workflow_runs.id", ondelete="SET NULL", use_alter=True), nullable=True
    )
    metadata_: Mapped[dict] = mapped_column("metadata", JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    artifacts: Mapped[list["DocumentArtifact"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def save_to_documents(self) -> bool:
        return self.retention_scope == "workspace"


class DocumentArtifact(Base):
    __tablename__ = "document_artifacts"
    __table_args__ = (
        Index("ix_document_artifacts_workspace_id", "workspace_id"),
        Index("ix_document_artifacts_workspace_id_document_id", "workspace_id", "document_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[str] = mapped_column(String(36), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    artifact_type: Mapped[str] = mapped_column(String(64), nullable=False)
    bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metadata_: Mapped[dict] = mapped_column("metadata", JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    document: Mapped[Document] = relationship(back_populates="artifacts")

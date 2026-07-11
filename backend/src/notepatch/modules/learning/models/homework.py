import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from notepatch.platform.database import Base, utcnow


class Homework(Base):
    __tablename__ = "homeworks"
    __table_args__ = (
        Index("ix_homeworks_workspace_id", "workspace_id"),
        Index("ix_homeworks_workspace_id_id", "workspace_id", "id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    document_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("documents.id"), nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)
    rubric_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    max_score: Mapped[float] = mapped_column(Float, default=100.0, nullable=False)
    metadata_: Mapped[dict] = mapped_column("metadata", JSON, default=dict, nullable=False)
    created_by_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class Question(Base):
    __tablename__ = "questions"
    __table_args__ = (
        Index("ix_questions_workspace_id", "workspace_id"),
        Index("ix_questions_workspace_id_document_id", "workspace_id", "document_id"),
        Index("ix_questions_workspace_id_homework_id", "workspace_id", "homework_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("documents.id"), nullable=True)
    homework_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("homeworks.id"), nullable=True)
    sequence_no: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    question_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_: Mapped[dict] = mapped_column("metadata", JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class GradingResult(Base):
    __tablename__ = "grading_results"
    __table_args__ = (
        Index("ix_grading_results_workspace_id", "workspace_id"),
        Index("ix_grading_results_workspace_id_homework_id", "workspace_id", "homework_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    homework_id: Mapped[str] = mapped_column(String(36), ForeignKey("homeworks.id"), nullable=False)
    question_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("questions.id"), nullable=True)
    student_user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    grading_mode: Mapped[str] = mapped_column(String(32), default="provisional", nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_storage_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    metadata_: Mapped[dict] = mapped_column("metadata", JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Mistake(Base):
    __tablename__ = "mistakes"
    __table_args__ = (
        Index("ix_mistakes_workspace_id", "workspace_id"),
        Index("ix_mistakes_workspace_id_id", "workspace_id", "id"),
        Index("ix_mistakes_workspace_id_student_user_id", "workspace_id", "student_user_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    question_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("questions.id"), nullable=True)
    grading_result_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("grading_results.id"), nullable=True)
    student_user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    subject: Mapped[str | None] = mapped_column(String(64), nullable=True)
    knowledge_point: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="open", nullable=False)
    metadata_: Mapped[dict] = mapped_column("metadata", JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class HomeworkReference(Base):
    __tablename__ = "homework_references"
    __table_args__ = (
        UniqueConstraint(
            "homework_id",
            "document_id",
            "reference_type",
            name="uq_homework_references_homework_document_type",
        ),
        Index("ix_homework_references_workspace_id_homework_id", "workspace_id", "homework_id"),
        Index("ix_homework_references_workspace_id_document_id", "workspace_id", "document_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    homework_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("homeworks.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    reference_type: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

"""real pipeline foundation

Revision ID: 202607100003
Revises: 202607100002
Create Date: 2026-07-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import VECTOR


revision: str = "202607100003"
down_revision: str | None = "202607100002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.drop_column("knowledge_chunks", "embedding")
    op.add_column("knowledge_chunks", sa.Column("embedding", VECTOR(dim=1024), nullable=True))
    op.create_index(
        "ix_knowledge_chunks_embedding_hnsw",
        "knowledge_chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )

    op.add_column("homeworks", sa.Column("rubric_text", sa.Text(), nullable=True))
    op.add_column(
        "homeworks",
        sa.Column("max_score", sa.Float(), nullable=False, server_default=sa.text("100")),
    )
    op.add_column("grading_results", sa.Column("max_score", sa.Float(), nullable=True))
    op.add_column(
        "grading_results",
        sa.Column("grading_mode", sa.String(length=32), nullable=False, server_default="provisional"),
    )
    op.add_column("grading_results", sa.Column("confidence", sa.Float(), nullable=True))

    op.create_table(
        "homework_references",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("homework_id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("reference_type", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["homework_id"], ["homeworks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "homework_id",
            "document_id",
            "reference_type",
            name="uq_homework_references_homework_document_type",
        ),
    )
    op.create_index(
        "ix_homework_references_workspace_id_homework_id",
        "homework_references",
        ["workspace_id", "homework_id"],
    )
    op.create_index(
        "ix_homework_references_workspace_id_document_id",
        "homework_references",
        ["workspace_id", "document_id"],
    )

    op.add_column("tasks", sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("tasks", sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"))
    op.add_column("tasks", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_tasks_next_attempt_at", "tasks", ["next_attempt_at"])


def downgrade() -> None:
    op.drop_index("ix_tasks_next_attempt_at", table_name="tasks")
    op.drop_column("tasks", "next_attempt_at")
    op.drop_column("tasks", "max_attempts")
    op.drop_column("tasks", "attempt")

    op.drop_table("homework_references")
    op.drop_column("grading_results", "confidence")
    op.drop_column("grading_results", "grading_mode")
    op.drop_column("grading_results", "max_score")
    op.drop_column("homeworks", "max_score")
    op.drop_column("homeworks", "rubric_text")

    op.drop_index("ix_knowledge_chunks_embedding_hnsw", table_name="knowledge_chunks")
    op.drop_column("knowledge_chunks", "embedding")
    op.add_column("knowledge_chunks", sa.Column("embedding", sa.JSON(), nullable=True))

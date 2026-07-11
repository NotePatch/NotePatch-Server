"""learning workflow

Revision ID: 202607100001
Revises: 202607090002
Create Date: 2026-07-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "202607100001"
down_revision: str | None = "202607090002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "learning_units",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("subject", sa.String(length=64), nullable=True),
        sa.Column("grade_level", sa.String(length=64), nullable=True),
        sa.Column("topic", sa.String(length=255), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_learning_units_workspace_id", "learning_units", ["workspace_id"])
    op.create_index("ix_learning_units_workspace_id_id", "learning_units", ["workspace_id", "id"])

    op.create_table(
        "learning_unit_documents",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("learning_unit_id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["learning_unit_id"], ["learning_units.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("learning_unit_id", "document_id", name="uq_learning_unit_documents_unit_document"),
    )
    op.create_index("ix_learning_unit_documents_document_id", "learning_unit_documents", ["document_id"])
    op.create_index("ix_learning_unit_documents_learning_unit_id", "learning_unit_documents", ["learning_unit_id"])
    op.create_index("ix_learning_unit_documents_workspace_id", "learning_unit_documents", ["workspace_id"])

    op.create_table(
        "study_note_versions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("learning_unit_id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=True),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("markdown_object_key", sa.String(length=1024), nullable=False),
        sa.Column("json_object_key", sa.String(length=1024), nullable=False),
        sa.Column("highlighted_object_key", sa.String(length=1024), nullable=True),
        sa.Column("highlight_map_object_key", sa.String(length=1024), nullable=True),
        sa.Column("source_document_ids", sa.JSON(), nullable=False),
        sa.Column("source_mistake_ids", sa.JSON(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["learning_unit_id"], ["learning_units.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_study_note_versions_learning_unit_id", "study_note_versions", ["learning_unit_id"])
    op.create_index("ix_study_note_versions_workspace_id", "study_note_versions", ["workspace_id"])


def downgrade() -> None:
    op.drop_table("study_note_versions")
    op.drop_table("learning_unit_documents")
    op.drop_table("learning_units")

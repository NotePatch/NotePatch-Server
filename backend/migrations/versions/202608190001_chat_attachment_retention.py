"""add conversation-scoped chat attachment retention

Revision ID: 202608190001
Revises: 202608140002
Create Date: 2026-08-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "202608190001"
down_revision: str | None = "202608140002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column(
            "retention_scope",
            sa.String(length=32),
            server_default="workspace",
            nullable=False,
        ),
    )
    op.add_column(
        "documents",
        sa.Column("chat_conversation_id", sa.String(length=36), nullable=True),
    )
    op.create_foreign_key(
        "fk_documents_chat_conversation_id",
        "documents",
        "chat_conversations",
        ["chat_conversation_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_documents_workspace_retention",
        "documents",
        ["workspace_id", "retention_scope", "status"],
    )
    op.create_index(
        "ix_documents_chat_conversation_id",
        "documents",
        ["chat_conversation_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_documents_chat_conversation_id", table_name="documents")
    op.drop_index("ix_documents_workspace_retention", table_name="documents")
    op.drop_constraint("fk_documents_chat_conversation_id", "documents", type_="foreignkey")
    op.drop_column("documents", "chat_conversation_id")
    op.drop_column("documents", "retention_scope")

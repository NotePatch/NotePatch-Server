"""auth rotation and chat source integrity

Revision ID: 202607120001
Revises: 202607110002
Create Date: 2026-07-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "202607120001"
down_revision: str | None = "202607110002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("refresh_tokens", sa.Column("family_id", sa.String(length=36), nullable=True))
    op.add_column("refresh_tokens", sa.Column("parent_token_id", sa.String(length=36), nullable=True))
    op.add_column("refresh_tokens", sa.Column("revoked_reason", sa.String(length=32), nullable=True))
    op.execute("UPDATE refresh_tokens SET family_id = id WHERE family_id IS NULL")
    op.alter_column("refresh_tokens", "family_id", nullable=False)
    op.create_index("ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"])
    op.create_foreign_key(
        "fk_refresh_tokens_parent_token_id",
        "refresh_tokens",
        "refresh_tokens",
        ["parent_token_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.add_column(
        "chat_messages",
        sa.Column("citations", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
    )
    op.add_column(
        "chat_messages",
        sa.Column("source_status", sa.String(length=32), server_default="available", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("chat_messages", "source_status")
    op.drop_column("chat_messages", "citations")
    op.drop_constraint("fk_refresh_tokens_parent_token_id", "refresh_tokens", type_="foreignkey")
    op.drop_index("ix_refresh_tokens_family_id", table_name="refresh_tokens")
    op.drop_column("refresh_tokens", "revoked_reason")
    op.drop_column("refresh_tokens", "parent_token_id")
    op.drop_column("refresh_tokens", "family_id")

"""track OpenClaw-generated conversation titles

Revision ID: 202608190002
Revises: 202608190001
Create Date: 2026-08-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "202608190002"
down_revision: str | None = "202608190001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing titles may have been renamed manually, so never auto-replace them.
    op.add_column(
        "chat_conversations",
        sa.Column(
            "title_source",
            sa.String(length=16),
            server_default="manual",
            nullable=False,
        ),
    )
    op.add_column(
        "chat_conversations",
        sa.Column("title_generated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("chat_conversations", "title_generated_at")
    op.drop_column("chat_conversations", "title_source")

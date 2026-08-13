"""add user AI model selection

Revision ID: 202607230001
Revises: 202607120002
Create Date: 2026-07-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "202607230001"
down_revision: str | None = "202607120002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("preferred_ai_model", sa.String(length=255), nullable=True))
    op.add_column("chat_messages", sa.Column("model_id", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_messages", "model_id")
    op.drop_column("users", "preferred_ai_model")

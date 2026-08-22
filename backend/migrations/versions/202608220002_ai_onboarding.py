"""add AI onboarding preferences

Revision ID: 202608220002
Revises: 202608220001
Create Date: 2026-08-22
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "202608220002"
down_revision: str | None = "202608220001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("ai_onboarding_version", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "users",
        sa.Column("ai_onboarding_completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("ai_preferences", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
    )
    op.create_check_constraint(
        "ck_users_ai_onboarding_version",
        "users",
        "ai_onboarding_version >= 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_users_ai_onboarding_version", "users", type_="check")
    op.drop_column("users", "ai_preferences")
    op.drop_column("users", "ai_onboarding_completed_at")
    op.drop_column("users", "ai_onboarding_version")

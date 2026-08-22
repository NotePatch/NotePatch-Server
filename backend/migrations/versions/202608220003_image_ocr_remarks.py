"""add OCR-based image remarks

Revision ID: 202608220003
Revises: 202608220002
Create Date: 2026-08-22
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "202608220003"
down_revision: str | None = "202608220002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("auto_image_remark_enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
    )
    op.add_column("documents", sa.Column("remark", sa.String(length=255), nullable=True))
    op.add_column("documents", sa.Column("remark_source", sa.String(length=32), nullable=True))
    op.execute(
        """
        UPDATE documents
        SET remark = COALESCE(metadata->'ai_image_naming'->>'name', title, original_filename),
            remark_source = CASE
                WHEN metadata->'ai_image_naming'->>'name' IS NOT NULL THEN 'legacy_ai'
                WHEN title IS NOT NULL THEN 'legacy_title'
                ELSE 'original_filename'
            END
        WHERE status <> 'deleted'
        """
    )
    op.execute("UPDATE tasks SET task_type = 'generate_image_remark' WHERE task_type = 'name_image'")


def downgrade() -> None:
    op.execute("UPDATE tasks SET task_type = 'name_image' WHERE task_type = 'generate_image_remark'")
    op.drop_column("documents", "remark_source")
    op.drop_column("documents", "remark")
    op.drop_column("users", "auto_image_remark_enabled")

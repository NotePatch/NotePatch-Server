"""mutation scope and purge state

Revision ID: 202607110001
Revises: 202607100003
Create Date: 2026-07-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "202607110001"
down_revision: str | None = "202607100003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("purge_status", sa.String(length=32), nullable=True))
    op.add_column("documents", sa.Column("purge_task_id", sa.String(length=36), nullable=True))
    op.add_column("documents", sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_documents_purge_status", "documents", ["purge_status"])
    op.add_column("tasks", sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_tasks_cancel_requested_at", "tasks", ["cancel_requested_at"])


def downgrade() -> None:
    op.drop_index("ix_tasks_cancel_requested_at", table_name="tasks")
    op.drop_column("tasks", "cancel_requested_at")
    op.drop_index("ix_documents_purge_status", table_name="documents")
    op.drop_column("documents", "purged_at")
    op.drop_column("documents", "purge_task_id")
    op.drop_column("documents", "purge_status")

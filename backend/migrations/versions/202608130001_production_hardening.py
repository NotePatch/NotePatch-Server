"""production hardening and workflow metadata

Revision ID: 202608130001
Revises: 202607230001
Create Date: 2026-08-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "202608130001"
down_revision: str | None = "202607230001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("scan_status", sa.String(length=32), nullable=False, server_default="clean"))
    op.add_column("documents", sa.Column("scan_message", sa.String(length=500), nullable=True))
    op.add_column("documents", sa.Column("scanned_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("documents", sa.Column("detected_mime_type", sa.String(length=255), nullable=True))
    op.add_column("learning_units", sa.Column("merge_status", sa.String(length=32), nullable=True))
    op.add_column("learning_units", sa.Column("merged_into_id", sa.String(length=36), sa.ForeignKey("learning_units.id", ondelete="SET NULL"), nullable=True))
    op.add_column("task_events", sa.Column("sequence_no", sa.BigInteger(), nullable=True))
    op.execute("""
        WITH numbered AS (
          SELECT id, row_number() OVER (PARTITION BY task_id ORDER BY created_at, id) AS seq
          FROM task_events
        )
        UPDATE task_events SET sequence_no = numbered.seq FROM numbered WHERE task_events.id = numbered.id
    """)
    op.alter_column("task_events", "sequence_no", nullable=False)
    op.create_unique_constraint("uq_task_events_task_sequence", "task_events", ["task_id", "sequence_no"])
    op.create_index("ix_task_events_task_sequence", "task_events", ["task_id", "sequence_no"])
    op.alter_column("documents", "scan_status", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_task_events_task_sequence", table_name="task_events")
    op.drop_constraint("uq_task_events_task_sequence", "task_events", type_="unique")
    op.drop_column("task_events", "sequence_no")
    op.drop_column("learning_units", "merged_into_id")
    op.drop_column("learning_units", "merge_status")
    op.drop_column("documents", "detected_mime_type")
    op.drop_column("documents", "scanned_at")
    op.drop_column("documents", "scan_message")
    op.drop_column("documents", "scan_status")

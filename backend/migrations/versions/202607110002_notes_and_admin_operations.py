"""note revisions and admin operations

Revision ID: 202607110002
Revises: 202607110001
Create Date: 2026-07-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "202607110002"
down_revision: str | None = "202607110001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("must_change_password", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column("study_note_versions", sa.Column("source_version_id", sa.String(length=36), nullable=True))
    op.add_column("study_note_versions", sa.Column("edited_by_user_id", sa.String(length=36), nullable=True))
    op.add_column("study_note_versions", sa.Column("edit_origin", sa.String(length=32), nullable=True))
    op.add_column("study_note_versions", sa.Column("edit_summary", sa.String(length=500), nullable=True))
    op.create_foreign_key(
        "fk_study_note_versions_source_version_id",
        "study_note_versions",
        "study_note_versions",
        ["source_version_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_study_note_versions_edited_by_user_id",
        "study_note_versions",
        "users",
        ["edited_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(
        "uq_study_note_versions_workspace_unit_version",
        "study_note_versions",
        ["workspace_id", "learning_unit_id", "version_no"],
    )
    op.create_table(
        "admin_operations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("actor_user_id", sa.String(length=36), nullable=True),
        sa.Column("actor_workspace_id", sa.String(length=36), nullable=True),
        sa.Column("operation_type", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.String(length=255), nullable=False),
        sa.Column("target_label_hash", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("phase", sa.String(length=64), nullable=True),
        sa.Column("task_id", sa.String(length=36), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_admin_operations_status", "admin_operations", ["status"])
    op.create_index("ix_admin_operations_actor_user_id", "admin_operations", ["actor_user_id"])
    op.create_index("ix_admin_operations_target", "admin_operations", ["target_type", "target_id"])
    op.create_table(
        "admin_audit_logs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("actor_user_id", sa.String(length=36), nullable=True),
        sa.Column("actor_email", sa.String(length=255), nullable=False),
        sa.Column("action", sa.String(length=96), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.String(length=255), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=True),
        sa.Column("before_data", sa.JSON(), nullable=True),
        sa.Column("after_data", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_admin_audit_logs_actor_user_id", "admin_audit_logs", ["actor_user_id"])
    op.create_index("ix_admin_audit_logs_target", "admin_audit_logs", ["target_type", "target_id"])
    op.create_index("ix_admin_audit_logs_created_at", "admin_audit_logs", ["created_at"])


def downgrade() -> None:
    op.drop_table("admin_audit_logs")
    op.drop_table("admin_operations")
    op.drop_constraint("uq_study_note_versions_workspace_unit_version", "study_note_versions", type_="unique")
    op.drop_constraint("fk_study_note_versions_edited_by_user_id", "study_note_versions", type_="foreignkey")
    op.drop_constraint("fk_study_note_versions_source_version_id", "study_note_versions", type_="foreignkey")
    op.drop_column("study_note_versions", "edit_summary")
    op.drop_column("study_note_versions", "edit_origin")
    op.drop_column("study_note_versions", "edited_by_user_id")
    op.drop_column("study_note_versions", "source_version_id")
    op.drop_column("users", "must_change_password")

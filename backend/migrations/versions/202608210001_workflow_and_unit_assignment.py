"""add workflow aggregation and learning unit assignments

Revision ID: 202608210001
Revises: 202608200001
Create Date: 2026-08-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "202608210001"
down_revision: str | None = "202608200001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workflow_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("document_id", sa.String(length=36), nullable=True),
        sa.Column("learning_unit_id", sa.String(length=36), nullable=True),
        sa.Column("trigger_type", sa.String(length=32), server_default="upload", nullable=False),
        sa.Column("status", sa.String(length=32), server_default="waiting_upload", nullable=False),
        sa.Column("core_status", sa.String(length=32), server_default="not_started", nullable=False),
        sa.Column("enrichment_status", sa.String(length=32), server_default="not_applicable", nullable=False),
        sa.Column("current_stage", sa.String(length=64), nullable=True),
        sa.Column("progress", sa.Integer(), server_default="0", nullable=False),
        sa.Column("waiting_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("result", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("metadata", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["learning_unit_id"], ["learning_units.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_workflow_runs_workspace_id", "workflow_runs", ["workspace_id"])
    op.create_index("ix_workflow_runs_workspace_document", "workflow_runs", ["workspace_id", "document_id"])
    op.create_index("ix_workflow_runs_workspace_unit", "workflow_runs", ["workspace_id", "learning_unit_id"])
    op.create_index("ix_workflow_runs_workspace_status", "workflow_runs", ["workspace_id", "status"])

    op.add_column("documents", sa.Column("latest_workflow_run_id", sa.String(length=36), nullable=True))
    op.create_foreign_key(
        "fk_documents_latest_workflow_run_id",
        "documents",
        "workflow_runs",
        ["latest_workflow_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_documents_latest_workflow_run_id", "documents", ["latest_workflow_run_id"])

    op.create_table(
        "workflow_task_links",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workflow_run_id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column("phase", sa.String(length=16), nullable=False),
        sa.Column("required", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workflow_run_id"], ["workflow_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_run_id", "task_id", name="uq_workflow_task_links_run_task"),
    )
    op.create_index("ix_workflow_task_links_task_id", "workflow_task_links", ["task_id"])
    op.create_index("ix_workflow_task_links_workflow_run_id", "workflow_task_links", ["workflow_run_id"])

    op.create_table(
        "workflow_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("workflow_run_id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=True),
        sa.Column("task_event_id", sa.String(length=36), nullable=True),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("level", sa.String(length=16), server_default="info", nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=True),
        sa.Column("data", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workflow_run_id"], ["workflow_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["task_event_id"], ["task_events.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_run_id", "sequence_no", name="uq_workflow_events_run_sequence"),
    )
    op.create_index("ix_workflow_events_workspace_run", "workflow_events", ["workspace_id", "workflow_run_id"])
    op.create_index("ix_workflow_events_task_id", "workflow_events", ["task_id"])

    op.create_table(
        "learning_unit_assignments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("learning_unit_id", sa.String(length=36), nullable=False),
        sa.Column("method", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="assigned", nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("candidate_scores", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("evidence", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["learning_unit_id"], ["learning_units.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "document_id", name="uq_learning_unit_assignments_document"),
    )
    op.create_index(
        "ix_learning_unit_assignments_workspace_unit",
        "learning_unit_assignments",
        ["workspace_id", "learning_unit_id"],
    )
    op.create_index(
        "ix_learning_unit_assignments_workspace_status",
        "learning_unit_assignments",
        ["workspace_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_learning_unit_assignments_workspace_status", table_name="learning_unit_assignments")
    op.drop_index("ix_learning_unit_assignments_workspace_unit", table_name="learning_unit_assignments")
    op.drop_table("learning_unit_assignments")
    op.drop_index("ix_workflow_events_task_id", table_name="workflow_events")
    op.drop_index("ix_workflow_events_workspace_run", table_name="workflow_events")
    op.drop_table("workflow_events")
    op.drop_index("ix_workflow_task_links_workflow_run_id", table_name="workflow_task_links")
    op.drop_index("ix_workflow_task_links_task_id", table_name="workflow_task_links")
    op.drop_table("workflow_task_links")
    op.drop_index("ix_documents_latest_workflow_run_id", table_name="documents")
    op.drop_constraint("fk_documents_latest_workflow_run_id", "documents", type_="foreignkey")
    op.drop_column("documents", "latest_workflow_run_id")
    op.drop_index("ix_workflow_runs_workspace_status", table_name="workflow_runs")
    op.drop_index("ix_workflow_runs_workspace_unit", table_name="workflow_runs")
    op.drop_index("ix_workflow_runs_workspace_document", table_name="workflow_runs")
    op.drop_index("ix_workflow_runs_workspace_id", table_name="workflow_runs")
    op.drop_table("workflow_runs")

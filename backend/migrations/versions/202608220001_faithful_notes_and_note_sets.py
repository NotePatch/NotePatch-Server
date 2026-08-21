"""add faithful note policies, note sets, corrections and note gaps

Revision ID: 202608220001
Revises: 202608210001
Create Date: 2026-08-22
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "202608220001"
down_revision: str | None = "202608210001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("note_content_edit_level", sa.String(32), server_default="conceptual", nullable=False))
    op.add_column("users", sa.Column("note_layout_edit_level", sa.String(32), server_default="minor", nullable=False))
    op.add_column("users", sa.Column("note_history_limit", sa.Integer(), server_default="3", nullable=False))
    op.create_check_constraint("ck_users_note_history_limit", "users", "note_history_limit >= 0 AND note_history_limit <= 100")

    op.add_column("study_note_versions", sa.Column("note_ir_object_key", sa.String(1024), nullable=True))
    op.add_column("study_note_versions", sa.Column("content_edit_level", sa.String(32), server_default="conceptual", nullable=False))
    op.add_column("study_note_versions", sa.Column("layout_edit_level", sa.String(32), server_default="minor", nullable=False))
    op.execute(
        "UPDATE study_note_versions SET metadata = "
        "(COALESCE(metadata, '{}'::json)::jsonb || jsonb_build_object('legacy', true))::json"
    )

    op.create_table(
        "note_sets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("learning_unit_id", sa.String(36), sa.ForeignKey("learning_units.id", ondelete="SET NULL"), nullable=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("expected_page_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), server_default="open", nullable=False),
        sa.Column("content_edit_level", sa.String(32), nullable=False),
        sa.Column("layout_edit_level", sa.String(32), nullable=False),
        sa.Column("metadata", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("expected_page_count >= 1 AND expected_page_count <= 200", name="ck_note_sets_page_count"),
    )
    op.create_index("ix_note_sets_workspace_status", "note_sets", ["workspace_id", "status"])
    op.create_index("ix_note_sets_workspace_unit", "note_sets", ["workspace_id", "learning_unit_id"])

    op.create_table(
        "note_set_documents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("note_set_id", sa.String(36), sa.ForeignKey("note_sets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("document_id", sa.String(36), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("page_index", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("note_set_id", "page_index", name="uq_note_set_documents_page"),
        sa.UniqueConstraint("workspace_id", "document_id", name="uq_note_set_documents_document"),
        sa.CheckConstraint("page_index >= 0", name="ck_note_set_documents_page_index"),
    )
    op.create_index("ix_note_set_documents_workspace_set", "note_set_documents", ["workspace_id", "note_set_id"])

    op.create_table(
        "study_note_corrections",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("learning_unit_id", sa.String(36), sa.ForeignKey("learning_units.id", ondelete="CASCADE"), nullable=False),
        sa.Column("note_version_id", sa.String(36), sa.ForeignKey("study_note_versions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_block_id", sa.String(128), nullable=True),
        sa.Column("correction_type", sa.String(32), nullable=False),
        sa.Column("original_text", sa.Text(), nullable=False),
        sa.Column("corrected_text", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("source_refs", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_study_note_corrections_workspace_version", "study_note_corrections", ["workspace_id", "note_version_id"])

    op.create_table(
        "note_gap_suggestions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("learning_unit_id", sa.String(36), sa.ForeignKey("learning_units.id", ondelete="CASCADE"), nullable=False),
        sa.Column("knowledge_point_id", sa.String(36), sa.ForeignKey("knowledge_points.id", ondelete="CASCADE"), nullable=False),
        sa.Column("note_version_id", sa.String(36), sa.ForeignKey("study_note_versions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("detected_by_task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True),
        sa.Column("accepted_version_id", sa.String(36), sa.ForeignKey("study_note_versions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.String(32), server_default="pending", nullable=False),
        sa.Column("coverage_score", sa.Float(), server_default="0", nullable=False),
        sa.Column("source_refs", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("target_section_id", sa.String(255), nullable=True),
        sa.Column("target_anchor", sa.String(255), nullable=True),
        sa.Column("insert_position", sa.String(16), server_default="after", nullable=False),
        sa.Column("metadata", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "learning_unit_id", "knowledge_point_id", "note_version_id", name="uq_note_gap_source_version"),
    )
    op.create_index("ix_note_gaps_workspace_unit_status", "note_gap_suggestions", ["workspace_id", "learning_unit_id", "status"])

    op.create_table(
        "note_supplement_drafts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("learning_unit_id", sa.String(36), sa.ForeignKey("learning_units.id", ondelete="CASCADE"), nullable=False),
        sa.Column("gap_suggestion_id", sa.String(36), sa.ForeignKey("note_gap_suggestions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("base_note_version_id", sa.String(36), sa.ForeignKey("study_note_versions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("generated_by_task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), server_default="draft", nullable=False),
        sa.Column("html", sa.Text(), server_default="", nullable=False),
        sa.Column("selected_source_refs", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("target_section_id", sa.String(255), nullable=True),
        sa.Column("target_anchor", sa.String(255), nullable=True),
        sa.Column("insert_position", sa.String(16), server_default="after", nullable=False),
        sa.Column("instruction", sa.Text(), nullable=True),
        sa.Column("feedback", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("gap_suggestion_id", "version_no", name="uq_note_supplement_gap_version"),
    )
    op.create_index("ix_note_supplement_workspace_gap", "note_supplement_drafts", ["workspace_id", "gap_suggestion_id"])


def downgrade() -> None:
    op.drop_index("ix_note_supplement_workspace_gap", table_name="note_supplement_drafts")
    op.drop_table("note_supplement_drafts")
    op.drop_index("ix_note_gaps_workspace_unit_status", table_name="note_gap_suggestions")
    op.drop_table("note_gap_suggestions")
    op.drop_index("ix_study_note_corrections_workspace_version", table_name="study_note_corrections")
    op.drop_table("study_note_corrections")
    op.drop_index("ix_note_set_documents_workspace_set", table_name="note_set_documents")
    op.drop_table("note_set_documents")
    op.drop_index("ix_note_sets_workspace_unit", table_name="note_sets")
    op.drop_index("ix_note_sets_workspace_status", table_name="note_sets")
    op.drop_table("note_sets")
    op.drop_column("study_note_versions", "layout_edit_level")
    op.drop_column("study_note_versions", "content_edit_level")
    op.drop_column("study_note_versions", "note_ir_object_key")
    op.drop_constraint("ck_users_note_history_limit", "users", type_="check")
    op.drop_column("users", "note_history_limit")
    op.drop_column("users", "note_layout_edit_level")
    op.drop_column("users", "note_content_edit_level")

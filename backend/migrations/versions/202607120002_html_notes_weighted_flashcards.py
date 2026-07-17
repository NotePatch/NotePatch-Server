"""html notes and weighted flashcards

Revision ID: 202607120002
Revises: 202607120001
Create Date: 2026-07-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import VECTOR


revision: str = "202607120002"
down_revision: str | None = "202607120001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("learning_units", sa.Column("knowledge_revision", sa.Integer(), server_default="0", nullable=False))
    op.add_column("learning_units", sa.Column("attempt_revision", sa.Integer(), server_default="0", nullable=False))
    op.add_column("learning_units", sa.Column("notes_generated_revision", sa.Integer(), server_default="0", nullable=False))
    op.add_column("learning_units", sa.Column("note_generation_due_at", sa.DateTime(timezone=True), nullable=True))

    op.alter_column("study_note_versions", "markdown_object_key", new_column_name="html_object_key")
    op.alter_column("study_note_versions", "highlighted_object_key", new_column_name="highlighted_html_object_key")
    op.add_column(
        "study_note_versions",
        sa.Column("knowledge_point_ids", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
    )

    op.create_table(
        "knowledge_points",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("learning_unit_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("normalized_name", sa.String(length=255), nullable=False),
        sa.Column("embedding", VECTOR(dim=1024), nullable=True),
        sa.Column("source_document_ids", sa.JSON(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["learning_unit_id"], ["learning_units.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id", "learning_unit_id", "normalized_name", name="uq_knowledge_points_workspace_unit_name"
        ),
    )
    op.create_index("ix_knowledge_points_workspace_unit", "knowledge_points", ["workspace_id", "learning_unit_id"])
    op.create_index(
        "ix_knowledge_points_embedding_hnsw",
        "knowledge_points",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )

    op.add_column("mistakes", sa.Column("knowledge_point_id", sa.String(length=36), nullable=True))
    op.create_foreign_key(
        "fk_mistakes_knowledge_point_id",
        "mistakes",
        "knowledge_points",
        ["knowledge_point_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "knowledge_point_attempts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("learning_unit_id", sa.String(length=36), nullable=False),
        sa.Column("knowledge_point_id", sa.String(length=36), nullable=False),
        sa.Column("student_user_id", sa.String(length=36), nullable=True),
        sa.Column("homework_id", sa.String(length=36), nullable=True),
        sa.Column("grading_result_id", sa.String(length=36), nullable=True),
        sa.Column("question_id", sa.String(length=36), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("score_ratio", sa.Float(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["grading_result_id"], ["grading_results.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["homework_id"], ["homeworks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["knowledge_point_id"], ["knowledge_points.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["learning_unit_id"], ["learning_units.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["question_id"], ["questions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["student_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "grading_result_id",
            "question_id",
            "knowledge_point_id",
            name="uq_knowledge_point_attempt_grade_question_point",
        ),
    )
    op.create_index(
        "ix_knowledge_point_attempts_workspace_unit",
        "knowledge_point_attempts",
        ["workspace_id", "learning_unit_id"],
    )
    op.create_index(
        "ix_knowledge_point_attempts_point_time",
        "knowledge_point_attempts",
        ["knowledge_point_id", "occurred_at"],
    )

    op.create_table(
        "flashcard_decks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("learning_unit_id", sa.String(length=36), nullable=False),
        sa.Column("study_note_version_id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=True),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("attempt_revision", sa.Integer(), nullable=False),
        sa.Column("weighting_config", sa.JSON(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["learning_unit_id"], ["learning_units.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["study_note_version_id"], ["study_note_versions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id", "learning_unit_id", "version_no", name="uq_flashcard_decks_workspace_unit_version"
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "learning_unit_id",
            "study_note_version_id",
            "attempt_revision",
            name="uq_flashcard_decks_source_revision",
        ),
    )
    op.create_index("ix_flashcard_decks_workspace_unit", "flashcard_decks", ["workspace_id", "learning_unit_id"])

    op.create_table(
        "flashcards",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("deck_id", sa.String(length=36), nullable=False),
        sa.Column("knowledge_point_id", sa.String(length=36), nullable=False),
        sa.Column("front", sa.Text(), nullable=False),
        sa.Column("back", sa.Text(), nullable=False),
        sa.Column("priority_score", sa.Float(), nullable=False),
        sa.Column("priority_factors", sa.JSON(), nullable=False),
        sa.Column("source_refs", sa.JSON(), nullable=False),
        sa.Column("difficulty", sa.String(length=32), nullable=True),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["deck_id"], ["flashcard_decks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["knowledge_point_id"], ["knowledge_points.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_flashcards_workspace_deck", "flashcards", ["workspace_id", "deck_id"])
    op.create_index("ix_flashcards_knowledge_point", "flashcards", ["knowledge_point_id"])


def downgrade() -> None:
    op.drop_index("ix_flashcards_knowledge_point", table_name="flashcards")
    op.drop_index("ix_flashcards_workspace_deck", table_name="flashcards")
    op.drop_table("flashcards")
    op.drop_index("ix_flashcard_decks_workspace_unit", table_name="flashcard_decks")
    op.drop_table("flashcard_decks")
    op.drop_index("ix_knowledge_point_attempts_point_time", table_name="knowledge_point_attempts")
    op.drop_index("ix_knowledge_point_attempts_workspace_unit", table_name="knowledge_point_attempts")
    op.drop_table("knowledge_point_attempts")
    op.drop_constraint("fk_mistakes_knowledge_point_id", "mistakes", type_="foreignkey")
    op.drop_column("mistakes", "knowledge_point_id")
    op.drop_index("ix_knowledge_points_embedding_hnsw", table_name="knowledge_points")
    op.drop_index("ix_knowledge_points_workspace_unit", table_name="knowledge_points")
    op.drop_table("knowledge_points")
    op.drop_column("study_note_versions", "knowledge_point_ids")
    op.alter_column("study_note_versions", "highlighted_html_object_key", new_column_name="highlighted_object_key")
    op.alter_column("study_note_versions", "html_object_key", new_column_name="markdown_object_key")
    op.drop_column("learning_units", "note_generation_due_at")
    op.drop_column("learning_units", "notes_generated_revision")
    op.drop_column("learning_units", "attempt_revision")
    op.drop_column("learning_units", "knowledge_revision")

"""add secure user profiles, avatars, and chat message revisions

Revision ID: 202608200001
Revises: 202608190002
Create Date: 2026-08-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "202608200001"
down_revision: str | None = "202608190002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("avatar_url", sa.String(length=512), nullable=True))
    op.add_column("users", sa.Column("avatar_storage_backend", sa.String(length=32), nullable=True))
    op.add_column("users", sa.Column("avatar_object_key", sa.String(length=1024), nullable=True))
    op.add_column("users", sa.Column("avatar_mime_type", sa.String(length=128), nullable=True))
    op.add_column("users", sa.Column("avatar_size", sa.BigInteger(), nullable=True))
    op.add_column(
        "users",
        sa.Column("profile_version", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column(
        "users",
        sa.Column("auth_version", sa.Integer(), server_default="1", nullable=False),
    )

    op.create_table(
        "identity_mutation_keys",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("operation_scope", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="processing", nullable=False),
        sa.Column("response_data", sa.JSON(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "operation_scope",
            "idempotency_key",
            name="uq_identity_mutation_key",
        ),
    )
    op.create_index(
        "ix_identity_mutation_keys_expires_at",
        "identity_mutation_keys",
        ["expires_at"],
    )

    op.create_table(
        "identity_audit_logs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("actor_user_id", sa.String(length=36), nullable=True),
        sa.Column("action", sa.String(length=96), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("before_data", sa.JSON(), nullable=True),
        sa.Column("after_data", sa.JSON(), nullable=True),
        sa.Column("result", sa.String(length=32), server_default="succeeded", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_identity_audit_logs_actor_user_id", "identity_audit_logs", ["actor_user_id"])
    op.create_index("ix_identity_audit_logs_created_at", "identity_audit_logs", ["created_at"])
    op.create_index("ix_identity_audit_logs_action", "identity_audit_logs", ["action"])

    op.add_column(
        "chat_messages",
        sa.Column("revision_of_message_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "chat_messages",
        sa.Column("superseded_by_message_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "chat_messages",
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_chat_messages_revision_of_message_id",
        "chat_messages",
        "chat_messages",
        ["revision_of_message_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_chat_messages_superseded_by_message_id",
        "chat_messages",
        "chat_messages",
        ["superseded_by_message_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_chat_messages_conversation_superseded",
        "chat_messages",
        ["conversation_id", "superseded_at", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_chat_messages_conversation_superseded", table_name="chat_messages")
    op.drop_constraint(
        "fk_chat_messages_superseded_by_message_id",
        "chat_messages",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_chat_messages_revision_of_message_id",
        "chat_messages",
        type_="foreignkey",
    )
    op.drop_column("chat_messages", "superseded_at")
    op.drop_column("chat_messages", "superseded_by_message_id")
    op.drop_column("chat_messages", "revision_of_message_id")

    op.drop_index("ix_identity_audit_logs_action", table_name="identity_audit_logs")
    op.drop_index("ix_identity_audit_logs_created_at", table_name="identity_audit_logs")
    op.drop_index("ix_identity_audit_logs_actor_user_id", table_name="identity_audit_logs")
    op.drop_table("identity_audit_logs")
    op.drop_index("ix_identity_mutation_keys_expires_at", table_name="identity_mutation_keys")
    op.drop_table("identity_mutation_keys")

    op.drop_column("users", "auth_version")
    op.drop_column("users", "profile_version")
    op.drop_column("users", "avatar_size")
    op.drop_column("users", "avatar_mime_type")
    op.drop_column("users", "avatar_object_key")
    op.drop_column("users", "avatar_storage_backend")
    op.drop_column("users", "avatar_url")

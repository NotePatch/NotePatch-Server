"""file management tusd seaweedfs

Revision ID: 202607090001
Revises: 202607080001
Create Date: 2026-07-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "202607090001"
down_revision: str | None = "202607080001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column("hashed_password", new_column_name="password_hash")
        batch_op.add_column(sa.Column("phone", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("username", sa.String(length=64), nullable=True))
    op.create_index("ix_users_phone", "users", ["phone"], unique=True)
    op.create_index("ix_users_username", "users", ["username"], unique=True)

    with op.batch_alter_table("documents") as batch_op:
        batch_op.alter_column("owner_user_id", new_column_name="uploaded_by")
        batch_op.alter_column("filename", new_column_name="original_filename")
        batch_op.alter_column("content_type", new_column_name="mime_type")
        batch_op.alter_column("size_bytes", new_column_name="file_size")
        batch_op.alter_column("storage_key", new_column_name="object_key")
        batch_op.add_column(sa.Column("title", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("file_type", sa.String(length=32), server_default="other", nullable=False))
        batch_op.add_column(sa.Column("document_kind", sa.String(length=64), server_default="other", nullable=False))
        batch_op.add_column(
            sa.Column("storage_backend", sa.String(length=64), server_default="seaweedfs", nullable=False)
        )
        batch_op.add_column(sa.Column("bucket", sa.String(length=255), server_default="notepatch", nullable=False))
        batch_op.add_column(sa.Column("upload_id", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("tus_upload_url", sa.String(length=1024), nullable=True))
        batch_op.add_column(sa.Column("sha256", sa.String(length=64), nullable=True))

    op.execute("UPDATE documents SET status = 'ready' WHERE status = 'processed'")

    with op.batch_alter_table("document_artifacts") as batch_op:
        batch_op.alter_column("kind", new_column_name="artifact_type")
        batch_op.alter_column("storage_key", new_column_name="object_key")
        batch_op.alter_column("content_type", new_column_name="mime_type")
        batch_op.alter_column("size_bytes", new_column_name="file_size")
        batch_op.add_column(sa.Column("bucket", sa.String(length=255), server_default="notepatch", nullable=False))

    op.execute("UPDATE document_artifacts SET artifact_type = 'deskewed_image' WHERE artifact_type = 'processed_image'")

    op.create_table(
        "upload_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("tus_upload_id", sa.String(length=255), nullable=True),
        sa.Column("tus_upload_url", sa.String(length=1024), nullable=True),
        sa.Column("bucket", sa.String(length=255), nullable=False),
        sa.Column("object_key", sa.String(length=1024), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_upload_sessions_tus_upload_id", "upload_sessions", ["tus_upload_id"])
    op.create_index("ix_upload_sessions_workspace_id_document_id", "upload_sessions", ["workspace_id", "document_id"])
    op.create_index("ix_upload_sessions_workspace_id_id", "upload_sessions", ["workspace_id", "id"])


def downgrade() -> None:
    op.drop_table("upload_sessions")

    op.execute("UPDATE document_artifacts SET artifact_type = 'processed_image' WHERE artifact_type = 'deskewed_image'")
    with op.batch_alter_table("document_artifacts") as batch_op:
        batch_op.drop_column("bucket")
        batch_op.alter_column("file_size", new_column_name="size_bytes")
        batch_op.alter_column("mime_type", new_column_name="content_type")
        batch_op.alter_column("object_key", new_column_name="storage_key")
        batch_op.alter_column("artifact_type", new_column_name="kind")

    op.execute("UPDATE documents SET status = 'processed' WHERE status = 'ready'")
    with op.batch_alter_table("documents") as batch_op:
        batch_op.drop_column("sha256")
        batch_op.drop_column("tus_upload_url")
        batch_op.drop_column("upload_id")
        batch_op.drop_column("bucket")
        batch_op.drop_column("storage_backend")
        batch_op.drop_column("document_kind")
        batch_op.drop_column("file_type")
        batch_op.drop_column("title")
        batch_op.alter_column("object_key", new_column_name="storage_key")
        batch_op.alter_column("file_size", new_column_name="size_bytes")
        batch_op.alter_column("mime_type", new_column_name="content_type")
        batch_op.alter_column("original_filename", new_column_name="filename")
        batch_op.alter_column("uploaded_by", new_column_name="owner_user_id")

    op.drop_index("ix_users_username", table_name="users")
    op.drop_index("ix_users_phone", table_name="users")
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("username")
        batch_op.drop_column("phone")
        batch_op.alter_column("password_hash", new_column_name="hashed_password")

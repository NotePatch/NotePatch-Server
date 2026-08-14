"""persist chat message attachments

Revision ID: 202608140002
Revises: 202608140001
Create Date: 2026-08-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "202608140002"
down_revision: str | None = "202608140001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "chat_messages",
        sa.Column("attachments", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
    )
    op.execute(
        """
        UPDATE chat_messages AS message
        SET attachments = task.payload->'input'->'attachments'
        FROM tasks AS task
        WHERE message.id = task.payload->>'user_message_id'
          AND json_typeof(task.payload->'input'->'attachments') = 'array'
        """
    )

    op.execute(
        """
        UPDATE chat_messages AS message
        SET attachments = (
            SELECT COALESCE(
                json_agg(
                    json_build_object(
                        'document_id', item.value->>'document_id',
                        'filename', COALESCE(document.original_filename, item.value->>'filename'),
                        'title', COALESCE(document.title, item.value->>'title'),
                        'mime_type', COALESCE(document.mime_type, item.value->>'mime_type'),
                        'file_type', COALESCE(document.file_type, item.value->>'file_type'),
                        'file_size', document.file_size,
                        'status', document.status,
                        'availability',
                            CASE
                                WHEN document.id IS NULL OR document.status = 'deleted' THEN 'unavailable'
                                ELSE 'available'
                            END
                    )
                    ORDER BY item.ordinality
                ),
                '[]'::json
            )
            FROM json_array_elements(message.attachments) WITH ORDINALITY AS item(value, ordinality)
            LEFT JOIN documents AS document
              ON document.id = item.value->>'document_id'
             AND document.workspace_id = message.workspace_id
        )
        WHERE json_array_length(message.attachments) > 0
        """
    )


def downgrade() -> None:
    op.drop_column("chat_messages", "attachments")

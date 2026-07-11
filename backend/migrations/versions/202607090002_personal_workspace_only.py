"""personal workspace only

Revision ID: 202607090002
Revises: 202607090001
Create Date: 2026-07-09
"""

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import uuid

import sqlalchemy as sa
from alembic import op

revision: str = "202607090002"
down_revision: str | None = "202607090001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


OWNER_PERMISSIONS: dict[str, str] = {
    "workspace.read": "Read workspace details",
    "documents.write": "Create, process, and delete documents",
    "homeworks.write": "Create and grade homeworks",
    "mistakes.write": "Update mistake status and metadata",
    "ai.run": "Run AI and sandbox tasks",
}

WORKSPACE_SCOPED_TABLES = (
    "documents",
    "document_artifacts",
    "homeworks",
    "questions",
    "grading_results",
    "mistakes",
    "knowledge_chunks",
    "tasks",
    "task_events",
    "upload_sessions",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


def _ensure_owner_role(bind: sa.Connection) -> str:
    owner_role_id = bind.execute(sa.text("SELECT id FROM roles WHERE name = 'owner'")).scalar_one_or_none()
    if owner_role_id is None:
        owner_role_id = _new_id()
        bind.execute(
            sa.text(
                """
                INSERT INTO roles (id, name, description, created_at)
                VALUES (:id, 'owner', :description, :created_at)
                """
            ),
            {
                "id": owner_role_id,
                "description": "Personal workspace owner with full access",
                "created_at": _now(),
            },
        )

    for name, description in OWNER_PERMISSIONS.items():
        permission_id = bind.execute(
            sa.text("SELECT id FROM permissions WHERE name = :name"),
            {"name": name},
        ).scalar_one_or_none()
        if permission_id is None:
            permission_id = _new_id()
            bind.execute(
                sa.text(
                    """
                    INSERT INTO permissions (id, name, description, created_at)
                    VALUES (:id, :name, :description, :created_at)
                    """
                ),
                {"id": permission_id, "name": name, "description": description, "created_at": _now()},
            )
        assigned = bind.execute(
            sa.text(
                """
                SELECT id
                FROM role_permissions
                WHERE role_id = :role_id AND permission_id = :permission_id
                """
            ),
            {"role_id": owner_role_id, "permission_id": permission_id},
        ).scalar_one_or_none()
        if assigned is None:
            bind.execute(
                sa.text(
                    """
                    INSERT INTO role_permissions (id, role_id, permission_id)
                    VALUES (:id, :role_id, :permission_id)
                    """
                ),
                {"id": _new_id(), "role_id": owner_role_id, "permission_id": permission_id},
            )
    return owner_role_id


def _workspace_name(user: Mapping[str, str | None]) -> str:
    display_name = user["full_name"] or user["email"]
    return f"{display_name}'s Workspace"


def _move_workspace_rows(bind: sa.Connection, source_workspace_id: str, target_workspace_id: str) -> None:
    for table_name in WORKSPACE_SCOPED_TABLES:
        bind.execute(
            sa.text(f"UPDATE {table_name} SET workspace_id = :target WHERE workspace_id = :source"),
            {"target": target_workspace_id, "source": source_workspace_id},
        )


def upgrade() -> None:
    bind = op.get_bind()
    owner_role_id = _ensure_owner_role(bind)

    users = bind.execute(
        sa.text("SELECT id, email, full_name FROM users ORDER BY created_at ASC, id ASC")
    ).mappings().all()

    for user in users:
        workspaces = bind.execute(
            sa.text(
                """
                SELECT id
                FROM workspaces
                WHERE owner_user_id = :owner_user_id
                ORDER BY created_at ASC, id ASC
                """
            ),
            {"owner_user_id": user["id"]},
        ).mappings().all()

        if workspaces:
            canonical_workspace_id = workspaces[0]["id"]
            bind.execute(
                sa.text(
                    """
                    UPDATE workspaces
                    SET type = 'personal', updated_at = :updated_at
                    WHERE id = :workspace_id
                    """
                ),
                {"workspace_id": canonical_workspace_id, "updated_at": _now()},
            )
        else:
            canonical_workspace_id = _new_id()
            now = _now()
            bind.execute(
                sa.text(
                    """
                    INSERT INTO workspaces (id, name, type, owner_user_id, created_at, updated_at)
                    VALUES (:id, :name, 'personal', :owner_user_id, :created_at, :updated_at)
                    """
                ),
                {
                    "id": canonical_workspace_id,
                    "name": _workspace_name(user),
                    "owner_user_id": user["id"],
                    "created_at": now,
                    "updated_at": now,
                },
            )

        for duplicate in workspaces[1:]:
            duplicate_workspace_id = duplicate["id"]
            _move_workspace_rows(bind, duplicate_workspace_id, canonical_workspace_id)
            bind.execute(
                sa.text("DELETE FROM workspace_members WHERE workspace_id = :workspace_id"),
                {"workspace_id": duplicate_workspace_id},
            )
            bind.execute(
                sa.text("DELETE FROM workspaces WHERE id = :workspace_id"),
                {"workspace_id": duplicate_workspace_id},
            )

        owner_member_id = bind.execute(
            sa.text(
                """
                SELECT id
                FROM workspace_members
                WHERE workspace_id = :workspace_id AND user_id = :user_id
                """
            ),
            {"workspace_id": canonical_workspace_id, "user_id": user["id"]},
        ).scalar_one_or_none()
        if owner_member_id is None:
            bind.execute(
                sa.text(
                    """
                    INSERT INTO workspace_members (id, workspace_id, user_id, role_id, created_at)
                    VALUES (:id, :workspace_id, :user_id, :role_id, :created_at)
                    """
                ),
                {
                    "id": _new_id(),
                    "workspace_id": canonical_workspace_id,
                    "user_id": user["id"],
                    "role_id": owner_role_id,
                    "created_at": _now(),
                },
            )
        else:
            bind.execute(
                sa.text("UPDATE workspace_members SET role_id = :role_id WHERE id = :member_id"),
                {"role_id": owner_role_id, "member_id": owner_member_id},
            )

    bind.execute(sa.text("UPDATE workspaces SET type = 'personal', updated_at = :updated_at"), {"updated_at": _now()})
    bind.execute(
        sa.text(
            """
            DELETE FROM workspace_members
            WHERE NOT EXISTS (
                SELECT 1
                FROM workspaces
                WHERE workspaces.id = workspace_members.workspace_id
                  AND workspaces.owner_user_id = workspace_members.user_id
            )
            """
        )
    )
    op.create_unique_constraint("uq_workspaces_owner_user_id", "workspaces", ["owner_user_id"])


def downgrade() -> None:
    op.drop_constraint("uq_workspaces_owner_user_id", "workspaces", type_="unique")

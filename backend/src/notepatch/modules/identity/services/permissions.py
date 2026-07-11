from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from notepatch.modules.identity.models.workspace import Permission, Role, RolePermission, WorkspaceMember

PERMISSIONS: dict[str, str] = {
    "workspace.read": "Read workspace details",
    "documents.write": "Create, process, and delete documents",
    "homeworks.write": "Create and grade homeworks",
    "mistakes.write": "Update mistake status and metadata",
    "ai.run": "Run AI and sandbox tasks",
}

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "owner": set(PERMISSIONS),
}

ROLE_DESCRIPTIONS = {
    "owner": "Personal workspace owner with full access",
}


def seed_roles_and_permissions(db: Session) -> None:
    existing_permissions = {item.name: item for item in db.scalars(select(Permission)).all()}
    for name, description in PERMISSIONS.items():
        if name not in existing_permissions:
            permission = Permission(name=name, description=description)
            db.add(permission)
            existing_permissions[name] = permission

    existing_roles = {item.name: item for item in db.scalars(select(Role)).all()}
    for name, permission_names in ROLE_PERMISSIONS.items():
        role = existing_roles.get(name)
        if role is None:
            role = Role(name=name, description=ROLE_DESCRIPTIONS.get(name))
            db.add(role)
            existing_roles[name] = role
        db.flush()
        role_permissions = db.scalars(select(RolePermission).where(RolePermission.role_id == role.id)).all()
        assigned = {row.permission.name for row in role_permissions}
        for role_permission in role_permissions:
            if role_permission.permission.name not in permission_names:
                db.delete(role_permission)
        for permission_name in permission_names - assigned:
            db.add(RolePermission(role_id=role.id, permission_id=existing_permissions[permission_name].id))
    db.commit()


def get_role(db: Session, name: str) -> Role:
    role = db.scalar(select(Role).where(Role.name == name))
    if role is None:
        seed_roles_and_permissions(db)
        role = db.scalar(select(Role).where(Role.name == name))
    if role is None:
        raise RuntimeError(f"Role {name!r} could not be created")
    return role


def member_has_permission(db: Session, member: WorkspaceMember, permission_name: str) -> bool:
    return (
        db.scalar(
            select(RolePermission.id)
            .join(Permission, Permission.id == RolePermission.permission_id)
            .where(RolePermission.role_id == member.role_id, Permission.name == permission_name)
        )
        is not None
    )


def require_member_permission(db: Session, member: WorkspaceMember, permission_name: str) -> None:
    if not member_has_permission(db, member, permission_name):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing workspace permission: {permission_name}",
        )

from datetime import datetime, timedelta, timezone
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from notepatch.entrypoints.deps import get_authenticated_user, get_current_user, get_presence_service
from notepatch.platform.config import get_settings
from notepatch.platform.database import get_db, utcnow
from notepatch.modules.identity.services.permissions import get_role, seed_roles_and_permissions
from notepatch.platform.security import (
    create_access_token,
    create_refresh_token,
    hash_password,
    hash_token,
    verify_password,
)
from notepatch.modules.identity.models.user import RefreshToken, User
from notepatch.modules.identity.models.workspace import Workspace, WorkspaceMember
from notepatch.modules.identity.schemas.auth import (
    LoginRequest,
    LogoutRequest,
    OkResponse,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserRead,
    UserPreferencesUpdate,
    ChangePasswordRequest,
)
from notepatch.modules.ai.services.runtime import OpenClawUserRuntimeService
from notepatch.modules.identity.services.presence import PresenceService

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger(__name__)


def _normalize_email(email: str) -> str:
    return email.lower().strip()


def _is_expired(expires_at: datetime) -> bool:
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= utcnow()


TERMINAL_REFRESH_REVOCATION_REASONS = {"logout", "password_change", "admin_action", "user_disabled"}


def _issue_tokens(
    db: Session,
    user: User,
    *,
    family_id: str | None = None,
    parent_token_id: str | None = None,
    commit: bool = True,
) -> TokenResponse:
    access_token, access_expires_at = create_access_token(user.id)
    refresh_token, refresh_expires_at = create_refresh_token(user.id)
    family_id = family_id or str(uuid.uuid4())
    db.add(
        RefreshToken(
            user_id=user.id,
            family_id=family_id,
            parent_token_id=parent_token_id,
            token_hash=hash_token(refresh_token),
            expires_at=refresh_expires_at,
        )
    )
    if commit:
        db.commit()
        db.refresh(user)
    else:
        db.flush()
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=access_expires_at,
        user=user,
    )


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, db: Session = Depends(get_db)) -> TokenResponse:
    email = _normalize_email(payload.email)
    if db.scalar(select(User.id).where(User.email == email)) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    seed_roles_and_permissions(db)
    owner_role = get_role(db, "owner")
    user = User(email=email, password_hash=hash_password(payload.password), full_name=payload.full_name)
    db.add(user)
    db.flush()

    workspace_name = f"{payload.full_name or email}'s Workspace"
    workspace = Workspace(name=workspace_name, type="personal", owner_user_id=user.id)
    db.add(workspace)
    db.flush()
    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role_id=owner_role.id))
    OpenClawUserRuntimeService().provision_user(user, workspace)
    db.commit()
    db.refresh(user)
    return _issue_tokens(db, user)


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    email = _normalize_email(payload.email)
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not user.is_active or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    return _issue_tokens(db, user)


@router.post("/refresh", response_model=TokenResponse)
def refresh(payload: RefreshRequest, db: Session = Depends(get_db)) -> TokenResponse:
    token_hash = hash_token(payload.refresh_token)
    refresh_token = db.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash).with_for_update()
    )
    if refresh_token is None or _is_expired(refresh_token.expires_at):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")

    terminal_revocation = db.scalar(
        select(RefreshToken.id)
        .where(
            RefreshToken.family_id == refresh_token.family_id,
            RefreshToken.revoked_reason.in_(TERMINAL_REFRESH_REVOCATION_REASONS),
        )
        .limit(1)
    )
    if terminal_revocation is not None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")

    now = utcnow()
    if refresh_token.revoked_at is not None:
        revoked_at = refresh_token.revoked_at
        if revoked_at.tzinfo is None:
            revoked_at = revoked_at.replace(tzinfo=timezone.utc)
        grace = timedelta(seconds=get_settings().refresh_token_rotation_grace_seconds)
        if refresh_token.revoked_reason != "rotated" or now - revoked_at > grace:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")

    user = db.get(User, refresh_token.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")
    if refresh_token.revoked_at is None:
        refresh_token.revoked_at = now
        refresh_token.revoked_reason = "rotated"
    response = _issue_tokens(
        db,
        user,
        family_id=refresh_token.family_id,
        parent_token_id=refresh_token.id,
        commit=False,
    )
    db.commit()
    db.refresh(user)
    return response


@router.post("/logout", response_model=OkResponse)
def logout(
    payload: LogoutRequest,
    db: Session = Depends(get_db),
    presence: PresenceService = Depends(get_presence_service),
) -> OkResponse:
    refresh_token = db.scalar(
        select(RefreshToken)
        .where(RefreshToken.token_hash == hash_token(payload.refresh_token))
        .with_for_update()
    )
    if refresh_token is not None:
        if payload.client_id:
            try:
                presence.offline(refresh_token.user_id, payload.client_id)
            except Exception as exc:
                logger.warning("Could not clear presence for user %s: %s", refresh_token.user_id, exc)
        now = utcnow()
        for family_token in db.scalars(
            select(RefreshToken).where(
                RefreshToken.family_id == refresh_token.family_id,
                RefreshToken.revoked_at.is_(None),
            )
        ).all():
            family_token.revoked_at = now
            family_token.revoked_reason = "logout"
        refresh_token.revoked_at = refresh_token.revoked_at or now
        refresh_token.revoked_reason = "logout"
        db.commit()
    return OkResponse()


@router.get("/me", response_model=UserRead)
def me(current_user: User = Depends(get_authenticated_user)) -> User:
    return current_user


@router.post("/change-password", response_model=TokenResponse)
def change_password(
    payload: ChangePasswordRequest,
    current_user: User = Depends(get_authenticated_user),
    db: Session = Depends(get_db),
) -> TokenResponse:
    if not verify_password(payload.current_password, current_user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect")
    current_user.password_hash = hash_password(payload.new_password)
    current_user.must_change_password = False
    now = utcnow()
    for token in db.scalars(
        select(RefreshToken).where(RefreshToken.user_id == current_user.id, RefreshToken.revoked_at.is_(None))
    ).all():
        token.revoked_at = now
        token.revoked_reason = "password_change"
    db.commit()
    return _issue_tokens(db, current_user)


@router.patch("/preferences", response_model=UserRead)
def update_preferences(
    payload: UserPreferencesUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> User:
    current_user.ai_history_enabled = payload.ai_history_enabled
    db.commit()
    db.refresh(current_user)
    return current_user

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import quote

import jwt
from fastapi import HTTPException, status

from notepatch.modules.learning.models.learning import StudyNoteVersion
from notepatch.modules.learning.services.note_themes import (
    DEFAULT_NOTE_THEME_ID,
    NOTE_THEME_WRAPPER_CLASS,
    note_theme_css_path,
    note_theme_id,
)
from notepatch.platform.config import Settings, get_settings


THEME_ID = DEFAULT_NOTE_THEME_ID
THEME_CSS_PATH = note_theme_css_path(DEFAULT_NOTE_THEME_ID)
WRAPPER_CLASS = NOTE_THEME_WRAPPER_CLASS


class NoteRenderService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def create_url(self, note: StudyNoteVersion, expires_seconds: int | None = None) -> str:
        ttl = expires_seconds or self.settings.note_render_token_expire_seconds
        now = datetime.now(timezone.utc)
        token = jwt.encode(
            {
                "type": "note_render",
                "workspace_id": note.workspace_id,
                "learning_unit_id": note.learning_unit_id,
                "note_version_id": note.id,
                "iat": now,
                "exp": now + timedelta(seconds=ttl),
            },
            self.settings.effective_secret_key,
            algorithm=self.settings.jwt_algorithm,
        )
        path = f"/api/v1/assets/study-notes/render?token={quote(token)}"
        return self.settings.public_route_url(path)

    def decode(self, token: str) -> dict:
        try:
            payload = jwt.decode(
                token,
                self.settings.effective_secret_key,
                algorithms=[self.settings.jwt_algorithm],
            )
        except jwt.PyJWTError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired render URL") from exc
        if payload.get("type") != "note_render" or not payload.get("note_version_id"):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid render URL")
        return payload

    def inject_visual_assets(self, note: StudyNoteVersion, fragment: str, storage) -> str:
        assets = (note.metadata_ or {}).get("visual_assets") or {}
        for asset_id, item in assets.items():
            object_key = item.get("object_key") if isinstance(item, dict) else None
            if not object_key:
                continue
            try:
                payload = storage.get_object_bytes(storage.bucket, object_key)
            except Exception:
                continue
            mime_type = str(item.get("mime_type") or "image/png")
            encoded = base64.b64encode(payload).decode("ascii")
            marker = (
                f'<figure class="np-source-fragment" data-note-asset-id="{asset_id}">'
                '<figcaption>原稿中的图示或批注区域</figcaption></figure>'
            )
            rendered = (
                f'<figure class="np-source-fragment" data-note-asset-id="{asset_id}">'
                f'<img src="data:{mime_type};base64,{encoded}" alt="原稿图示或批注">'
                '<figcaption>原稿中的图示或批注区域</figcaption></figure>'
            )
            fragment = fragment.replace(marker, rendered)
        return fragment

    def wrap_html(self, note: StudyNoteVersion, fragment: str) -> str:
        css_url = self.settings.public_route_url(note_theme_css_path(note_theme_id(note)))
        title = escape(note.title)
        return (
            "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{title}</title><link rel=\"stylesheet\" href=\"{css_url}\"></head>"
            f"<body><main class=\"{WRAPPER_CLASS}\">{fragment}</main></body></html>"
        )

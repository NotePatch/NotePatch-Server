from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import quote

import jwt
from fastapi import HTTPException, status

from notepatch.modules.learning.models.learning import StudyNoteVersion
from notepatch.platform.config import Settings, get_settings


THEME_ID = "notepatch-paper-v1"
THEME_CSS_PATH = "/api/v1/assets/note-themes/notepatch-paper-v1.css"
WRAPPER_CLASS = "np-note-theme"


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
        base = self.settings.public_api_base_url.rstrip("/")
        return f"{base}{path}" if base else path

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

    def wrap_html(self, note: StudyNoteVersion, fragment: str) -> str:
        css_url = THEME_CSS_PATH
        title = escape(note.title)
        return (
            "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{title}</title><link rel=\"stylesheet\" href=\"{css_url}\"></head>"
            f"<body><main class=\"{WRAPPER_CLASS}\">{fragment}</main></body></html>"
        )

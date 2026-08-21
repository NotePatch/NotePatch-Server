from __future__ import annotations

from notepatch.modules.learning.models.learning import StudyNoteVersion
from notepatch.platform.config import Settings, get_settings


DEFAULT_NOTE_THEME_ID = "notepatch-paper-v1"
CURRENT_NOTE_THEME_ID = "notepatch-paper-v2"
NOTE_THEME_FILES = {
    DEFAULT_NOTE_THEME_ID: "notepatch-paper-v1.css",
    CURRENT_NOTE_THEME_ID: "notepatch-paper-v2.css",
}
NOTE_THEME_WRAPPER_CLASS = "np-note-theme"


def normalize_note_theme_id(theme_id: object) -> str:
    return theme_id if isinstance(theme_id, str) and theme_id in NOTE_THEME_FILES else DEFAULT_NOTE_THEME_ID


def note_theme_id(note: StudyNoteVersion | None) -> str:
    metadata = getattr(note, "metadata_", None) if note is not None else None
    configured = metadata.get("theme_id") if isinstance(metadata, dict) else None
    return normalize_note_theme_id(configured)


def note_theme_css_path(theme_id: str) -> str:
    normalized = normalize_note_theme_id(theme_id)
    return f"/api/v1/assets/note-themes/{normalized}.css"


def note_theme_css_url(theme_id: str, settings: Settings | None = None) -> str:
    return (settings or get_settings()).public_route_url(note_theme_css_path(theme_id))

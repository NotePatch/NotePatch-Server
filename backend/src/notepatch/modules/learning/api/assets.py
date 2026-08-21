from importlib.resources import files

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from notepatch.entrypoints.deps import get_storage_service
from notepatch.modules.learning.models.learning import StudyNoteVersion
from notepatch.modules.learning.services.note_render import NoteRenderService
from notepatch.modules.learning.services.note_themes import NOTE_THEME_FILES
from notepatch.platform.database import get_db
from notepatch.platform.storage import StorageService

router = APIRouter(prefix="/assets", tags=["assets"])


@router.get("/note-themes/{theme_id}.css", include_in_schema=True)
def note_theme(theme_id: str) -> Response:
    filename = NOTE_THEME_FILES.get(theme_id)
    if filename is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Note theme not found")
    css = files("notepatch.modules.learning.assets").joinpath(filename).read_text(encoding="utf-8")
    return Response(
        content=css,
        media_type="text/css; charset=utf-8",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.get("/study-notes/render", response_class=HTMLResponse)
def render_study_note(
    token: str = Query(min_length=1),
    db: Session = Depends(get_db),
    storage: StorageService = Depends(get_storage_service),
) -> HTMLResponse:
    renderer = NoteRenderService()
    payload = renderer.decode(token)
    note = db.scalar(
        select(StudyNoteVersion).where(
            StudyNoteVersion.id == payload["note_version_id"],
            StudyNoteVersion.workspace_id == payload["workspace_id"],
            StudyNoteVersion.learning_unit_id == payload["learning_unit_id"],
        )
    )
    if note is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Study note not found")
    object_key = note.highlighted_html_object_key or note.html_object_key
    try:
        fragment = storage.get_text_artifact(object_key, bucket=storage.bucket)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Study note content not found") from exc
    return HTMLResponse(
        renderer.wrap_html(note, fragment),
        headers={
            "Cache-Control": "private, no-store",
            "Content-Security-Policy": "default-src 'none'; style-src 'self'; img-src 'none'; font-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )

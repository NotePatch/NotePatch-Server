from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from sqlalchemy import func, select


ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

import notepatch.models  # noqa: F401 - register every SQLAlchemy relationship target

from notepatch.modules.learning.models.learning import StudyNoteVersion
from notepatch.modules.learning.schemas.skills import ScholarNotesResult
from notepatch.modules.learning.services.html_notes import (
    validate_knowledge_point_references,
    validate_note_structure,
)
from notepatch.modules.learning.services.note_ir import render_note_ir
from notepatch.modules.learning.services.note_markdown import NOTE_MARKDOWN_RENDERER_REVISION
from notepatch.modules.learning.services.note_themes import CURRENT_NOTE_THEME_ID
from notepatch.modules.tasks.services.task import TaskService
from notepatch.platform.database import SessionLocal
from notepatch.platform.storage import StorageService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Re-render validated Note IR as safe, structured Markdown HTML"
    )
    parser.add_argument("--workspace-id")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def _put_bytes(storage: StorageService, object_key: str, body: bytes) -> None:
    with tempfile.NamedTemporaryFile(suffix=".html") as handle:
        handle.write(body)
        handle.flush()
        storage.put_file(
            storage.bucket,
            object_key,
            handle.name,
            content_type="text/html; charset=utf-8",
        )


def _load_result(storage: StorageService, note: StudyNoteVersion) -> ScholarNotesResult:
    structured = storage.get_json_artifact(note.json_object_key, bucket=storage.bucket)
    note_ir = storage.get_json_artifact(note.note_ir_object_key, bucket=storage.bucket)
    for block in note_ir.get("blocks", []):
        if isinstance(block, dict):
            # This retired field represented an image crop. Current notes preserve
            # the structured diagram/annotation text and never embed source images.
            block.pop("preserve_as_image", None)
    payload = {
        field: structured[field]
        for field in ScholarNotesResult.model_fields
        if field in structured
    }
    payload["note_ir"] = note_ir
    return ScholarNotesResult.model_validate(payload)


def main() -> None:
    args = parse_args()
    storage = StorageService()
    summary: dict[str, object] = {
        "apply": args.apply,
        "force": args.force,
        "eligible": 0,
        "updated": 0,
        "already_current": 0,
        "legacy_without_note_ir": [],
        "failed": [],
        "highlight_tasks": [],
    }
    with SessionLocal() as db:
        query = select(StudyNoteVersion).order_by(StudyNoteVersion.created_at.asc())
        if args.workspace_id:
            query = query.where(StudyNoteVersion.workspace_id == args.workspace_id)
        note_ids = list(db.scalars(query).all())
        note_ids = [note.id for note in note_ids]
        latest_versions = dict(
            db.execute(
                select(
                    StudyNoteVersion.learning_unit_id,
                    func.max(StudyNoteVersion.version_no),
                )
                .where(
                    StudyNoteVersion.workspace_id == args.workspace_id
                    if args.workspace_id
                    else True
                )
                .group_by(StudyNoteVersion.learning_unit_id)
            ).all()
        )

        for note_id in note_ids:
            note = db.get(StudyNoteVersion, note_id)
            if note is None:
                continue
            if not note.note_ir_object_key:
                summary["legacy_without_note_ir"].append(note.id)
                continue
            metadata = note.metadata_ or {}
            if (
                not args.force
                and metadata.get("renderer_revision") == NOTE_MARKDOWN_RENDERER_REVISION
                and metadata.get("theme_id") == CURRENT_NOTE_THEME_ID
            ):
                summary["already_current"] += 1
                continue
            summary["eligible"] += 1
            old_html: bytes | None = None
            old_highlighted_key = note.highlighted_html_object_key
            old_highlight_map_key = note.highlight_map_object_key
            try:
                result = _load_result(storage, note)
                html = render_note_ir(result)
                validate_note_structure(html)
                validate_knowledge_point_references(html, set(note.knowledge_point_ids or []))
                if not args.apply:
                    continue

                old_html = storage.get_object_bytes(storage.bucket, note.html_object_key)
                _put_bytes(storage, note.html_object_key, html.encode("utf-8"))
                note.metadata_ = {
                    **metadata,
                    "theme_id": CURRENT_NOTE_THEME_ID,
                    "renderer_revision": NOTE_MARKDOWN_RENDERER_REVISION,
                }
                note.highlighted_html_object_key = None
                note.highlight_map_object_key = None
                db.commit()
                summary["updated"] += 1

                for object_key in (old_highlighted_key, old_highlight_map_key):
                    if object_key:
                        try:
                            storage.delete_object(storage.bucket, object_key)
                        except Exception as exc:
                            summary["failed"].append(
                                {"note_id": note.id, "stage": "delete_old_highlight", "error": str(exc)}
                            )

                if (
                    old_highlighted_key
                    and latest_versions.get(note.learning_unit_id) == note.version_no
                ):
                    active = TaskService(db).find_active_task(
                        workspace_id=note.workspace_id,
                        task_type="highlight_study_notes",
                        resource_type="learning_unit",
                        resource_id=note.learning_unit_id,
                    )
                    if active is None:
                        task = TaskService(db).create_task(
                            workspace_id=note.workspace_id,
                            task_type="highlight_study_notes",
                            resource_type="learning_unit",
                            resource_id=note.learning_unit_id,
                            payload={
                                "learning_unit_id": note.learning_unit_id,
                                "study_note_version_id": note.id,
                                "mistake_ids": list(note.source_mistake_ids or []),
                                "reason": "note_renderer_updated",
                            },
                        )
                        summary["highlight_tasks"].append(task.id)
            except Exception as exc:
                db.rollback()
                if args.apply and old_html is not None:
                    try:
                        _put_bytes(storage, note.html_object_key, old_html)
                    except Exception as restore_exc:
                        summary["failed"].append(
                            {"note_id": note_id, "stage": "restore_html", "error": str(restore_exc)}
                        )
                summary["failed"].append(
                    {"note_id": note_id, "stage": "rerender", "error": str(exc)}
                )

    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

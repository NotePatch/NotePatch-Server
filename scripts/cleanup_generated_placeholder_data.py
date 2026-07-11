from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "backend" / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select
from sqlalchemy.engine import make_url

from notepatch.platform.config import get_settings
from notepatch.platform.database import SessionLocal
from notepatch.modules.documents.models.document import Document, DocumentArtifact
from notepatch.modules.learning.models.homework import GradingResult, Mistake, Question
from notepatch.modules.learning.models.knowledge import KnowledgeChunk
from notepatch.modules.learning.models.learning import StudyNoteVersion
from notepatch.platform.storage import StorageService
from notepatch.modules.tasks.services.task import TaskService


OLD_SKILLS = {
    "notepatch-kb-builder",
    "notepatch-scholar-notes",
    "notepatch-grading",
    "notepatch-note-highlighter",
}
REMOVABLE_ARTIFACT_TYPES = {
    "ocr_json",
    "ocr_markdown",
    "ocr_text",
    "layout_json",
    "formula_json",
    "tables_json",
    "questions_json",
    "grading_report",
    "summary",
    "flashcards",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Remove historical generated placeholder data and requeue documents")
    parser.add_argument("--workspace-id")
    parser.add_argument("--backup-dir", default="/tmp/notepatch-backups")
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def is_historical(metadata: dict | None) -> bool:
    value = metadata or {}
    return value.get("mock") is True or value.get("skill") in OLD_SKILLS or str(value.get("source", "")).startswith("mock")


def pg_dump(backup_dir: str) -> Path:
    settings = get_settings()
    url = make_url(settings.database_url)
    if not url.drivername.startswith("postgresql"):
        raise RuntimeError("Cleanup backup requires PostgreSQL")
    target_dir = Path(backup_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"notepatch-before-generated-cleanup-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.dump"
    plain_url = url.set(drivername="postgresql", password=None).render_as_string(hide_password=False)
    env = dict(os.environ)
    if url.password:
        env["PGPASSWORD"] = url.password
    subprocess.run(
        ["pg_dump", "--format=custom", "--file", str(path), plain_url],
        check=True,
        env=env,
    )
    return path


def main() -> None:
    args = parse_args()
    storage = StorageService()
    with SessionLocal() as db:
        document_query = select(Document).where(Document.status != "deleted")
        if args.workspace_id:
            document_query = document_query.where(Document.workspace_id == args.workspace_id)
        documents = db.scalars(document_query).all()
        document_ids = {item.id for item in documents}
        artifact_rows = db.scalars(
            select(DocumentArtifact).where(DocumentArtifact.document_id.in_(document_ids))
        ).all() if document_ids else []
        artifacts = [
            item
            for item in artifact_rows
            if item.artifact_type in REMOVABLE_ARTIFACT_TYPES and is_historical(item.metadata_)
        ]
        questions = [
            item
            for item in db.scalars(select(Question).where(Question.document_id.in_(document_ids))).all()
            if is_historical(item.metadata_)
        ] if document_ids else []
        chunks = [
            item
            for item in db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.document_id.in_(document_ids))).all()
            if is_historical(item.metadata_)
        ] if document_ids else []
        workspace_ids = {item.workspace_id for item in documents}
        grading_rows = db.scalars(
            select(GradingResult).where(GradingResult.workspace_id.in_(workspace_ids))
        ).all() if workspace_ids else []
        gradings = [item for item in grading_rows if is_historical(item.metadata_)]
        grading_ids = {item.id for item in gradings}
        mistakes = db.scalars(
            select(Mistake).where(Mistake.grading_result_id.in_(grading_ids))
        ).all() if grading_ids else []
        note_rows = db.scalars(
            select(StudyNoteVersion).where(StudyNoteVersion.workspace_id.in_(workspace_ids))
        ).all() if workspace_ids else []
        notes = [item for item in note_rows if is_historical(item.metadata_)]
        affected_ids = {
            *(item.document_id for item in artifacts),
            *(item.document_id for item in questions if item.document_id),
            *(item.document_id for item in chunks if item.document_id),
        }
        object_locations = {(item.bucket, item.object_key) for item in artifacts}
        object_locations.update(
            (storage.bucket, key)
            for note in notes
            for key in (
                note.markdown_object_key,
                note.json_object_key,
                note.highlighted_object_key,
                note.highlight_map_object_key,
            )
            if key
        )
        object_locations.update(
            (storage.bucket, grading.report_storage_key)
            for grading in gradings
            if grading.report_storage_key
        )
        print(
            {
                "apply": args.apply,
                "documents_to_reprocess": len(affected_ids),
                "artifacts": len(artifacts),
                "questions": len(questions),
                "knowledge_chunks": len(chunks),
                "gradings": len(gradings),
                "mistakes": len(mistakes),
                "study_notes": len(notes),
                "objects": len(object_locations),
            }
        )
        if not args.apply:
            return
        backup_path = pg_dump(args.backup_dir)
        print({"backup": str(backup_path)})
        for bucket, key in object_locations:
            storage.delete_object(bucket, key)
        for rows in (mistakes, gradings, questions, chunks, artifacts, notes):
            for item in rows:
                db.delete(item)
            db.flush()
        for document in documents:
            if document.id in affected_ids:
                document.status = "uploaded"
        db.commit()
        service = TaskService(db)
        for document in documents:
            if document.id not in affected_ids:
                continue
            service.create_task(
                workspace_id=document.workspace_id,
                task_type="document_processing_pipeline",
                resource_type="document",
                resource_id=document.id,
                payload={
                    "document_id": document.id,
                    "force_reprocess": True,
                    "options": {"auto_learning": True, "force_reprocess": True},
                },
            )
        print({"requeued": len(affected_ids)})


if __name__ == "__main__":
    main()

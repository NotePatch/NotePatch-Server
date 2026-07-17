from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "backend" / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select

from notepatch.modules.learning.models.homework import GradingResult, Homework, Mistake
from notepatch.modules.learning.models.knowledge import KnowledgeChunk
from notepatch.modules.learning.models.learning import (
    Flashcard,
    FlashcardDeck,
    KnowledgePointAttempt,
    LearningUnit,
    StudyNoteVersion,
)
from notepatch.modules.learning.services.embedding import EmbeddingClient
from notepatch.modules.learning.services.knowledge_points import KnowledgePointService
from notepatch.modules.learning.services.workflow import LearningWorkflowService
from notepatch.modules.tasks.models.task import Task
from notepatch.modules.tasks.services.task import TaskService
from notepatch.platform.config import get_settings
from notepatch.platform.database import SessionLocal
from notepatch.platform.storage import StorageService


RESET_TASK_TYPES = ("generate_study_notes", "generate_flashcards", "highlight_study_notes")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Delete Markdown notes and schedule native HTML note rebuilds")
    parser.add_argument("--workspace-id")
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    storage = StorageService()
    embedding = EmbeddingClient()
    with SessionLocal() as db:
        unit_query = select(LearningUnit)
        if args.workspace_id:
            unit_query = unit_query.where(LearningUnit.workspace_id == args.workspace_id)
        units = db.scalars(unit_query).all()
        unit_ids = {unit.id for unit in units}
        notes = db.scalars(select(StudyNoteVersion).where(StudyNoteVersion.learning_unit_id.in_(unit_ids))).all() if unit_ids else []
        decks = db.scalars(select(FlashcardDeck).where(FlashcardDeck.learning_unit_id.in_(unit_ids))).all() if unit_ids else []
        deck_ids = {deck.id for deck in decks}
        cards = db.scalars(select(Flashcard).where(Flashcard.deck_id.in_(deck_ids))).all() if deck_ids else []
        tasks = db.scalars(
            select(Task).where(Task.resource_type == "learning_unit", Task.resource_id.in_(unit_ids), Task.task_type.in_(RESET_TASK_TYPES))
        ).all() if unit_ids else []
        object_keys = {
            key
            for note in notes
            for key in (
                note.html_object_key,
                note.json_object_key,
                note.highlighted_html_object_key,
                note.highlight_map_object_key,
            )
            if key
        }
        print(
            {
                "apply": args.apply,
                "learning_units": len(units),
                "study_notes": len(notes),
                "flashcard_decks": len(decks),
                "flashcards": len(cards),
                "tasks": len(tasks),
                "objects": len(object_keys),
            }
        )
        if not args.apply:
            return

        task_service = TaskService(db)
        for task in tasks:
            if task.status in {"queued", "running"}:
                task_service.request_cancel(task, "Study notes are being reset to native HTML", commit=False)
        db.commit()
        for key in object_keys:
            storage.delete_object(storage.bucket, key)
        for task in tasks:
            storage.delete_prefix(f"workspaces/{task.workspace_id}/sandbox/tasks/{task.id}/")
            task.result = {"reset": True, "reason": "native_html_notes"}
            task.error_message = None

        for rows in (cards, decks, notes):
            for row in rows:
                db.delete(row)
            db.flush()

        point_service = KnowledgePointService(
            db,
            embedding,
            match_threshold=get_settings().knowledge_point_match_threshold,
        )
        rebuilt_units: list[LearningUnit] = []
        for unit in units:
            chunks = [
                chunk
                for chunk in db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.workspace_id == unit.workspace_id)).all()
                if (chunk.metadata_ or {}).get("learning_unit_id") == unit.id
            ]
            if not chunks:
                continue
            names = [str((chunk.metadata_ or {}).get("title") or chunk.content[:120]).strip() for chunk in chunks]
            vectors = {name: list(chunk.embedding) for name, chunk in zip(names, chunks, strict=True) if chunk.embedding is not None}
            points = point_service.resolve_many(
                unit=unit,
                names=names,
                source_document_ids=list(dict.fromkeys(chunk.document_id for chunk in chunks if chunk.document_id)),
                vectors=vectors,
                owner=f"reset:{unit.id}:knowledge-points",
            )
            for chunk, name in zip(chunks, names, strict=True):
                chunk.metadata_ = {**(chunk.metadata_ or {}), "knowledge_point_id": points[name].id}

            mistakes = [
                mistake
                for mistake in db.scalars(select(Mistake).where(Mistake.workspace_id == unit.workspace_id)).all()
                if (mistake.metadata_ or {}).get("learning_unit_id") == unit.id and mistake.knowledge_point
            ]
            mistake_points = point_service.resolve_many(
                unit=unit,
                names=[mistake.knowledge_point for mistake in mistakes if mistake.knowledge_point],
                owner=f"reset:{unit.id}:mistakes",
            )
            existing_backfills = {
                (attempt.metadata_ or {}).get("backfilled_from_mistake_id")
                for attempt in db.scalars(
                    select(KnowledgePointAttempt).where(
                        KnowledgePointAttempt.workspace_id == unit.workspace_id,
                        KnowledgePointAttempt.learning_unit_id == unit.id,
                    )
                ).all()
            }
            attempts_added = 0
            for mistake in mistakes:
                point = mistake_points.get(mistake.knowledge_point)
                if point is None:
                    continue
                mistake.knowledge_point_id = point.id
                if mistake.id in existing_backfills:
                    continue
                grading = db.get(GradingResult, mistake.grading_result_id) if mistake.grading_result_id else None
                homework = db.get(Homework, grading.homework_id) if grading else None
                db.add(
                    KnowledgePointAttempt(
                        workspace_id=unit.workspace_id,
                        learning_unit_id=unit.id,
                        knowledge_point_id=point.id,
                        student_user_id=mistake.student_user_id,
                        homework_id=homework.id if homework else None,
                        grading_result_id=grading.id if grading else None,
                        question_id=mistake.question_id,
                        outcome="incorrect",
                        score_ratio=0.0,
                        occurred_at=mistake.created_at,
                        metadata_={"backfilled_from_mistake_id": mistake.id},
                    )
                )
                attempts_added += 1
            unit.knowledge_revision = max(unit.knowledge_revision, 1)
            if attempts_added:
                unit.attempt_revision += 1
            rebuilt_units.append(unit)
        db.commit()

        workflow = LearningWorkflowService(db, storage, embedding_client=embedding)
        scheduled = [workflow.schedule_study_notes(unit, reason="native_html_reset") for unit in rebuilt_units]
        print({"deleted_notes": len(notes), "scheduled_html_notes": len(scheduled)})


if __name__ == "__main__":
    main()

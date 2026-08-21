from __future__ import annotations

from sqlalchemy import delete, select

from notepatch.modules.learning.models.learning import (
    Flashcard,
    FlashcardDeck,
    KnowledgePoint,
    StudyNoteVersion,
)
from notepatch.modules.learning.models.knowledge import KnowledgeChunk
from notepatch.modules.learning.models.note_workflow import (
    NoteGapSuggestion,
    NoteSupplementDraft,
    StudyNoteCorrection,
)
from notepatch.modules.learning.schemas.skills import NoteSupplementResult
from notepatch.modules.learning.services.html_notes import sanitize_note_html
from notepatch.platform.errors import PermanentTaskError, RetryableTaskError
from notepatch.platform.storage import StorageService


class NoteGapOperations:
    def _latest_note_version(self, workspace_id: str, learning_unit_id: str) -> StudyNoteVersion | None:
        return self.db.scalar(
            select(StudyNoteVersion)
            .where(
                StudyNoteVersion.workspace_id == workspace_id,
                StudyNoteVersion.learning_unit_id == learning_unit_id,
            )
            .order_by(StudyNoteVersion.version_no.desc())
        )

    def _gap_source_refs(self, point: KnowledgePoint) -> list[dict]:
        chunks = self.db.scalars(
            select(KnowledgeChunk).where(
                KnowledgeChunk.workspace_id == point.workspace_id,
                KnowledgeChunk.metadata_["learning_unit_id"].as_string() == point.learning_unit_id,
                KnowledgeChunk.metadata_["knowledge_point_id"].as_string() == point.id,
            )
        ).all()
        refs: list[dict] = []
        for chunk in chunks:
            page_refs = list((chunk.metadata_ or {}).get("page_refs") or [0])
            refs.append(
                {
                    "document_id": chunk.document_id,
                    "page_index": int(page_refs[0]) if page_refs else 0,
                    "block_id": f"knowledge-chunk:{chunk.id}",
                    "bbox": None,
                    "excerpt": chunk.content[:500],
                    "knowledge_chunk_id": chunk.id,
                }
            )
        if not refs:
            refs.extend(
                {
                    "document_id": document_id,
                    "page_index": 0,
                    "block_id": None,
                    "bbox": None,
                    "excerpt": point.name,
                }
                for document_id in (point.source_document_ids or [])
            )
        return refs

    def _target_for_gap(self, note: StudyNoteVersion | None) -> tuple[str | None, str | None]:
        if note is None:
            return None, None
        section_id = None
        try:
            payload = self.storage.get_json_artifact(note.json_object_key, bucket=self.storage.bucket)
            points = list(payload.get("knowledge_points") or [])
            if points:
                section_id = str(points[-1].get("section_id") or "") or None
        except Exception:
            section_id = None
        if section_id is None and note.knowledge_point_ids:
            section_id = f"kp-{note.knowledge_point_ids[-1]}"
        return section_id, f"#{section_id}" if section_id else None

    def detect_note_gaps(self, task) -> dict:
        unit = self._learning_unit(task.payload.get("learning_unit_id") or task.resource_id, task.workspace_id)
        note = self._latest_note_version(task.workspace_id, unit.id)
        covered = set(note.knowledge_point_ids or []) if note else set()
        points = self.db.scalars(
            select(KnowledgePoint).where(
                KnowledgePoint.workspace_id == task.workspace_id,
                KnowledgePoint.learning_unit_id == unit.id,
            )
        ).all()
        target_section_id, target_anchor = self._target_for_gap(note)
        created: list[str] = []
        stale: list[str] = []
        for existing in self.db.scalars(
            select(NoteGapSuggestion).where(
                NoteGapSuggestion.workspace_id == task.workspace_id,
                NoteGapSuggestion.learning_unit_id == unit.id,
                NoteGapSuggestion.status.in_(("pending", "draft", "no_base_note")),
            )
        ).all():
            if existing.knowledge_point_id in covered or existing.note_version_id != (note.id if note else None):
                existing.status = "stale"
                stale.append(existing.id)

        for point in points:
            if point.id in covered:
                continue
            suggestion = self.db.scalar(
                select(NoteGapSuggestion).where(
                    NoteGapSuggestion.workspace_id == task.workspace_id,
                    NoteGapSuggestion.learning_unit_id == unit.id,
                    NoteGapSuggestion.knowledge_point_id == point.id,
                    NoteGapSuggestion.note_version_id == (note.id if note else None),
                )
            )
            source_refs = self._gap_source_refs(point)
            if suggestion is None:
                suggestion = NoteGapSuggestion(
                    workspace_id=task.workspace_id,
                    learning_unit_id=unit.id,
                    knowledge_point_id=point.id,
                    note_version_id=note.id if note else None,
                )
                self.db.add(suggestion)
                created.append(suggestion.id)
            suggestion.detected_by_task_id = task.id
            suggestion.status = "pending" if note else "no_base_note"
            suggestion.coverage_score = 0.0
            suggestion.source_refs = source_refs
            suggestion.target_section_id = target_section_id
            suggestion.target_anchor = target_anchor
            suggestion.insert_position = "after"
            suggestion.metadata_ = {
                **(suggestion.metadata_ or {}),
                "knowledge_point_name": point.name,
                "source": task.payload.get("reason") or "learning_workflow",
            }
        self.db.commit()
        return {
            "learning_unit_id": unit.id,
            "note_version_id": note.id if note else None,
            "gaps_created": len(created),
            "gap_ids": created,
            "gaps_staled": len(stale),
            "has_base_note": note is not None,
        }

    def generate_note_supplement(self, task) -> dict:
        draft = self.db.scalar(
            select(NoteSupplementDraft).where(
                NoteSupplementDraft.workspace_id == task.workspace_id,
                NoteSupplementDraft.id == (task.payload.get("draft_id") or task.resource_id),
            )
        )
        if draft is None:
            raise PermanentTaskError("Note supplement draft not found")
        gap = self.db.scalar(
            select(NoteGapSuggestion).where(
                NoteGapSuggestion.workspace_id == task.workspace_id,
                NoteGapSuggestion.learning_unit_id == draft.learning_unit_id,
                NoteGapSuggestion.id == draft.gap_suggestion_id,
            )
        )
        if gap is None or gap.status in {"stale", "accepted", "rejected"}:
            raise PermanentTaskError("Note gap suggestion is no longer actionable")
        point = self.db.scalar(
            select(KnowledgePoint).where(
                KnowledgePoint.workspace_id == task.workspace_id,
                KnowledgePoint.learning_unit_id == draft.learning_unit_id,
                KnowledgePoint.id == gap.knowledge_point_id,
            )
        )
        if point is None:
            raise PermanentTaskError("Knowledge point not found")
        result, run = self._skill().execute(
            task=task,
            skill_name="notepatch_note_supplement",
            input_payload={
                "knowledge_point": {"id": point.id, "name": point.name},
                "source_refs": draft.selected_source_refs or gap.source_refs,
                "target": {
                    "base_note_version_id": draft.base_note_version_id,
                    "section_id": draft.target_section_id,
                    "insert_position": draft.insert_position,
                },
                "instruction": draft.instruction,
                "feedback": draft.feedback,
            },
            output_filename="note_supplement.json",
            schema=NoteSupplementResult,
        )
        try:
            html = sanitize_note_html(result.html)
        except ValueError as exc:
            raise PermanentTaskError(str(exc)) from exc
        draft.html = html
        draft.status = "ready"
        draft.generated_by_task_id = task.id
        gap.status = "draft"
        self.db.commit()
        return {
            "learning_unit_id": draft.learning_unit_id,
            "gap_id": gap.id,
            "draft_id": draft.id,
            "output_key": run["output_key"],
        }

    def purge_study_note_history(self, task, storage: StorageService) -> dict:
        unit = self._learning_unit(task.payload.get("learning_unit_id") or task.resource_id, task.workspace_id)
        keep_history = max(0, min(100, int(task.payload.get("keep_history", 3))))
        notes = self.db.scalars(
            select(StudyNoteVersion)
            .where(
                StudyNoteVersion.workspace_id == task.workspace_id,
                StudyNoteVersion.learning_unit_id == unit.id,
            )
            .order_by(StudyNoteVersion.version_no.desc())
        ).all()
        obsolete = notes[keep_history + 1 :]
        deleted_ids: list[str] = []
        for note in obsolete:
            keys = {
                note.html_object_key,
                note.json_object_key,
                note.note_ir_object_key,
                note.highlighted_html_object_key,
                note.highlight_map_object_key,
                (note.metadata_ or {}).get("skill_output_key"),
                *[
                    item.get("object_key")
                    for item in ((note.metadata_ or {}).get("visual_assets") or {}).values()
                    if isinstance(item, dict)
                ],
            }
            for object_key in filter(None, keys):
                try:
                    storage.delete_object(storage.bucket, object_key)
                except Exception as exc:
                    if not StorageService.is_object_not_found_error(exc):
                        raise RetryableTaskError("Study note history storage cleanup failed") from exc
            deck_ids = self.db.scalars(
                select(FlashcardDeck.id).where(
                    FlashcardDeck.workspace_id == task.workspace_id,
                    FlashcardDeck.study_note_version_id == note.id,
                )
            ).all()
            if deck_ids:
                self.db.execute(delete(Flashcard).where(Flashcard.deck_id.in_(deck_ids)))
                self.db.execute(delete(FlashcardDeck).where(FlashcardDeck.id.in_(deck_ids)))
            for gap in self.db.scalars(
                select(NoteGapSuggestion).where(
                    NoteGapSuggestion.workspace_id == task.workspace_id,
                    NoteGapSuggestion.learning_unit_id == unit.id,
                    NoteGapSuggestion.note_version_id == note.id,
                    NoteGapSuggestion.status.in_(("pending", "draft", "no_base_note")),
                )
            ).all():
                gap.status = "stale"
            self.db.execute(delete(StudyNoteCorrection).where(StudyNoteCorrection.note_version_id == note.id))
            self.db.delete(note)
            deleted_ids.append(note.id)
        self.db.commit()
        return {
            "learning_unit_id": unit.id,
            "history_limit": keep_history,
            "deleted_note_version_ids": deleted_ids,
            "latest_note_version_id": notes[0].id if notes else None,
        }

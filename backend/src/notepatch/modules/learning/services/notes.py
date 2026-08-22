from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from notepatch.modules.identity.models.user import User
from notepatch.modules.learning.models.homework import Mistake
from notepatch.modules.learning.models.learning import LearningUnit, StudyNoteVersion
from notepatch.modules.learning.models.note_workflow import StudyNoteCorrection
from notepatch.modules.tasks.models.task import Task
from notepatch.modules.tasks.services.task import TaskService
from notepatch.platform.storage import StorageService
from notepatch.modules.learning.services.html_notes import sanitize_note_html, validate_knowledge_point_references


class StudyNoteService:
    def __init__(self, db: Session, storage: StorageService) -> None:
        self.db = db
        self.storage = storage

    def create_revision(
        self,
        *,
        workspace_id: str,
        learning_unit_id: str,
        base_version_id: str,
        actor: User,
        html: str,
        title: str | None,
        edit_summary: str | None,
        edit_origin: str = "user",
        knowledge_point_ids: list[str] | None = None,
        source_document_ids: list[str] | None = None,
    ) -> tuple[StudyNoteVersion, list[Task]]:
        unit = self.db.scalar(
            select(LearningUnit)
            .where(LearningUnit.workspace_id == workspace_id, LearningUnit.id == learning_unit_id)
            .with_for_update()
        )
        if unit is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Learning unit not found")
        latest = self.db.scalar(
            select(StudyNoteVersion)
            .where(
                StudyNoteVersion.workspace_id == workspace_id,
                StudyNoteVersion.learning_unit_id == learning_unit_id,
            )
            .order_by(StudyNoteVersion.version_no.desc())
        )
        if latest is None or latest.id != base_version_id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Study note version is not the latest")

        try:
            clean_html = sanitize_note_html(html)
            effective_point_ids = list(
                dict.fromkeys(knowledge_point_ids if knowledge_point_ids is not None else latest.knowledge_point_ids or [])
            )
            validate_knowledge_point_references(clean_html, set(effective_point_ids))
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
        structured = self._download_json(latest.json_object_key)
        structured["title"] = title.strip() if isinstance(title, str) and title.strip() else latest.title
        structured["html"] = clean_html
        effective_source_ids = list(
            dict.fromkeys(source_document_ids if source_document_ids is not None else latest.source_document_ids or [])
        )
        structured["source_document_ids"] = effective_source_ids
        structured["knowledge_point_ids"] = effective_point_ids
        structured["revision"] = {
            "source_version_id": latest.id,
            "edited_by_user_id": actor.id,
            "edit_origin": edit_origin,
            "edit_summary": edit_summary,
        }

        version_id = str(uuid.uuid4())
        html_key = StorageService.learning_unit_note_key(
            workspace_id, learning_unit_id, version_id, "study_note", "html"
        )
        json_key = StorageService.learning_unit_note_key(
            workspace_id, learning_unit_id, version_id, "study_note", "json"
        )
        ir_key = (
            StorageService.learning_unit_note_key(
                workspace_id, learning_unit_id, version_id, "note_ir", "json"
            )
            if latest.note_ir_object_key else None
        )
        uploaded: list[str] = []
        try:
            self._put_text(html_key, clean_html)
            uploaded.append(html_key)
            self.storage.put_json_artifact(json_key, structured, bucket=self.storage.bucket)
            uploaded.append(json_key)
            if ir_key:
                self.storage.copy_object(
                    self.storage.bucket,
                    latest.note_ir_object_key,
                    self.storage.bucket,
                    ir_key,
                )
                uploaded.append(ir_key)
            note_metadata = {**(latest.metadata_ or {}), "manual_revision": True}
            note_metadata.pop("visual_assets", None)
            note_metadata["source_images_embedded"] = False
            note = StudyNoteVersion(
                id=version_id,
                workspace_id=workspace_id,
                learning_unit_id=learning_unit_id,
                version_no=latest.version_no + 1,
                title=structured["title"],
                html_object_key=html_key,
                json_object_key=json_key,
                note_ir_object_key=ir_key,
                content_edit_level=latest.content_edit_level,
                layout_edit_level=latest.layout_edit_level,
                knowledge_point_ids=effective_point_ids,
                source_document_ids=effective_source_ids,
                source_mistake_ids=list(latest.source_mistake_ids or []),
                source_version_id=latest.id,
                edited_by_user_id=actor.id,
                edit_origin=edit_origin,
                edit_summary=edit_summary,
                metadata_=note_metadata,
            )
            self.db.add(note)
            self.db.flush()
            for correction in self.db.scalars(
                select(StudyNoteCorrection).where(
                    StudyNoteCorrection.workspace_id == workspace_id,
                    StudyNoteCorrection.learning_unit_id == learning_unit_id,
                    StudyNoteCorrection.note_version_id == latest.id,
                )
            ).all():
                self.db.add(
                    StudyNoteCorrection(
                        workspace_id=workspace_id,
                        learning_unit_id=learning_unit_id,
                        note_version_id=note.id,
                        source_block_id=correction.source_block_id,
                        correction_type=correction.correction_type,
                        original_text=correction.original_text,
                        corrected_text=correction.corrected_text,
                        reason=correction.reason,
                        confidence=correction.confidence,
                        source_refs=list(correction.source_refs or []),
                    )
                )
            self.db.commit()
            self.db.refresh(note)
        except Exception:
            self.db.rollback()
            for object_key in uploaded:
                try:
                    self.storage.delete_object(self.storage.bucket, object_key)
                except Exception:
                    pass
            raise

        tasks = [
            TaskService(self.db).create_task(
                workspace_id=workspace_id,
                task_type="generate_flashcards",
                resource_type="learning_unit",
                resource_id=learning_unit_id,
                payload={
                    "learning_unit_id": learning_unit_id,
                    "study_note_version_id": note.id,
                    "expected_attempt_revision": unit.attempt_revision,
                    "reason": "study_note_revised",
                },
            )
        ]
        mistakes = self.db.scalars(
            select(Mistake).where(Mistake.workspace_id == workspace_id, Mistake.status == "open")
        ).all()
        mistake_ids = [
            item.id
            for item in mistakes
            if (item.metadata_ or {}).get("learning_unit_id") == learning_unit_id
        ]
        if mistake_ids:
            tasks.append(
                TaskService(self.db).create_task(
                    workspace_id=workspace_id,
                    task_type="highlight_study_notes",
                    resource_type="learning_unit",
                    resource_id=learning_unit_id,
                    payload={
                        "learning_unit_id": learning_unit_id,
                        "mistake_ids": mistake_ids,
                        "expected_note_version_id": note.id,
                        "reason": "study_note_revised",
                    },
                )
            )
        tasks.append(
            TaskService(self.db).create_task(
                workspace_id=workspace_id,
                task_type="purge_study_note_history",
                resource_type="learning_unit",
                resource_id=learning_unit_id,
                payload={
                    "learning_unit_id": learning_unit_id,
                    "keep_history": actor.note_history_limit,
                },
            )
        )
        return note, tasks

    def _download_json(self, object_key: str) -> dict:
        try:
            return self.storage.get_json_artifact(object_key, bucket=self.storage.bucket)
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Study note JSON is invalid") from exc

    def _put_text(self, object_key: str, content: str) -> None:
        with tempfile.TemporaryDirectory(prefix="notepatch-note-") as directory:
            path = Path(directory) / "study_note.html"
            path.write_text(content, encoding="utf-8")
            self.storage.put_file(
                self.storage.bucket,
                object_key,
                path,
                content_type="text/html; charset=utf-8",
            )

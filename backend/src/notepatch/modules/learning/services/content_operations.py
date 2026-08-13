from __future__ import annotations

import uuid

from sqlalchemy import delete, func, select

from notepatch.modules.learning.models.homework import Mistake, Question
from notepatch.modules.learning.models.knowledge import KnowledgeChunk
from notepatch.modules.learning.models.learning import (
    Flashcard,
    FlashcardDeck,
    KnowledgePoint,
    LearningUnit,
    StudyNoteVersion,
)
from notepatch.modules.learning.schemas.skills import (
    FlashcardsSkillResult,
    KnowledgeBuildResult,
    QuestionExtractionResult,
    ScholarNotesResult,
)
from notepatch.modules.tasks.models.task import Task
from notepatch.platform.errors import PermanentTaskError
from notepatch.platform.storage import StorageService
from notepatch.modules.learning.services.flashcard_priority import FlashcardPriorityService
from notepatch.modules.learning.services.html_notes import sanitize_note_html, validate_knowledge_point_references
from notepatch.modules.learning.services.knowledge_points import KnowledgePointService
from notepatch.platform.config import get_settings


class LearningContentOperations:
    def extract_questions(self, task: Task, storage: StorageService) -> dict:
        existing = self._artifact_for_task(task, "questions_json")
        if existing is not None:
            return {"document_id": existing.document_id, "artifact_id": existing.id, "output_key": existing.object_key}
        document = self._document(task.payload.get("document_id") or task.resource_id, task.workspace_id)
        source_text = self._required_ocr_text(document)
        learning_unit = self.ensure_learning_unit_for_document(document)
        result, run = self._skill().execute(
            task=task,
            skill_name="notepatch_question_extractor",
            input_payload={
                "document": self._document_payload(document),
                "learning_unit": self._learning_unit_payload(learning_unit),
                "ocr_text": source_text,
            },
            output_filename="questions.json",
            schema=QuestionExtractionResult,
        )
        self._ensure_active(task)
        artifact = self._store_document_json_artifact(
            task=task,
            document=document,
            artifact_type="questions_json",
            filename="questions.json",
            payload=result.model_dump(mode="json"),
            metadata={"skill": "notepatch_question_extractor", "source_ocr_run_id": task.payload.get("source_ocr_run_id")},
        )
        homework = None
        if document.document_kind in {"homework", "corrected_homework"}:
            homework = self.ensure_homework_for_document(document, learning_unit)
        for item in result.questions:
            self._ensure_active(task)
            self.db.add(
                Question(
                    workspace_id=task.workspace_id,
                    document_id=document.id,
                    homework_id=homework.id if homework else None,
                    sequence_no=item.sequence_no,
                    question_type=item.question_type,
                    prompt=item.prompt,
                    answer=item.answer,
                    metadata_={
                        "skill": "notepatch_question_extractor",
                        "task_id": task.id,
                        "page_refs": item.page_refs,
                        "evidence": item.evidence,
                    },
                )
            )
        self._ensure_active(task)
        self.db.commit()
        downstream: list[Task] = []
        if homework is not None:
            self._ensure_active(task)
            downstream = self._create_unique_tasks(
                [
                    (
                        "grade_homework",
                        "homework",
                        homework.id,
                        {
                            "homework_id": homework.id,
                            "document_id": document.id,
                            "learning_unit_id": learning_unit.id,
                            "source_question_task_id": task.id,
                        },
                    )
                ],
                force_reprocess=bool(task.payload.get("force_reprocess")),
            )
        return {
            "document_id": document.id,
            "artifact_id": artifact.id,
            "questions_created": len(result.questions),
            "output_key": artifact.object_key,
            "skill_output_key": run["output_key"],
            "downstream_tasks": [{"id": item.id, "task_type": item.task_type} for item in downstream],
        }

    def build_knowledge_base(self, task: Task, storage: StorageService) -> dict:
        document = self._document(task.payload.get("document_id") or task.resource_id, task.workspace_id)
        learning_unit = self.ensure_learning_unit_for_document(document)
        existing = self.db.scalars(
            select(KnowledgeChunk).where(
                KnowledgeChunk.workspace_id == task.workspace_id,
                KnowledgeChunk.metadata_["task_id"].as_string() == task.id,
            )
        ).all()
        if existing:
            note_task = self.schedule_study_notes(learning_unit, reason="knowledge_reused")
            return {
                "chunks_created": 0,
                "chunk_ids": [chunk.id for chunk in existing],
                "reused": True,
                "downstream_tasks": [{"id": note_task.id, "task_type": note_task.task_type}],
            }
        source_text = self._required_ocr_text(document)
        result, run = self._skill().execute(
            task=task,
            skill_name="notepatch_kb_builder",
            input_payload={
                "document": self._document_payload(document),
                "learning_unit": self._learning_unit_payload(learning_unit),
                "ocr_text": source_text,
            },
            output_filename="knowledge_chunks.json",
            schema=KnowledgeBuildResult,
        )
        self._ensure_active(task)
        vectors = self.embedding_client.embed(
            [chunk.content for chunk in result.chunks],
            owner=f"task:{task.id}:knowledge",
            event_callback=lambda event, data: self._record_task_event(task, event, data),
        )
        self._ensure_active(task)
        points = KnowledgePointService(
            self.db,
            self.embedding_client,
            match_threshold=get_settings().knowledge_point_match_threshold,
        ).resolve_many(
            unit=learning_unit,
            names=[item.title for item in result.chunks],
            source_document_ids=[document.id],
            vectors={item.title: vector for item, vector in zip(result.chunks, vectors, strict=True)},
            owner=f"task:{task.id}:knowledge-points",
        )
        if bool(task.payload.get("force_reprocess")):
            self.db.execute(
                delete(KnowledgeChunk).where(
                    KnowledgeChunk.workspace_id == task.workspace_id,
                    KnowledgeChunk.document_id == document.id,
                )
            )
        chunk_ids: list[str] = []
        for item, vector in zip(result.chunks, vectors, strict=True):
            chunk = KnowledgeChunk(
                workspace_id=task.workspace_id,
                document_id=document.id,
                subject=item.subject or learning_unit.subject,
                grade_level=item.grade_level or learning_unit.grade_level,
                source_type="openclaw_skill",
                content=item.content,
                embedding=vector,
                metadata_={
                    "skill": "notepatch_kb_builder",
                    "task_id": task.id,
                    "learning_unit_id": learning_unit.id,
                    "title": item.title,
                    "key_terms": item.key_terms,
                    "page_refs": item.page_refs,
                    "difficulty": item.difficulty,
                    "prerequisites": item.prerequisites,
                    "order": item.order,
                    "source_ocr_run_id": task.payload.get("source_ocr_run_id"),
                    "knowledge_point_id": points[item.title].id,
                },
            )
            self.db.add(chunk)
            self.db.flush()
            chunk_ids.append(chunk.id)
        learning_unit.knowledge_revision += 1
        self._ensure_active(task)
        self.db.commit()
        self._ensure_active(task)
        note_task = self.schedule_study_notes(learning_unit, reason="knowledge_updated")
        return {
            "chunks_created": len(chunk_ids),
            "chunk_ids": chunk_ids,
            "output_key": run["output_key"],
            "learning_unit_id": learning_unit.id,
            "knowledge_revision": learning_unit.knowledge_revision,
            "downstream_tasks": [{"id": note_task.id, "task_type": note_task.task_type}],
        }

    def generate_study_notes(self, task: Task, storage: StorageService) -> dict:
        existing = self.db.scalar(select(StudyNoteVersion).where(StudyNoteVersion.task_id == task.id))
        if existing is not None:
            return {"learning_unit_id": existing.learning_unit_id, "study_note_version_id": existing.id, "reused": True}
        unit = self._learning_unit(task.payload.get("learning_unit_id") or task.resource_id, task.workspace_id)
        expected_revision = int(task.payload.get("expected_knowledge_revision", unit.knowledge_revision))
        if expected_revision != unit.knowledge_revision:
            replacement = self.schedule_study_notes(unit, reason="knowledge_changed_before_note_start")
            return {
                "learning_unit_id": unit.id,
                "skipped": True,
                "reason": "knowledge_revision_changed",
                "replacement_task_id": replacement.id,
            }
        documents = self._unit_documents(unit.id, task.workspace_id)
        chunks = self._unit_chunks(unit.id, task.workspace_id)
        if not chunks:
            raise PermanentTaskError("Cannot generate study notes before knowledge chunks exist")
        current_point_ids = {
            str((chunk.metadata_ or {}).get("knowledge_point_id"))
            for chunk in chunks
            if (chunk.metadata_ or {}).get("knowledge_point_id")
        }
        points = (
            self.db.scalars(
                select(KnowledgePoint).where(
                    KnowledgePoint.workspace_id == task.workspace_id,
                    KnowledgePoint.learning_unit_id == unit.id,
                    KnowledgePoint.id.in_(current_point_ids),
                )
            ).all()
            if current_point_ids
            else []
        )
        result, run = self._skill().execute(
            task=task,
            skill_name="notepatch_scholar_notes",
            input_payload={
                "learning_unit": self._learning_unit_payload(unit),
                "documents": [self._document_payload(document) for document in documents],
                "knowledge_chunks": [self._chunk_payload(chunk) for chunk in chunks],
                "knowledge_points": [
                    {"id": point.id, "name": point.name, "source_document_ids": point.source_document_ids}
                    for point in points
                ],
                "allowed_html_classes": sorted(
                    {
                        "np-note", "np-note-header", "np-note-title", "np-note-summary", "np-note-section",
                        "np-knowledge-point", "np-callout", "np-callout--tip", "np-callout--warning",
                        "np-formula", "np-table", "np-reinforcement",
                    }
                ),
            },
            output_filename="study_note.json",
            schema=ScholarNotesResult,
        )
        self._ensure_active(task)
        self.db.refresh(unit)
        if unit.knowledge_revision != expected_revision:
            replacement = self.schedule_study_notes(unit, reason="knowledge_changed_during_note_generation")
            return {
                "learning_unit_id": unit.id,
                "skipped": True,
                "reason": "knowledge_revision_changed",
                "replacement_task_id": replacement.id,
            }
        allowed_point_ids = {point.id for point in points}
        result_point_ids = {item.id for item in result.knowledge_points}
        if not result_point_ids.issubset(allowed_point_ids):
            raise PermanentTaskError("Scholar notes returned unknown knowledge point ids")
        try:
            html = sanitize_note_html(result.html)
            validate_knowledge_point_references(html, allowed_point_ids)
        except ValueError as exc:
            raise PermanentTaskError(str(exc)) from exc
        version_id = str(uuid.uuid4())
        version_no = int(
            self.db.scalar(
                select(func.coalesce(func.max(StudyNoteVersion.version_no), 0)).where(
                    StudyNoteVersion.workspace_id == task.workspace_id,
                    StudyNoteVersion.learning_unit_id == unit.id,
                )
            )
            or 0
        ) + 1
        html_key = StorageService.learning_unit_note_key(task.workspace_id, unit.id, version_id, "study_note", "html")
        json_key = StorageService.learning_unit_note_key(task.workspace_id, unit.id, version_id, "study_note", "json")
        structured = result.model_dump(mode="json")
        structured["html"] = html
        self._put_text(storage, html_key, html, "text/html; charset=utf-8")
        storage.put_json_artifact(json_key, structured, bucket=storage.bucket)
        self._ensure_active(task)
        note = StudyNoteVersion(
            id=version_id,
            workspace_id=task.workspace_id,
            learning_unit_id=unit.id,
            task_id=task.id,
            version_no=version_no,
            title=result.title,
            html_object_key=html_key,
            json_object_key=json_key,
            knowledge_point_ids=[item.id for item in result.knowledge_points],
            source_document_ids=result.source_document_ids or [document.id for document in documents],
            source_mistake_ids=[],
            edit_origin="skill",
            metadata_={"skill": "notepatch_scholar_notes", "skill_output_key": run["output_key"]},
        )
        self.db.add(note)
        unit.notes_generated_revision = expected_revision
        unit.note_generation_due_at = None
        self._ensure_active(task)
        self.db.commit()
        self._ensure_active(task)
        flashcard_task = self.schedule_flashcards(unit, note, reason="study_note_generated")
        downstream_tasks = [flashcard_task]
        mistakes = self.db.scalars(
            select(Mistake).where(
                Mistake.workspace_id == task.workspace_id,
                Mistake.status == "open",
            )
        ).all()
        mistake_ids = [
            mistake.id
            for mistake in mistakes
            if (mistake.metadata_ or {}).get("learning_unit_id") == unit.id
        ]
        if mistake_ids:
            downstream_tasks.extend(
                self._create_unique_tasks(
                    [
                        (
                            "highlight_study_notes",
                            "learning_unit",
                            unit.id,
                            {
                                "learning_unit_id": unit.id,
                                "mistake_ids": mistake_ids,
                                "expected_note_version_id": note.id,
                                "reason": "study_note_generated",
                            },
                        )
                    ],
                    force_reprocess=True,
                )
            )
        return {
            "learning_unit_id": unit.id,
            "study_note_version_id": note.id,
            "html_key": html_key,
            "json_key": json_key,
            "downstream_tasks": [
                {"id": downstream.id, "task_type": downstream.task_type}
                for downstream in downstream_tasks
            ],
        }

    def generate_flashcards(self, task: Task, storage: StorageService) -> dict:
        unit_id = task.payload.get("learning_unit_id") or task.resource_id
        if not isinstance(unit_id, str):
            document = self._document(task.payload.get("document_id"), task.workspace_id)
            unit_id = self.ensure_learning_unit_for_document(document).id
        unit = self._learning_unit(unit_id, task.workspace_id)
        note = self.db.scalar(
            select(StudyNoteVersion)
            .where(StudyNoteVersion.workspace_id == task.workspace_id, StudyNoteVersion.learning_unit_id == unit.id)
            .order_by(StudyNoteVersion.version_no.desc())
        )
        if note is None:
            return {"learning_unit_id": unit.id, "skipped": True, "reason": "no_study_note"}
        expected_note_id = task.payload.get("study_note_version_id") or note.id
        expected_attempt_revision = int(task.payload.get("expected_attempt_revision", unit.attempt_revision))
        existing_deck = self._flashcard_deck_for_revision(
            workspace_id=task.workspace_id,
            learning_unit_id=unit.id,
            study_note_version_id=expected_note_id,
            attempt_revision=expected_attempt_revision,
        )
        if existing_deck is not None:
            return {"learning_unit_id": unit.id, "flashcard_deck_id": existing_deck.id, "reused": True}
        if note.id != expected_note_id or unit.attempt_revision != expected_attempt_revision:
            replacement = self.schedule_flashcards(unit, note, reason="flashcard_source_changed")
            return {
                "learning_unit_id": unit.id,
                "skipped": True,
                "reason": "source_revision_changed",
                "replacement_task_id": replacement.id,
            }
        chunks = self._unit_chunks(unit.id, task.workspace_id)
        note_html = self._download_text(storage, storage.bucket, note.html_object_key)
        priority_service = FlashcardPriorityService(self.db)
        weighted_points = priority_service.calculate(
            workspace_id=task.workspace_id,
            learning_unit_id=unit.id,
            note=note,
        )
        if not weighted_points:
            return {"learning_unit_id": unit.id, "skipped": True, "reason": "no_knowledge_points"}
        result, run = self._skill().execute(
            task=task,
            skill_name="notepatch_flashcards",
            input_payload={
                "learning_unit": self._learning_unit_payload(unit),
                "study_note_html": note_html,
                "weighted_knowledge_points": weighted_points,
                "knowledge_chunks": [self._chunk_payload(chunk) for chunk in chunks],
            },
            output_filename="flashcards.json",
            schema=FlashcardsSkillResult,
        )
        self._ensure_active(task)
        self.db.refresh(unit)
        latest_note = self.db.scalar(
            select(StudyNoteVersion)
            .where(StudyNoteVersion.workspace_id == task.workspace_id, StudyNoteVersion.learning_unit_id == unit.id)
            .order_by(StudyNoteVersion.version_no.desc())
        )
        if latest_note is None or latest_note.id != expected_note_id or unit.attempt_revision != expected_attempt_revision:
            replacement = self.schedule_flashcards(unit, latest_note or note, reason="flashcard_source_changed_during_run")
            return {
                "learning_unit_id": unit.id,
                "skipped": True,
                "reason": "source_revision_changed",
                "replacement_task_id": replacement.id,
            }
        candidates = {item["id"]: item for item in weighted_points}
        returned_ids = [item.knowledge_point_id for item in result.flashcards]
        unknown_ids = sorted(set(returned_ids) - set(candidates))
        if unknown_ids:
            raise PermanentTaskError(f"Flashcards contain unknown knowledge point ids: {unknown_ids}")
        fingerprints = {
            (item.front.strip().casefold(), item.back.strip().casefold())
            for item in result.flashcards
        }
        if len(fingerprints) != len(result.flashcards):
            raise PermanentTaskError("Flashcards contain duplicate card content")
        locked_unit = self.db.scalar(select(LearningUnit).where(LearningUnit.id == unit.id).with_for_update())
        existing_deck = self._flashcard_deck_for_revision(
            workspace_id=task.workspace_id,
            learning_unit_id=unit.id,
            study_note_version_id=expected_note_id,
            attempt_revision=expected_attempt_revision,
        )
        if existing_deck is not None:
            del locked_unit
            self.db.commit()
            return {"learning_unit_id": unit.id, "flashcard_deck_id": existing_deck.id, "reused": True}
        version_no = int(
            self.db.scalar(
                select(func.coalesce(func.max(FlashcardDeck.version_no), 0)).where(
                    FlashcardDeck.workspace_id == task.workspace_id,
                    FlashcardDeck.learning_unit_id == unit.id,
                )
            )
            or 0
        ) + 1
        deck = FlashcardDeck(
            workspace_id=task.workspace_id,
            learning_unit_id=unit.id,
            study_note_version_id=note.id,
            task_id=task.id,
            version_no=version_no,
            attempt_revision=expected_attempt_revision,
            weighting_config=priority_service.weighting_config(),
            metadata_={"skill": "notepatch_flashcards", "skill_output_key": run["output_key"]},
        )
        self.db.add(deck)
        self.db.flush()
        for rank, item in enumerate(result.flashcards, start=1):
            candidate = candidates[item.knowledge_point_id]
            self.db.add(
                Flashcard(
                    workspace_id=task.workspace_id,
                    deck_id=deck.id,
                    knowledge_point_id=item.knowledge_point_id,
                    front=item.front,
                    back=item.back,
                    priority_score=candidate["priority_score"],
                    priority_factors=candidate["priority_factors"],
                    source_refs=item.source_refs,
                    difficulty=item.difficulty,
                    rank=rank,
                )
            )
        del locked_unit
        self.db.commit()
        return {
            "learning_unit_id": unit.id,
            "flashcard_deck_id": deck.id,
            "version_no": deck.version_no,
            "cards_created": len(result.flashcards),
            **run,
        }

    def _flashcard_deck_for_revision(
        self,
        *,
        workspace_id: str,
        learning_unit_id: str,
        study_note_version_id: str,
        attempt_revision: int,
    ) -> FlashcardDeck | None:
        return self.db.scalar(
            select(FlashcardDeck).where(
                FlashcardDeck.workspace_id == workspace_id,
                FlashcardDeck.learning_unit_id == learning_unit_id,
                FlashcardDeck.study_note_version_id == study_note_version_id,
                FlashcardDeck.attempt_revision == attempt_revision,
            )
        )

    def _flashcard_task_for_revision(
        self,
        unit: LearningUnit,
        note: StudyNoteVersion,
    ) -> tuple[Task | None, int]:
        locked_unit = self.db.scalar(
            select(LearningUnit).where(
                LearningUnit.workspace_id == unit.workspace_id,
                LearningUnit.id == unit.id,
            ).with_for_update()
        )
        if locked_unit is None:
            raise PermanentTaskError("Learning unit not found")
        expected_attempt_revision = locked_unit.attempt_revision
        candidates = self.db.scalars(
            select(Task)
            .where(
                Task.workspace_id == unit.workspace_id,
                Task.task_type == "generate_flashcards",
                Task.resource_type == "learning_unit",
                Task.resource_id == unit.id,
                Task.status.in_(("queued", "running", "succeeded")),
                Task.cancel_requested_at.is_(None),
            )
            .order_by(Task.created_at.desc())
        ).all()
        existing = next(
            (
                task
                for task in candidates
                if (task.payload or {}).get("study_note_version_id") == note.id
                and int((task.payload or {}).get("expected_attempt_revision", -1))
                == expected_attempt_revision
            ),
            None,
        )
        if existing is not None:
            self.db.commit()
        return existing, expected_attempt_revision

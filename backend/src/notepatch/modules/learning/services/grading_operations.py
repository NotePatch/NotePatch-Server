from __future__ import annotations

from sqlalchemy import select

from notepatch.modules.learning.models.homework import GradingResult, Homework, Mistake, Question
from notepatch.modules.learning.models.knowledge import KnowledgeChunk
from notepatch.modules.learning.models.learning import (
    KnowledgePoint,
    LearningUnit,
    KnowledgePointAttempt,
    StudyNoteVersion,
)
from notepatch.modules.learning.schemas.skills import GradingSkillResult, NoteHighlightResult
from notepatch.modules.tasks.models.task import Task
from notepatch.modules.tasks.services.task import TaskService
from notepatch.platform.errors import PermanentTaskError
from notepatch.platform.storage import StorageService
from notepatch.modules.learning.services.flashcard_priority import FlashcardPriorityService
from notepatch.modules.learning.services.html_notes import (
    referenced_knowledge_point_ids,
    sanitize_note_html,
    validate_knowledge_point_references,
)
from notepatch.modules.learning.services.knowledge_points import KnowledgePointService
from notepatch.platform.config import get_settings
from notepatch.platform.database import utcnow


class LearningGradingOperations:
    def grade_homework(self, task: Task, homework: Homework, storage: StorageService) -> dict:
        existing = self._grading_for_task(task.id)
        if existing is not None:
            return {"homework_id": homework.id, "grading_result_id": existing.id, "reused": True}
        unit = self._learning_unit_for_homework(homework)
        document = self._document(homework.document_id, task.workspace_id) if homework.document_id else None
        if document is None:
            raise PermanentTaskError("Homework has no source document")
        questions = self.db.scalars(
            select(Question).where(Question.workspace_id == task.workspace_id, Question.homework_id == homework.id)
        ).all()
        references = self._grading_references(homework)
        canonical_points = self.db.scalars(
            select(KnowledgePoint).where(
                KnowledgePoint.workspace_id == task.workspace_id,
                KnowledgePoint.learning_unit_id == unit.id,
            )
        ).all() if unit else []
        official_basis = bool(homework.rubric_text or references)
        result, run = self._skill().execute(
            task=task,
            skill_name="notepatch_grading",
            input_payload={
                "homework": {
                    "id": homework.id,
                    "title": homework.title,
                    "max_score": homework.max_score,
                    "rubric_text": homework.rubric_text,
                },
                "homework_ocr": self._required_ocr_text(document),
                "questions": [
                    {"sequence_no": q.sequence_no, "prompt": q.prompt, "answer": q.answer} for q in questions
                ],
                "references": references,
                "knowledge_chunks": [self._chunk_payload(chunk) for chunk in self._unit_chunks(unit.id, task.workspace_id)] if unit else [],
                "knowledge_points": [{"id": point.id, "name": point.name} for point in canonical_points],
                "required_grading_mode": "official" if official_basis else "provisional",
            },
            output_filename="grading_report.json",
            schema=GradingSkillResult,
        )
        self._ensure_active(task)
        mode = result.grading_mode if official_basis else "provisional"
        report = result.model_dump(mode="json")
        report["grading_mode"] = mode
        report_key = run["output_key"]
        grading = GradingResult(
            workspace_id=task.workspace_id,
            homework_id=homework.id,
            student_user_id=task.payload.get("student_user_id"),
            score=result.score,
            max_score=result.max_score,
            grading_mode=mode,
            confidence=result.confidence,
            feedback=result.summary,
            report_storage_key=report_key,
            metadata_={"skill": "notepatch_grading", "task_id": task.id, "official_basis": official_basis},
        )
        self.db.add(grading)
        self.db.flush()
        point_by_id = {point.id: point for point in canonical_points}
        point_by_name: dict[str, KnowledgePoint] = {}
        if unit is not None:
            unresolved_names = list(
                dict.fromkeys(
                    [
                        reference.name
                        for question_result in result.per_question
                        for reference in question_result.knowledge_points
                        if not reference.id or reference.id not in point_by_id
                    ]
                    + [item.knowledge_point for item in result.mistakes]
                )
            )
            point_by_name = KnowledgePointService(
                self.db,
                self.embedding_client,
                match_threshold=get_settings().knowledge_point_match_threshold,
            ).resolve_many(
                unit=unit,
                names=unresolved_names,
                source_document_ids=[document.id],
                owner=f"task:{task.id}:grading-knowledge-points",
            )

        def resolved_point(reference) -> KnowledgePoint | None:
            return point_by_id.get(reference.id) or point_by_name.get(reference.name)

        questions_by_sequence = {question.sequence_no: question for question in questions}
        attempts_created = 0
        attempt_keys: set[tuple[str | None, str]] = set()
        for question_result in result.per_question:
            ratio = min(max(question_result.score / question_result.max_score, 0.0), 1.0)
            outcome = "correct" if ratio >= 0.999 else "incorrect" if ratio <= 0.001 else "partial"
            question = questions_by_sequence.get(question_result.sequence_no)
            for reference in question_result.knowledge_points:
                point = resolved_point(reference)
                if point is None or unit is None:
                    continue
                attempt_key = (question.id if question else None, point.id)
                if attempt_key in attempt_keys:
                    continue
                attempt_keys.add(attempt_key)
                self.db.add(
                    KnowledgePointAttempt(
                        workspace_id=task.workspace_id,
                        learning_unit_id=unit.id,
                        knowledge_point_id=point.id,
                        student_user_id=task.payload.get("student_user_id"),
                        homework_id=homework.id,
                        grading_result_id=grading.id,
                        question_id=question.id if question else None,
                        outcome=outcome,
                        score_ratio=ratio,
                        occurred_at=utcnow(),
                        metadata_={"task_id": task.id, "sequence_no": question_result.sequence_no},
                    )
                )
                attempts_created += 1
        if unit is not None and attempts_created:
            unit = self._increment_attempt_revision(unit)
        mistake_chunks: list[KnowledgeChunk] = []
        mistakes: list[Mistake] = []
        for item in result.mistakes:
            point = point_by_name.get(item.knowledge_point)
            mistake = Mistake(
                workspace_id=task.workspace_id,
                grading_result_id=grading.id,
                knowledge_point_id=point.id if point else None,
                student_user_id=task.payload.get("student_user_id"),
                subject=unit.subject if unit else None,
                knowledge_point=item.knowledge_point,
                description=item.description,
                status="open",
                metadata_={
                    "skill": "notepatch_grading",
                    "task_id": task.id,
                    "evidence": item.evidence,
                    "correction": item.correction,
                    "recommendation": item.recommendation,
                    "question_sequence_no": item.question_sequence_no,
                    "learning_unit_id": unit.id if unit else None,
                },
            )
            self.db.add(mistake)
            mistakes.append(mistake)
            mistake_chunks.append(
                KnowledgeChunk(
                    workspace_id=task.workspace_id,
                    document_id=document.id,
                    subject=unit.subject if unit else None,
                    grade_level=unit.grade_level if unit else None,
                    source_type="mistake",
                    content=" ".join(
                        part for part in [item.knowledge_point, item.description, item.correction] if part
                    ),
                    embedding=None,
                    metadata_={
                        "skill": "notepatch_grading",
                        "task_id": task.id,
                        "learning_unit_id": unit.id if unit else None,
                        "title": item.knowledge_point,
                    },
                )
            )
        if mistake_chunks:
            vectors = self.embedding_client.embed(
                [chunk.content for chunk in mistake_chunks],
                owner=f"task:{task.id}:mistakes",
                event_callback=lambda event, data: self._record_task_event(task, event, data),
            )
            self._ensure_active(task)
            for chunk, vector in zip(mistake_chunks, vectors, strict=True):
                chunk.embedding = vector
                self.db.add(chunk)
        self._ensure_active(task)
        homework.status = "graded"
        self.db.commit()
        highlight_status = "not_applicable"
        highlight_tasks = []
        latest_note = None
        if unit is not None:
            latest_note = self.db.scalar(
                select(StudyNoteVersion)
                .where(
                    StudyNoteVersion.workspace_id == task.workspace_id,
                    StudyNoteVersion.learning_unit_id == unit.id,
                )
                .order_by(StudyNoteVersion.version_no.desc())
            )
        if unit is not None and latest_note is not None:
            self._ensure_active(task)
            highlight_tasks = self._create_unique_tasks(
                [
                    (
                        "highlight_study_notes",
                        "learning_unit",
                        unit.id,
                        {
                            "learning_unit_id": unit.id,
                            "mistake_ids": [mistake.id for mistake in mistakes],
                            "source_grading_result_id": grading.id,
                            "expected_note_version_id": latest_note.id,
                        },
                    )
                ],
                force_reprocess=True,
            )
            highlight_status = "queued"
        elif unit is not None and mistakes:
            highlight_status = "skipped_no_study_note"
            self._record_task_event(
                task,
                "note_highlight_skipped",
                {"reason": "no_study_note", "learning_unit_id": unit.id},
            )
        flashcard_task = None
        if unit is not None and latest_note is not None:
            flashcard_task = self.schedule_flashcards(unit, latest_note, reason="homework_graded")
        gap_task = None
        if unit is not None:
            gap_task = TaskService(self.db).create_task(
                workspace_id=task.workspace_id,
                task_type="detect_note_gaps",
                resource_type="learning_unit",
                resource_id=unit.id,
                payload={
                    "learning_unit_id": unit.id,
                    "source_grading_result_id": grading.id,
                    "reason": "homework_graded",
                },
            )
        return {
            "homework_id": homework.id,
            "grading_result_id": grading.id,
            "score": result.score,
            "max_score": result.max_score,
            "grading_mode": mode,
            "confidence": result.confidence,
            "report_key": report_key,
            "mistakes_created": len(mistakes),
            "note_highlight_status": highlight_status,
            "attempts_created": attempts_created,
            "attempt_revision": unit.attempt_revision if unit else None,
            "flashcard_task_id": flashcard_task.id if flashcard_task else None,
            "downstream_tasks": [
                {"id": child.id, "task_type": child.task_type}
                for child in [
                    *highlight_tasks,
                    *([flashcard_task] if flashcard_task else []),
                    *([gap_task] if gap_task else []),
                ]
            ],
        }

    def _increment_attempt_revision(self, unit: LearningUnit) -> LearningUnit:
        locked_unit = self.db.scalar(
            select(LearningUnit)
            .where(
                LearningUnit.workspace_id == unit.workspace_id,
                LearningUnit.id == unit.id,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if locked_unit is None:
            raise PermanentTaskError("Learning unit not found")
        locked_unit.attempt_revision += 1
        return locked_unit

    def highlight_study_notes(self, task: Task, storage: StorageService) -> dict:
        unit = self._learning_unit(task.payload.get("learning_unit_id") or task.resource_id, task.workspace_id)
        note = self.db.scalar(
            select(StudyNoteVersion)
            .where(StudyNoteVersion.workspace_id == task.workspace_id, StudyNoteVersion.learning_unit_id == unit.id)
            .order_by(StudyNoteVersion.version_no.desc())
        )
        if note is None:
            return {"learning_unit_id": unit.id, "skipped": True, "reason": "no_study_note"}
        mistake_ids = task.payload.get("mistake_ids") if isinstance(task.payload.get("mistake_ids"), list) else []
        query = select(Mistake).where(Mistake.workspace_id == task.workspace_id)
        query = query.where(Mistake.id.in_(mistake_ids)) if mistake_ids else query.where(Mistake.status == "open")
        mistakes = self.db.scalars(query).all()
        weighted = FlashcardPriorityService(self.db).calculate(
            workspace_id=task.workspace_id,
            learning_unit_id=unit.id,
            note=note,
        )
        highlighted_points = [item for item in weighted if item["highlight_level"] is not None]
        source_html = self._download_text(storage, storage.bucket, note.html_object_key)
        if highlighted_points:
            result, run = self._skill().execute(
                task=task,
                skill_name="notepatch_note_highlighter",
                input_payload={
                    "learning_unit": self._learning_unit_payload(unit),
                    "study_note_html": source_html,
                    "weighted_knowledge_points": highlighted_points,
                    "mistakes": [
                        {
                            "id": item.id,
                            "knowledge_point_id": item.knowledge_point_id,
                            "knowledge_point": item.knowledge_point,
                            "description": item.description,
                            "metadata": item.metadata_,
                        }
                        for item in mistakes
                    ],
                },
                output_filename="highlighted_note.json",
                schema=NoteHighlightResult,
            )
        else:
            result = NoteHighlightResult.model_validate({"html": source_html, "highlight_map": {"items": []}})
            run = {"output_key": None}
        self._ensure_active(task)
        try:
            highlighted_html = sanitize_note_html(result.html)
            unit_point_ids = {
                item.id
                for item in self.db.scalars(
                    select(KnowledgePoint).where(
                        KnowledgePoint.workspace_id == task.workspace_id,
                        KnowledgePoint.learning_unit_id == unit.id,
                    )
                ).all()
            }
            validate_knowledge_point_references(highlighted_html, unit_point_ids)
        except ValueError as exc:
            raise PermanentTaskError(str(exc)) from exc
        latest = self.db.scalar(
            select(StudyNoteVersion)
            .where(
                StudyNoteVersion.workspace_id == task.workspace_id,
                StudyNoteVersion.learning_unit_id == unit.id,
            )
            .order_by(StudyNoteVersion.version_no.desc())
        )
        if latest is None:
            return {"learning_unit_id": unit.id, "skipped": True, "reason": "no_study_note"}
        if latest.id != note.id:
            if isinstance(run.get("output_key"), str):
                try:
                    storage.delete_object(storage.bucket, run["output_key"])
                except Exception:
                    pass
            replacement = TaskService(self.db).create_task(
                workspace_id=task.workspace_id,
                task_type="highlight_study_notes",
                resource_type="learning_unit",
                resource_id=unit.id,
                payload={
                    **(task.payload or {}),
                    "expected_note_version_id": latest.id,
                    "replaces_task_id": task.id,
                },
            )
            return {
                "learning_unit_id": unit.id,
                "skipped": True,
                "reason": "newer_note_version_available",
                "replacement_task_id": replacement.id,
            }
        highlighted_key = StorageService.learning_unit_note_key(
            task.workspace_id, unit.id, note.id, "highlighted_note", "html"
        )
        map_key = StorageService.learning_unit_note_key(task.workspace_id, unit.id, note.id, "highlight_map", "json")
        self._put_text(storage, highlighted_key, highlighted_html, "text/html; charset=utf-8")
        storage.put_json_artifact(map_key, result.highlight_map.model_dump(mode="json"), bucket=storage.bucket)
        self._ensure_active(task)
        note.highlighted_html_object_key = highlighted_key
        note.highlight_map_object_key = map_key
        note.source_mistake_ids = [mistake.id for mistake in mistakes]
        note.knowledge_point_ids = list(
            dict.fromkeys([*(note.knowledge_point_ids or []), *referenced_knowledge_point_ids(highlighted_html)])
        )
        note.metadata_ = {**(note.metadata_ or {}), "highlight_skill_output_key": run["output_key"]}
        self.db.commit()
        return {
            "learning_unit_id": unit.id,
            "study_note_version_id": note.id,
            "highlighted_key": highlighted_key,
            "highlight_map_key": map_key,
            "mistakes_highlighted": len(mistakes),
        }

    def cleanup_cancelled_grading(self, task: Task, storage: StorageService) -> None:
        gradings = [
            item
            for item in self.db.scalars(
                select(GradingResult).where(GradingResult.workspace_id == task.workspace_id)
            ).all()
            if (item.metadata_ or {}).get("task_id") == task.id
        ]
        grading_ids = {item.id for item in gradings}
        for mistake in self.db.scalars(
            select(Mistake).where(
                Mistake.workspace_id == task.workspace_id,
                Mistake.grading_result_id.in_(grading_ids),
            )
        ).all() if grading_ids else []:
            self.db.delete(mistake)
        for chunk in self.db.scalars(
            select(KnowledgeChunk).where(KnowledgeChunk.workspace_id == task.workspace_id)
        ).all():
            if (chunk.metadata_ or {}).get("task_id") == task.id:
                self.db.delete(chunk)
        task_service = TaskService(self.db)
        for downstream in self.db.scalars(
            select(Task).where(
                Task.workspace_id == task.workspace_id,
                Task.task_type == "highlight_study_notes",
                Task.status.in_(("queued", "running")),
            )
        ).all():
            if (downstream.payload or {}).get("source_grading_result_id") in grading_ids:
                task_service.request_cancel(downstream, "Source grading task was cancelled", commit=False)
        for grading in gradings:
            if grading.report_storage_key:
                try:
                    storage.delete_object(storage.bucket, grading.report_storage_key)
                except Exception:
                    pass
            self.db.delete(grading)
        homework_id = task.payload.get("homework_id") or task.resource_id
        homework = self.db.scalar(
            select(Homework).where(
                Homework.workspace_id == task.workspace_id,
                Homework.id == homework_id,
            )
        )
        if homework is not None:
            homework.status = "draft"
        self.db.commit()
        try:
            storage.delete_prefix(f"workspaces/{task.workspace_id}/sandbox/tasks/{task.id}/")
        except Exception:
            pass

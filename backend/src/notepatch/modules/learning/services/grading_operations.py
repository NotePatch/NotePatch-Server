from __future__ import annotations

from sqlalchemy import select

from notepatch.modules.learning.models.homework import GradingResult, Homework, Mistake, Question
from notepatch.modules.learning.models.knowledge import KnowledgeChunk
from notepatch.modules.learning.models.learning import StudyNoteVersion
from notepatch.modules.learning.schemas.skills import GradingSkillResult, NoteHighlightResult
from notepatch.modules.tasks.models.task import Task
from notepatch.modules.tasks.services.task import TaskService
from notepatch.platform.errors import PermanentTaskError
from notepatch.platform.storage import StorageService


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
        mistake_chunks: list[KnowledgeChunk] = []
        mistakes: list[Mistake] = []
        for item in result.mistakes:
            mistake = Mistake(
                workspace_id=task.workspace_id,
                grading_result_id=grading.id,
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
        if unit is not None and mistakes:
            self._ensure_active(task)
            self._create_unique_tasks(
                [
                    (
                        "highlight_study_notes",
                        "learning_unit",
                        unit.id,
                        {
                            "learning_unit_id": unit.id,
                            "mistake_ids": [mistake.id for mistake in mistakes],
                            "source_grading_result_id": grading.id,
                        },
                    )
                ],
                force_reprocess=True,
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
        }

    def highlight_study_notes(self, task: Task, storage: StorageService) -> dict:
        unit = self._learning_unit(task.payload.get("learning_unit_id") or task.resource_id, task.workspace_id)
        note = self.db.scalar(
            select(StudyNoteVersion)
            .where(StudyNoteVersion.workspace_id == task.workspace_id, StudyNoteVersion.learning_unit_id == unit.id)
            .order_by(StudyNoteVersion.version_no.desc())
        )
        if note is None:
            raise PermanentTaskError("Study note does not exist for highlighting")
        mistake_ids = task.payload.get("mistake_ids") if isinstance(task.payload.get("mistake_ids"), list) else []
        query = select(Mistake).where(Mistake.workspace_id == task.workspace_id)
        query = query.where(Mistake.id.in_(mistake_ids)) if mistake_ids else query.where(Mistake.status == "open")
        mistakes = self.db.scalars(query).all()
        result, run = self._skill().execute(
            task=task,
            skill_name="notepatch_note_highlighter",
            input_payload={
                "learning_unit": self._learning_unit_payload(unit),
                "study_note_markdown": self._download_text(storage, storage.bucket, note.markdown_object_key),
                "mistakes": [
                    {
                        "id": item.id,
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
        self._ensure_active(task)
        highlighted_key = StorageService.learning_unit_note_key(
            task.workspace_id, unit.id, note.id, "highlighted_note", "md"
        )
        map_key = StorageService.learning_unit_note_key(task.workspace_id, unit.id, note.id, "highlight_map", "json")
        self._put_text(storage, highlighted_key, result.markdown, "text/markdown; charset=utf-8")
        storage.put_json_artifact(map_key, result.highlight_map.model_dump(mode="json"), bucket=storage.bucket)
        self._ensure_active(task)
        note.highlighted_object_key = highlighted_key
        note.highlight_map_object_key = map_key
        note.source_mistake_ids = [mistake.id for mistake in mistakes]
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

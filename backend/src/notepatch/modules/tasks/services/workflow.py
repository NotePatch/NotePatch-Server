from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from notepatch.modules.documents.models.document import Document
from notepatch.modules.tasks.models.task import Task, TaskEvent
from notepatch.modules.tasks.models.workflow import WorkflowEvent, WorkflowRun, WorkflowTaskLink
from notepatch.platform.database import utcnow


TASK_STAGE = {
    "scan_document": ("upload_validation", "core"),
    "document_processing_pipeline": ("ocr", "core"),
    "ocr_document": ("ocr", "core"),
    "assign_learning_unit": ("learning_unit_assignment", "core"),
    "build_knowledge_base": ("knowledge_base", "core"),
    "extract_questions": ("question_extraction", "core"),
    "grade_homework": ("grading", "core"),
    "generate_study_notes": ("study_notes", "enrichment"),
    "generate_flashcards": ("flashcards", "enrichment"),
    "highlight_study_notes": ("note_highlight", "enrichment"),
    "detect_note_gaps": ("note_gap_detection", "enrichment"),
    "generate_note_supplement": ("note_supplement", "enrichment"),
    "purge_study_note_history": ("note_history_cleanup", "enrichment"),
    "merge_learning_units": ("learning_unit_merge", "core"),
}

TERMINAL_STAGE_BY_DOCUMENT_KIND = {
    "courseware": "knowledge_base",
    "note": "knowledge_base",
    "other": "knowledge_base",
    "homework": "grading",
    "corrected_homework": "grading",
    "exam": "question_extraction",
    "answer_key": "ocr",
    "rubric": "ocr",
    "chat_attachment": "upload",
}

ENRICHMENT_DOCUMENT_KINDS = {"courseware", "note", "other", "homework", "corrected_homework"}


class WorkflowTracker:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create_for_document(
        self,
        document: Document,
        *,
        user_id: str | None,
        trigger_type: str,
        waiting_upload: bool,
    ) -> WorkflowRun:
        run = WorkflowRun(
            workspace_id=document.workspace_id,
            user_id=user_id,
            document_id=document.id,
            learning_unit_id=(
                (document.metadata_ or {}).get("learning_unit_id")
                if isinstance((document.metadata_ or {}).get("learning_unit_id"), str)
                else None
            ),
            trigger_type=trigger_type,
            status="waiting_upload" if waiting_upload else "queued",
            core_status="not_started" if waiting_upload else "queued",
            enrichment_status=(
                "not_started" if document.document_kind in ENRICHMENT_DOCUMENT_KINDS else "not_applicable"
            ),
            current_stage="upload" if waiting_upload else "queued",
            metadata_={
                "document_kind": document.document_kind,
                "core_terminal_stage": TERMINAL_STAGE_BY_DOCUMENT_KIND.get(document.document_kind, "ocr"),
                "enrichment_expected": document.document_kind in ENRICHMENT_DOCUMENT_KINDS,
            },
        )
        self.db.add(run)
        self.db.flush()
        document.latest_workflow_run_id = run.id
        self.add_event(
            run,
            "workflow_created",
            "Document workflow created",
            stage="upload",
            progress=0,
            data={"trigger_type": trigger_type, "document_id": document.id},
        )
        return run

    def mark_upload_completed(self, run: WorkflowRun, document: Document) -> None:
        run.status = "queued"
        run.core_status = "queued"
        run.current_stage = "scheduling"
        run.started_at = run.started_at or utcnow()
        self.add_event(
            run,
            "upload_completed",
            "Document upload completed",
            stage="upload",
            progress=5,
            data={"document_id": document.id},
        )

    def mark_ready_without_tasks(self, run: WorkflowRun, document: Document) -> None:
        run.status = "succeeded"
        run.core_status = "succeeded"
        run.enrichment_status = "not_applicable"
        run.current_stage = "upload"
        run.progress = 100
        run.finished_at = utcnow()
        run.result = {"document_id": document.id}
        self.add_event(
            run,
            "workflow_succeeded",
            "Document workflow succeeded",
            stage="upload",
            progress=100,
            data={"document_id": document.id},
        )

    def mark_upload_failed(self, document: Document, message: str) -> None:
        run = self.latest_for_document(document.workspace_id, document.id)
        if run is None:
            return
        run.status = "failed"
        run.core_status = "failed"
        run.error_message = message
        run.current_stage = "upload"
        run.finished_at = utcnow()
        self.add_event(
            run,
            "upload_failed",
            "Document upload failed",
            stage="upload",
            level="error",
            data={"document_id": document.id, "error": message},
        )

    def latest_for_document(self, workspace_id: str, document_id: str) -> WorkflowRun | None:
        return self.db.scalar(
            select(WorkflowRun)
            .where(WorkflowRun.workspace_id == workspace_id, WorkflowRun.document_id == document_id)
            .order_by(WorkflowRun.created_at.desc())
        )

    def link_task(
        self,
        task: Task,
        *,
        workflow_runs: Iterable[WorkflowRun] | None = None,
        parent_task: Task | None = None,
    ) -> list[WorkflowTaskLink]:
        runs = list(workflow_runs or [])
        if parent_task is not None:
            runs.extend(self.runs_for_task(parent_task.id))
        if not runs:
            run_ids = task.payload.get("workflow_run_ids") or []
            if isinstance(task.payload.get("workflow_run_id"), str):
                run_ids = [task.payload["workflow_run_id"], *run_ids]
            if task.resource_type == "document" and task.resource_id:
                document_run = self.latest_for_document(task.workspace_id, task.resource_id)
                if document_run is not None:
                    run_ids.append(document_run.id)
            unique_ids = list(dict.fromkeys(item for item in run_ids if isinstance(item, str)))
            if unique_ids:
                runs.extend(
                    self.db.scalars(
                        select(WorkflowRun).where(
                            WorkflowRun.workspace_id == task.workspace_id,
                            WorkflowRun.id.in_(unique_ids),
                        )
                    ).all()
                )
        unique_runs = {run.id: run for run in runs if run.workspace_id == task.workspace_id}
        if not unique_runs:
            return []

        stage, phase = TASK_STAGE.get(task.task_type, (task.task_type, "core"))
        links: list[WorkflowTaskLink] = []
        for run in unique_runs.values():
            link = self.db.scalar(
                select(WorkflowTaskLink).where(
                    WorkflowTaskLink.workflow_run_id == run.id,
                    WorkflowTaskLink.task_id == task.id,
                )
            )
            if link is None:
                link = WorkflowTaskLink(
                    workflow_run_id=run.id,
                    task_id=task.id,
                    stage=stage,
                    phase=phase,
                    required=True,
                )
                self.db.add(link)
                self.db.flush()
                self._mirror_existing_task_events(run, link, task)
                self.add_event(
                    run,
                    "task_linked",
                    f"{task.task_type} linked to workflow",
                    task_id=task.id,
                    stage=stage,
                    progress=task.progress,
                    data={"task_type": task.task_type, "phase": phase, "status": task.status},
                )
            self._supersede_failed_links(run, link, task)
            links.append(link)
            learning_unit_id = task.payload.get("learning_unit_id")
            if isinstance(learning_unit_id, str):
                run.learning_unit_id = learning_unit_id

        payload = dict(task.payload or {})
        payload["workflow_run_ids"] = sorted(unique_runs)
        if len(unique_runs) == 1:
            payload["workflow_run_id"] = next(iter(unique_runs))
        task.payload = payload
        for run in unique_runs.values():
            self.recompute(run)
        return links

    def _supersede_failed_links(
        self,
        run: WorkflowRun,
        current_link: WorkflowTaskLink,
        current_task: Task,
    ) -> None:
        if current_task.resource_type is None or current_task.resource_id is None:
            return
        rows = self.db.execute(
            select(WorkflowTaskLink, Task)
            .join(Task, Task.id == WorkflowTaskLink.task_id)
            .where(
                WorkflowTaskLink.workflow_run_id == run.id,
                WorkflowTaskLink.id != current_link.id,
                WorkflowTaskLink.stage == current_link.stage,
                WorkflowTaskLink.required.is_(True),
                Task.resource_type == current_task.resource_type,
                Task.resource_id == current_task.resource_id,
                Task.status.in_(("failed", "cancelled")),
            )
        ).all()
        for old_link, old_task in rows:
            old_link.required = False
            self.add_event(
                run,
                "task_superseded",
                f"{old_task.task_type} was superseded by a replacement task",
                task_id=current_task.id,
                stage=current_link.stage,
                progress=current_task.progress,
                data={"superseded_task_id": old_task.id, "replacement_task_id": current_task.id},
            )

    def _mirror_existing_task_events(
        self,
        run: WorkflowRun,
        link: WorkflowTaskLink,
        task: Task,
    ) -> None:
        mirrored_ids = set(
            self.db.scalars(
                select(WorkflowEvent.task_event_id).where(
                    WorkflowEvent.workflow_run_id == run.id,
                    WorkflowEvent.task_id == task.id,
                    WorkflowEvent.task_event_id.is_not(None),
                )
            ).all()
        )
        for task_event in self.db.scalars(
            select(TaskEvent)
            .where(TaskEvent.task_id == task.id)
            .order_by(TaskEvent.sequence_no.asc())
        ).all():
            if task_event.id in mirrored_ids:
                continue
            self.add_event(
                run,
                task_event.event_type,
                task_event.message,
                task_id=task.id,
                task_event_id=task_event.id,
                stage=link.stage,
                level=task_event.level,
                progress=task_event.progress,
                data=task_event.data,
            )

    def mirror_task_event(self, task: Task, task_event: TaskEvent) -> None:
        for link, run in self.db.execute(
            select(WorkflowTaskLink, WorkflowRun)
            .join(WorkflowRun, WorkflowRun.id == WorkflowTaskLink.workflow_run_id)
            .where(WorkflowTaskLink.task_id == task.id)
        ).all():
            self.add_event(
                run,
                task_event.event_type,
                task_event.message,
                task_id=task.id,
                task_event_id=task_event.id,
                stage=link.stage,
                level=task_event.level,
                progress=task_event.progress,
                data=task_event.data,
            )
            self.recompute(run)

    def reconcile_downstream(self, task: Task) -> None:
        runs = self.runs_for_task(task.id)
        if not runs:
            return
        child_ids = self._downstream_task_ids(task.result or {})
        for child in self.db.scalars(
            select(Task).where(Task.workspace_id == task.workspace_id, Task.id.in_(child_ids))
        ).all() if child_ids else []:
            self.link_task(child, workflow_runs=runs)

        if task.task_type == "grade_homework" and not any(
            TASK_STAGE.get(child.task_type, ("", ""))[1] == "enrichment"
            for child in self.db.scalars(
                select(Task).where(Task.workspace_id == task.workspace_id, Task.id.in_(child_ids))
            ).all()
        ):
            for run in runs:
                metadata = dict(run.metadata_ or {})
                metadata["enrichment_expected"] = False
                run.metadata_ = metadata

        for run in runs:
            self.recompute(run)

    def runs_for_task(self, task_id: str) -> list[WorkflowRun]:
        return self.db.scalars(
            select(WorkflowRun)
            .join(WorkflowTaskLink, WorkflowTaskLink.workflow_run_id == WorkflowRun.id)
            .where(WorkflowTaskLink.task_id == task_id)
        ).all()

    def recompute(self, run: WorkflowRun) -> None:
        rows = self.db.execute(
            select(WorkflowTaskLink, Task)
            .join(Task, Task.id == WorkflowTaskLink.task_id)
            .where(WorkflowTaskLink.workflow_run_id == run.id)
            .order_by(WorkflowTaskLink.created_at.asc())
        ).all()
        if not rows:
            return

        previous = (run.status, run.core_status, run.enrichment_status, run.current_stage)
        core_rows = [(link, task) for link, task in rows if link.phase == "core"]
        enrichment_rows = [(link, task) for link, task in rows if link.phase == "enrichment"]
        core_status, core_waiting = self._phase_status(core_rows)
        terminal_stage = (run.metadata_ or {}).get("core_terminal_stage")
        terminal_done = any(
            link.stage == terminal_stage and task.status == "succeeded" for link, task in core_rows
        )
        if core_status == "succeeded" and terminal_stage not in {None, "upload"} and not terminal_done:
            core_status = "running"
        run.core_status = core_status

        enrichment_expected = bool((run.metadata_ or {}).get("enrichment_expected"))
        if enrichment_rows:
            enrichment_status, enrichment_waiting = self._phase_status(enrichment_rows)
        elif enrichment_expected and core_status == "succeeded":
            enrichment_status, enrichment_waiting = "waiting", None
        elif enrichment_expected:
            enrichment_status, enrichment_waiting = "not_started", None
        else:
            enrichment_status, enrichment_waiting = "not_applicable", None
        run.enrichment_status = enrichment_status
        run.waiting_until = self._earliest(core_waiting, enrichment_waiting)

        if core_status == "failed":
            run.status = "failed"
        elif core_status == "cancelled":
            run.status = "cancelled"
        elif core_status == "succeeded":
            if enrichment_status == "failed":
                run.status = "partially_succeeded"
            elif enrichment_status in {"queued", "waiting", "not_started"}:
                run.status = "waiting"
            elif enrichment_status == "running":
                run.status = "running"
            else:
                run.status = "succeeded"
        elif core_status == "waiting":
            run.status = "waiting"
        else:
            run.status = core_status

        active = next(
            (
                (link, task)
                for link, task in reversed(rows)
                if task.status in {"running", "queued"}
            ),
            rows[-1],
        )
        run.current_stage = active[0].stage
        run.progress = self._progress(core_rows, enrichment_rows, enrichment_expected)
        run.error_message = next(
            (
                task.error_message
                for link, task in rows
                if link.required and task.status == "failed" and task.error_message
            ),
            None,
        )
        run.result = self._result_summary(rows)
        run.started_at = run.started_at or min((task.started_at for _link, task in rows if task.started_at), default=None)
        if run.status in {"succeeded", "partially_succeeded", "failed", "cancelled"}:
            run.finished_at = utcnow()
        else:
            run.finished_at = None

        if previous != (run.status, run.core_status, run.enrichment_status, run.current_stage):
            self.add_event(
                run,
                "workflow_status_changed",
                "Workflow status changed",
                stage=run.current_stage,
                progress=run.progress,
                data={
                    "status": run.status,
                    "core_status": run.core_status,
                    "enrichment_status": run.enrichment_status,
                    "waiting_until": run.waiting_until.isoformat() if run.waiting_until else None,
                },
            )

    def add_event(
        self,
        run: WorkflowRun,
        event_type: str,
        message: str,
        *,
        task_id: str | None = None,
        task_event_id: str | None = None,
        stage: str | None = None,
        level: str = "info",
        progress: int | None = None,
        data: dict | None = None,
    ) -> WorkflowEvent:
        self.db.flush()
        self.db.execute(select(WorkflowRun.id).where(WorkflowRun.id == run.id).with_for_update())
        sequence = self.db.scalar(
            select(func.coalesce(func.max(WorkflowEvent.sequence_no), 0) + 1).where(
                WorkflowEvent.workflow_run_id == run.id
            )
        )
        event = WorkflowEvent(
            workspace_id=run.workspace_id,
            workflow_run_id=run.id,
            task_id=task_id,
            task_event_id=task_event_id,
            sequence_no=int(sequence or 1),
            stage=stage,
            event_type=event_type,
            level=level,
            message=message,
            progress=progress,
            data=data or {},
        )
        self.db.add(event)
        return event

    @staticmethod
    def _phase_status(rows: list[tuple[WorkflowTaskLink, Task]]) -> tuple[str, datetime | None]:
        required = [(link, task) for link, task in rows if link.required] or rows
        if not required:
            return "not_started", None
        if any(task.status == "failed" for _link, task in required):
            return "failed", None
        if any(task.status == "running" for _link, task in required):
            return "running", None
        queued = [task for _link, task in required if task.status == "queued"]
        if queued:
            waiting = [task.next_attempt_at for task in queued if task.next_attempt_at is not None]
            return ("waiting" if waiting else "queued"), min(waiting) if waiting else None
        if any(task.status == "cancelled" for _link, task in required):
            return "cancelled", None
        if all(task.status == "succeeded" for _link, task in required):
            return "succeeded", None
        return "not_started", None

    @staticmethod
    def _progress(
        core_rows: list[tuple[WorkflowTaskLink, Task]],
        enrichment_rows: list[tuple[WorkflowTaskLink, Task]],
        enrichment_expected: bool,
    ) -> int:
        def average(rows: list[tuple[WorkflowTaskLink, Task]]) -> float:
            required = [(link, task) for link, task in rows if link.required] or rows
            return sum(task.progress for _link, task in required) / len(required) if required else 0.0

        core = average(core_rows)
        if not enrichment_expected:
            return min(100, round(core))
        return min(100, round(core * 0.75 + average(enrichment_rows) * 0.25))

    @staticmethod
    def _result_summary(rows: list[tuple[WorkflowTaskLink, Task]]) -> dict:
        summary: dict = {}
        scalar_keys = (
            "document_id",
            "learning_unit_id",
            "assignment_id",
            "artifact_id",
            "homework_id",
            "grading_result_id",
            "study_note_version_id",
            "flashcard_deck_id",
        )
        for link, task in rows:
            if not link.required or task.status != "succeeded" or not isinstance(task.result, dict):
                continue
            for key in scalar_keys:
                value = task.result.get(key)
                if isinstance(value, str):
                    summary[key] = value
            ocr_artifacts = task.result.get("ocr_artifacts")
            if isinstance(ocr_artifacts, dict):
                summary["ocr_artifact_ids"] = {
                    key: value
                    for key, value in ocr_artifacts.items()
                    if isinstance(key, str) and isinstance(value, str)
                }
            chunk_ids = task.result.get("chunk_ids")
            if isinstance(chunk_ids, list):
                summary["knowledge_chunk_ids"] = [value for value in chunk_ids if isinstance(value, str)]
        return summary

    @staticmethod
    def _earliest(*values: datetime | None) -> datetime | None:
        present = [value for value in values if value is not None]
        return min(present) if present else None

    @classmethod
    def _downstream_task_ids(cls, result: dict) -> list[str]:
        values: list[str] = []
        for key in ("downstream_task_id", "flashcard_task_id", "replacement_task_id"):
            value = result.get(key)
            if isinstance(value, str):
                values.append(value)
        downstream = result.get("downstream_tasks")
        if isinstance(downstream, list):
            for item in downstream:
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    values.append(item["id"])
        return list(dict.fromkeys(values))

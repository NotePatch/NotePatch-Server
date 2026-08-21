#!/usr/bin/env python3
from __future__ import annotations

import argparse

from sqlalchemy import select

from notepatch.modules.documents.models.document import Document
from notepatch.modules.learning.models.learning import LearningUnit, LearningUnitDocument
from notepatch.modules.tasks.models.task import Task
from notepatch.modules.tasks.services.task import TaskService
from notepatch.platform.database import SessionLocal


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile legacy automatic note tasks with the faithful-note workflow."
    )
    parser.add_argument("--workspace-id")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    with SessionLocal() as db:
        unit_query = select(LearningUnit).where(LearningUnit.merged_into_id.is_(None))
        if args.workspace_id:
            unit_query = unit_query.where(LearningUnit.workspace_id == args.workspace_id)
        units = db.scalars(unit_query).all()
        cancelled: list[str] = []
        gap_units: list[str] = []
        for unit in units:
            has_note = db.scalar(
                select(Document.id)
                .join(LearningUnitDocument, LearningUnitDocument.document_id == Document.id)
                .where(
                    LearningUnitDocument.workspace_id == unit.workspace_id,
                    LearningUnitDocument.learning_unit_id == unit.id,
                    Document.workspace_id == unit.workspace_id,
                    Document.document_kind == "note",
                    Document.status != "deleted",
                )
            )
            pending = db.scalars(
                select(Task).where(
                    Task.workspace_id == unit.workspace_id,
                    Task.task_type == "generate_study_notes",
                    Task.resource_type == "learning_unit",
                    Task.resource_id == unit.id,
                    Task.status == "queued",
                )
            ).all()
            if has_note is None:
                cancelled.extend(task.id for task in pending)
                gap_units.append(unit.id)
                if args.apply:
                    for task in pending:
                        TaskService(db).request_cancel(task, reason="faithful_note_workflow_reconcile")
                    active = TaskService(db).find_active_task(
                        workspace_id=unit.workspace_id,
                        task_type="detect_note_gaps",
                        resource_type="learning_unit",
                        resource_id=unit.id,
                    )
                    if active is None:
                        TaskService(db).create_task(
                            workspace_id=unit.workspace_id,
                            task_type="detect_note_gaps",
                            resource_type="learning_unit",
                            resource_id=unit.id,
                            payload={
                                "learning_unit_id": unit.id,
                                "reason": "faithful_note_workflow_reconcile",
                            },
                        )
        if args.apply:
            db.commit()
        print(
            {
                "mode": "apply" if args.apply else "dry-run",
                "units_without_note_sources": len(gap_units),
                "queued_note_tasks_to_cancel": len(cancelled),
                "task_ids": cancelled,
            }
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

from sqlalchemy import select

from notepatch.modules.learning.models.homework import Homework
from notepatch.modules.learning.services.workflow import LearningWorkflowService
from notepatch.modules.tasks.models.task import Task
from notepatch.modules.tasks.services.task import TaskService
from notepatch.platform.errors import PermanentTaskError
from notepatch.platform.storage import StorageService


def run_learning_task(
    tasks: TaskService,
    task: Task,
    learning: LearningWorkflowService,
    storage: StorageService,
) -> None:
    handlers = {
        "extract_questions": ("question_extraction", lambda: learning.extract_questions(task, storage)),
        "build_knowledge_base": ("knowledge_build", lambda: learning.build_knowledge_base(task, storage)),
        "generate_study_notes": ("study_notes", lambda: learning.generate_study_notes(task, storage)),
        "generate_flashcards": ("flashcards", lambda: learning.generate_flashcards(task, storage)),
        "highlight_study_notes": ("note_highlight", lambda: learning.highlight_study_notes(task, storage)),
    }
    if task.task_type == "grade_homework":
        homework_id = task.payload.get("homework_id") or task.resource_id
        homework = tasks.db.scalar(
            select(Homework).where(
                Homework.workspace_id == task.workspace_id,
                Homework.id == homework_id,
            )
        )
        if homework is None:
            raise PermanentTaskError("Homework not found")
        event_type = "grading"
        callback = lambda: learning.grade_homework(task, homework, storage)
    else:
        selected = handlers.get(task.task_type)
        if selected is None:
            raise PermanentTaskError(f"No learning handler for task type {task.task_type}")
        event_type, callback = selected

    tasks.ensure_active(task)
    tasks.add_event(task, f"{event_type}_started", f"{event_type.replace('_', ' ').title()} task started", progress=35)
    tasks.db.commit()
    result = callback()
    tasks.ensure_active(task)
    tasks.mark_succeeded(task, result)

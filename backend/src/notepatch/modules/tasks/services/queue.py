from __future__ import annotations

from notepatch.platform.config import Settings


OPENCLAW_BACKED_TASK_TYPES = {
    "generate_image_remark",
    "openclaw_agent_run",
    "extract_questions",
    "build_knowledge_base",
    "generate_study_notes",
    "generate_flashcards",
    "grade_homework",
    "highlight_study_notes",
    "generate_note_supplement",
}

OPENCLAW_LEARNING_TASK_TYPES = OPENCLAW_BACKED_TASK_TYPES - {"openclaw_agent_run"}


def parse_queue_names(value: str | None, *, default: str) -> list[str]:
    names: list[str] = []
    for item in (value or default).split(","):
        name = item.strip()
        if name and name not in names:
            names.append(name)
    return names or [default]


def queue_name_for_task_type(settings: Settings, task_type: str) -> str:
    if task_type in {"ocr_document", "document_processing_pipeline"}:
        return settings.ocr_queue_name
    if task_type == "openclaw_agent_run":
        return settings.chat_queue_name
    if task_type == "assign_learning_unit":
        return settings.ai_queue_name
    if task_type in OPENCLAW_LEARNING_TASK_TYPES:
        return settings.ai_queue_name
    return settings.default_queue_name


def redis_key_for_queue(settings: Settings, queue_name: str) -> str:
    normalized = (queue_name or settings.default_queue_name).strip()
    if normalized == settings.default_queue_name:
        return settings.redis_task_queue
    return f"{settings.redis_task_queue}:{normalized}"


def redis_keys_for_queue_names(settings: Settings, queue_names: list[str]) -> list[str]:
    keys: list[str] = []
    for queue_name in queue_names:
        key = redis_key_for_queue(settings, queue_name)
        if key not in keys:
            keys.append(key)
    return keys


def retry_key_for_queue(settings: Settings, queue_name: str) -> str:
    return f"{settings.redis_task_retry_queue}:{queue_name.strip()}"


def promote_due_retries(client, settings: Settings, queue_names: list[str], *, now: float | None = None) -> int:
    timestamp = now if now is not None else __import__("time").time()
    promoted = 0
    for queue_name in queue_names:
        retry_key = retry_key_for_queue(settings, queue_name)
        queue_key = redis_key_for_queue(settings, queue_name)
        for task_id in client.zrangebyscore(retry_key, "-inf", timestamp):
            if client.zrem(retry_key, task_id):
                client.rpush(queue_key, task_id)
                promoted += 1
    return promoted

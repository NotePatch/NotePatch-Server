from __future__ import annotations

from mimetypes import guess_type
from pathlib import Path
import time
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from notepatch.modules.ai.services.chat import ChatService
from notepatch.modules.ai.services.gateway import OpenClawGatewayRunner, OpenClawRunner
from notepatch.modules.ai.services.model_catalog import normalize_ai_model_id
from notepatch.modules.ai.services.model_selection import AiModelSelectionService
from notepatch.modules.ai.services.runtime import OpenClawUserRuntimeService
from notepatch.modules.documents.services.task_support import _progress
from notepatch.modules.learning.models.knowledge import KnowledgeChunk
from notepatch.modules.learning.services.embedding import EmbeddingClient
from notepatch.modules.learning.services.knowledge import KnowledgeService
from notepatch.modules.tasks.models.task import Task
from notepatch.modules.tasks.services.task import TaskService
from notepatch.modules.tasks.services.cancellation import is_task_cancellation_signalled
from notepatch.platform.config import get_settings
from notepatch.platform.errors import PermanentTaskError
from notepatch.platform.errors import TaskCancelledError
from notepatch.platform.storage import StorageService


@dataclass
class ChatStreamEventWriter:
    """Batch gateway deltas into durable task events and an assistant draft."""

    db: Session
    tasks: TaskService
    task: Task
    chat_service: ChatService
    buffers: dict[str, str] = field(default_factory=lambda: {"answer": "", "reasoning": ""})
    persisted_characters: dict[str, int] = field(default_factory=lambda: {"answer": 0, "reasoning": 0})
    chunk_indexes: dict[str, int] = field(default_factory=lambda: {"answer": 0, "reasoning": 0})
    truncated: set[str] = field(default_factory=set)
    last_flush_at: float = field(default_factory=time.monotonic)

    def write(self, stream: str, delta: str) -> None:
        if stream not in self.buffers or not delta:
            return
        self.tasks.ensure_active(self.task)
        limit = (
            get_settings().ai_chat_stream_max_answer_chars
            if stream == "answer"
            else get_settings().ai_chat_stream_max_reasoning_chars
        )
        remaining = max(0, limit - self.persisted_characters[stream] - len(self.buffers[stream]))
        accepted = delta[:remaining]
        if accepted:
            self.buffers[stream] += accepted
        if len(accepted) < len(delta) and stream not in self.truncated:
            self.truncated.add(stream)
            self.tasks.add_event(
                self.task,
                "chat_stream_truncated",
                "Chat stream exceeded its persisted event limit",
                level="warning",
                data={"stream": stream, "limit": limit},
            )
        if (
            len(self.buffers[stream]) >= get_settings().ai_chat_stream_chunk_max_chars
            or (time.monotonic() - self.last_flush_at) * 1000 >= get_settings().ai_chat_stream_flush_milliseconds
        ):
            self.flush()

    def flush(self) -> None:
        self.tasks.ensure_active(self.task)
        wrote = False
        for stream, buffered in tuple(self.buffers.items()):
            if not buffered:
                continue
            self.chunk_indexes[stream] += 1
            self.persisted_characters[stream] += len(buffered)
            self.tasks.add_event(
                self.task,
                "chat_answer_delta" if stream == "answer" else "chat_reasoning_delta",
                "Chat answer delta received" if stream == "answer" else "Chat reasoning summary delta received",
                progress=75,
                data={
                    "stream": stream,
                    "delta": buffered,
                    "chunk_index": self.chunk_indexes[stream],
                    "attempt": self.task.attempt,
                    "characters": self.persisted_characters[stream],
                },
            )
            if stream == "answer":
                self.chat_service.append_assistant_stream_delta(self.task, buffered)
            self.buffers[stream] = ""
            wrote = True
        if wrote:
            self.db.commit()
            self.last_flush_at = time.monotonic()

    def summary(self) -> dict[str, int | bool]:
        return {
            "answer_characters": self.persisted_characters["answer"],
            "reasoning_characters": self.persisted_characters["reasoning"],
            "answer_truncated": "answer" in self.truncated,
            "reasoning_truncated": "reasoning" in self.truncated,
        }


def process_openclaw_chat(
    db: Session,
    tasks: TaskService,
    task: Task,
    storage: StorageService,
    runner: OpenClawRunner,
    embedding_client: EmbeddingClient,
) -> None:
    model_selection = AiModelSelectionService(db)
    provider_model, model_snapshotted = model_selection.resolve_for_task(task)
    ChatService(db).set_assistant_model(task, provider_model)
    if model_snapshotted:
        tasks.add_event(
            task,
            "ai_model_selected",
            "AI model selected for task",
            data={
                "provider_model": provider_model,
                "gateway_model": get_settings().openclaw_gateway_model,
            },
        )
    db.commit()
    if isinstance(runner, OpenClawGatewayRunner):
        model_selection.ensure_credentials(provider_model)
    settings = get_settings()
    try:
        title_model = normalize_ai_model_id(settings.ai_chat_title_model)
    except ValueError:
        title_model = None
    chat_service = ChatService(db)
    attachment_document_ids = chat_service.attachment_document_ids_for_task(task)
    # An empty attachment set means an ordinary workspace chat, not an
    # instruction to query `id IN ()`. Keep explicit attachments narrowly
    # scoped, while allowing ordinary chat to see the user's ready documents.
    mirror_document_ids = attachment_document_ids or None
    runtime = OpenClawUserRuntimeService().sync_workspace_documents(
        db=db,
        storage=storage,
        workspace_id=task.workspace_id,
        task_id=task.id,
        model_ids=tuple(dict.fromkeys(filter(None, (provider_model, title_model)))),
        document_ids=mirror_document_ids,
    )
    task.payload = {
        **(task.payload or {}),
        "mirrored_document_ids": runtime.get("mirrored_document_ids", []),
    }
    tasks.ensure_active(task)
    tasks.add_event(
        task,
        "openclaw_prepare",
        "OpenClaw user workspace prepared",
        progress=15,
        data={
            "gateway_url": runtime["gateway_url"],
            "gateway_container": runtime["container_name"],
            "model": get_settings().openclaw_gateway_model,
            "agent_model": provider_model,
            "provider_model": provider_model,
            "documents_synced": runtime["documents_synced"],
            "files_synced": runtime["files_synced"],
            "documents_skipped": runtime["documents_skipped"],
            "artifacts_skipped": runtime["artifacts_skipped"],
            "skipped_documents": runtime["skipped_documents"],
            "skipped_artifacts": runtime["skipped_artifacts"],
            "mirror_scope": "attachments" if mirror_document_ids is not None else "workspace",
        },
    )
    db.commit()
    prompt = task.payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise PermanentTaskError("Chat prompt is required")
    input_payload = dict(task.payload.get("input") or {})
    citations = []
    has_vectors = db.scalar(
        select(KnowledgeChunk.id)
        .where(KnowledgeChunk.workspace_id == task.workspace_id, KnowledgeChunk.embedding.is_not(None))
        .limit(1)
    )
    should_retrieve_knowledge = _should_retrieve_knowledge(input_payload)
    if has_vectors is not None and should_retrieve_knowledge:
        tasks.ensure_active(task)
        citations = KnowledgeService(db, embedding_client).search(
            workspace_id=task.workspace_id,
            query=prompt,
            learning_unit_id=input_payload.get("learning_unit_id"),
            subject=input_payload.get("subject"),
            limit=get_settings().knowledge_search_limit,
            owner=f"task:{task.id}:chat-rag",
        )
        tasks.ensure_active(task)
        tasks.add_event(
            task,
            "knowledge_retrieved",
            "Relevant knowledge chunks retrieved",
            progress=20,
            data={"matches": len(citations), "chunk_ids": [item["id"] for item in citations]},
        )
        db.commit()
    elif not should_retrieve_knowledge:
        tasks.add_event(
            task,
            "knowledge_retrieval_skipped",
            "Knowledge retrieval skipped for attachment-focused chat",
            progress=20,
            data={"reason": "attachment_focused_query"},
        )
        db.commit()
    document_contexts = runtime.get("document_contexts") or {}
    if "attachments" in input_payload:
        input_payload["attachments"] = chat_service.resolve_attachments(
            input_payload.get("attachments"),
            document_contexts,
        )
    input_payload["knowledge_context"] = [
        {
            "chunk_id": item["id"],
            "document_id": item["document_id"],
            "content": item["content"],
            "score": item["score"],
            "metadata": item["metadata"],
        }
        for item in citations
    ]
    payload = dict(task.payload)
    payload["input"] = input_payload
    payload["conversation_messages"] = chat_service.history_for_task(
        task,
        document_contexts=document_contexts,
    )
    payload["_openclaw"] = {
        "gateway_url": runtime["gateway_url"],
        "gateway_token": runtime["gateway_token"],
        "gateway_container": runtime["container_name"],
        "user_workspace_dir": runtime["workspace_dir"],
        "documents_index_path": runtime["documents_index_path"],
        "documents_root_path": runtime["documents_root_path"],
        "task_output_path": runtime["task_output_path"],
        "host_task_input_dir": runtime["host_task_input_dir"],
        "host_task_output_dir": runtime["host_task_output_dir"],
        "session_key": f"notepatch:{task.workspace_id}:chat:{task.payload.get('conversation_id') or task.id}",
    }
    tasks.ensure_active(task)
    _progress(db, tasks, task, "openclaw_run", "OpenClaw chat started", 70)
    chat_stream = ChatStreamEventWriter(db=db, tasks=tasks, task=task, chat_service=chat_service)
    tasks.add_event(
        task,
        "chat_stream_started",
        "Chat response stream started",
        progress=70,
        data={"attempt": task.attempt, "thinking_enabled": _thinking_enabled(payload)},
    )
    db.commit()
    result = runner.run_chat_task(
        task.workspace_id,
        task.id,
        payload,
        on_stream_event=chat_stream.write,
        is_cancel_requested=lambda: _is_chat_cancelled(tasks, task),
    )
    chat_stream.flush()
    tasks.ensure_active(task)
    answer = result.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        raise PermanentTaskError("OpenClaw gateway returned no answer")
    if not chat_stream.persisted_characters["answer"]:
        chat_stream.write("answer", answer)
        chat_stream.flush()
    stream_summary = chat_stream.summary()
    if _thinking_enabled(payload) and not stream_summary["reasoning_characters"]:
        tasks.add_event(
            task,
            "chat_reasoning_unavailable",
            "The selected model did not provide a reasoning summary",
            level="warning",
            data={"attempt": task.attempt},
        )
    tasks.add_event(
        task,
        "chat_stream_completed",
        "Chat response stream completed",
        progress=90,
        data={"attempt": task.attempt, **stream_summary},
    )
    db.commit()
    output_keys = _upload_openclaw_outputs(
        task,
        storage,
        runner.collect_output(task.workspace_id, task.id),
        ensure_active=lambda: tasks.ensure_active(task),
    )
    output_key = next(
        (key for key in output_keys if key.endswith("/result.json")),
        output_keys[0] if output_keys else None,
    )
    runner.cleanup(task.workspace_id, task.id)
    result_citations = [
        {
            "chunk_id": item["id"],
            "document_id": item["document_id"],
            "score": item["score"],
            "metadata": item["metadata"],
        }
        for item in citations
    ]
    ChatService(db).mark_assistant_succeeded(
        task,
        answer.strip(),
        citations=result_citations,
        model_id=provider_model,
    )
    # Make the answer visible while the non-critical title request runs, but keep
    # the task active so clients receive a stable title before the terminal event.
    db.commit()
    _generate_conversation_title(
        db=db,
        tasks=tasks,
        task=task,
        runner=runner,
        runtime=payload["_openclaw"],
    )
    tasks.ensure_active(task)
    tasks.mark_succeeded(
        task,
        {
            "answer": answer.strip(),
            "runner": "gateway",
            "gateway_model": get_settings().openclaw_gateway_model,
            "provider_model": provider_model,
            "gateway_container": runtime["container_name"],
            "output_key": output_key,
            "output_keys": output_keys,
            "citations": result_citations,
            "stream": stream_summary,
        },
    )


def _thinking_enabled(payload: dict) -> bool:
    options = payload.get("options") if isinstance(payload.get("options"), dict) else {}
    thinking = options.get("thinking") if isinstance(options.get("thinking"), dict) else {}
    return thinking.get("enabled") is True


def _is_chat_cancelled(tasks: TaskService, task: Task) -> bool:
    if is_task_cancellation_signalled(task.id):
        return True
    try:
        tasks.ensure_active(task)
    except TaskCancelledError:
        return True
    return False


def _generate_conversation_title(
    *,
    db: Session,
    tasks: TaskService,
    task: Task,
    runner: OpenClawRunner,
    runtime: dict,
) -> None:
    settings = get_settings()
    if not settings.ai_chat_auto_title_enabled:
        return
    chat_service = ChatService(db)
    title_context = chat_service.title_context_for_task(
        task,
        limit=settings.ai_chat_title_message_limit,
    )
    if title_context is None:
        return
    conversation, messages = title_context
    client_locale = task.payload.get("client_locale")
    if not isinstance(client_locale, str) or not client_locale.strip():
        client_locale = settings.ai_chat_title_fallback_locale
    try:
        tasks.ensure_active(task)
        title_model = normalize_ai_model_id(settings.ai_chat_title_model)
        if isinstance(runner, OpenClawGatewayRunner):
            AiModelSelectionService(db).ensure_credentials(title_model)
        generated_title = runner.generate_conversation_title(
            task.workspace_id,
            conversation.id,
            messages,
            runtime=runtime,
            provider_model=title_model,
            client_locale=client_locale,
            max_length=settings.ai_chat_title_max_length,
            timeout_seconds=settings.ai_chat_title_timeout_seconds,
        )
        # A user may have stopped the chat while the inexpensive title request
        # was in flight. Discard that result instead of mutating the deleted/
        # cancelled turn's conversation.
        tasks.ensure_active(task)
        if not generated_title:
            return
        updated = chat_service.apply_generated_title(
            conversation_id=conversation.id,
            generated_title=generated_title,
            max_length=settings.ai_chat_title_max_length,
        )
        if updated is None:
            db.rollback()
            return
        tasks.add_event(
            task,
            "chat_title_generated",
            "Conversation title generated",
            data={
                "conversation_id": updated.id,
                "title_source": updated.title_source,
                "title_model": title_model,
                "client_locale": client_locale,
            },
        )
        db.commit()
    except TaskCancelledError:
        db.rollback()
        return
    except Exception as exc:
        db.rollback()
        persisted_task = db.get(Task, task.id)
        if persisted_task is None:
            return
        tasks.add_event(
            persisted_task,
            "chat_title_generation_failed",
            "Conversation title generation failed; the prompt title was retained",
            level="warning",
            data={
                "error": str(exc)[:500],
                "title_model": settings.ai_chat_title_model,
                "client_locale": client_locale,
            },
        )
        db.commit()


def _should_retrieve_knowledge(input_payload: dict) -> bool:
    if input_payload.get("use_knowledge_base") is False:
        return False
    attachments = input_payload.get("attachments")
    has_attachments = isinstance(attachments, list) and bool(attachments)
    if not has_attachments:
        return True
    return (
        input_payload.get("use_knowledge_base") is True
        or bool(input_payload.get("learning_unit_id"))
        or bool(input_payload.get("subject"))
    )


def _upload_openclaw_outputs(
    task: Task,
    storage: StorageService,
    collected: dict,
    *,
    ensure_active=lambda: None,
) -> list[str]:
    output_dir = Path(collected["output_dir"]) if collected.get("output_dir") else None
    if output_dir is None or not output_dir.exists():
        return []
    keys = []
    for path in sorted(output_dir.rglob("*")):
        if not path.is_file():
            continue
        ensure_active()
        key = storage.sandbox_output_key(task.workspace_id, task.id, path.relative_to(output_dir).as_posix())
        storage.put_file(
            storage.bucket,
            key,
            path,
            content_type=guess_type(path.name)[0] or "application/octet-stream",
            metadata={"task_id": task.id},
        )
        ensure_active()
        keys.append(key)
    return keys

from __future__ import annotations

from mimetypes import guess_type
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from notepatch.modules.ai.services.chat import ChatService
from notepatch.modules.ai.services.gateway import OpenClawGatewayRunner, OpenClawRunner
from notepatch.modules.ai.services.model_selection import AiModelSelectionService
from notepatch.modules.ai.services.runtime import OpenClawUserRuntimeService
from notepatch.modules.documents.services.task_support import _progress
from notepatch.modules.learning.models.knowledge import KnowledgeChunk
from notepatch.modules.learning.services.embedding import EmbeddingClient
from notepatch.modules.learning.services.knowledge import KnowledgeService
from notepatch.modules.tasks.models.task import Task
from notepatch.modules.tasks.services.task import TaskService
from notepatch.platform.config import get_settings
from notepatch.platform.errors import PermanentTaskError
from notepatch.platform.storage import StorageService


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
    runtime = OpenClawUserRuntimeService().sync_workspace_documents(
        db=db,
        storage=storage,
        workspace_id=task.workspace_id,
        task_id=task.id,
        model_ids=(provider_model,),
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
    chat_service = ChatService(db)
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
    result = runner.run_task(task.workspace_id, task.id, payload)
    tasks.ensure_active(task)
    answer = result.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        raise PermanentTaskError("OpenClaw gateway returned no answer")
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
        },
    )


def _should_retrieve_knowledge(input_payload: dict) -> bool:
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

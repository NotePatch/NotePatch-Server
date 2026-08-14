from __future__ import annotations

import re

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from notepatch.platform.config import get_settings
from notepatch.platform.database import utcnow
from notepatch.modules.ai.models.chat import ChatConversation, ChatMessage
from notepatch.modules.documents.models.document import Document
from notepatch.modules.tasks.models.task import Task
from notepatch.modules.identity.models.user import User
from notepatch.modules.tasks.services.task import TaskService


class ChatConversationNotFoundError(LookupError):
    pass


class ChatService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.settings = get_settings()

    def create_chat_task(
        self,
        *,
        workspace_id: str,
        user: User,
        prompt: str,
        input_payload: dict,
        options: dict,
        conversation_id: str | None,
        task_service: TaskService,
    ) -> Task:
        conversation = (
            self.get_active_conversation(workspace_id=workspace_id, user_id=user.id, conversation_id=conversation_id)
            if conversation_id
            else self._create_conversation(workspace_id=workspace_id, user_id=user.id, prompt=prompt)
        )
        normalized_input = dict(input_payload)
        attachments = self._normalize_attachments(
            workspace_id=workspace_id,
            raw_attachments=normalized_input.get("attachments"),
        )
        if "attachments" in normalized_input:
            normalized_input["attachments"] = attachments
        now = utcnow()
        user_message = ChatMessage(
            workspace_id=workspace_id,
            conversation_id=conversation.id,
            user_id=user.id,
            role="user",
            content=prompt.strip(),
            status="succeeded",
            attachments=attachments,
        )
        assistant_message = ChatMessage(
            workspace_id=workspace_id,
            conversation_id=conversation.id,
            user_id=user.id,
            role="assistant",
            content="",
            status="queued",
        )
        conversation.last_message_at = now
        self.db.add_all((user_message, assistant_message))
        self.db.flush()

        task = task_service.create_task(
            workspace_id=workspace_id,
            task_type="openclaw_agent_run",
            resource_type="chat_conversation",
            resource_id=conversation.id,
            payload={
                "prompt": prompt.strip(),
                "input": normalized_input,
                "options": options,
                "conversation_id": conversation.id,
                "user_message_id": user_message.id,
                "assistant_message_id": assistant_message.id,
                "ai_history_enabled": bool(user.ai_history_enabled),
            },
            enqueue=False,
        )
        assistant_message.task_id = task.id
        self.db.commit()
        self.db.refresh(task)
        if not task_service.enqueue_task(task.id):
            self.mark_assistant_failed(task, "Task queue is unavailable")
            self.db.commit()
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Task queue is unavailable")
        return task

    def get_active_conversation(self, *, workspace_id: str, user_id: str, conversation_id: str) -> ChatConversation:
        conversation = self.db.scalar(
            select(ChatConversation).where(
                ChatConversation.id == conversation_id,
                ChatConversation.workspace_id == workspace_id,
                ChatConversation.user_id == user_id,
                ChatConversation.deleted_at.is_(None),
            )
        )
        if conversation is None:
            raise ChatConversationNotFoundError("Conversation not found")
        return conversation

    def list_conversations(self, *, workspace_id: str, user_id: str, page: int, page_size: int) -> tuple[list[ChatConversation], int]:
        query = select(ChatConversation).where(
            ChatConversation.workspace_id == workspace_id,
            ChatConversation.user_id == user_id,
            ChatConversation.deleted_at.is_(None),
        )
        total = int(self.db.scalar(select(func.count()).select_from(query.subquery())) or 0)
        items = self.db.scalars(
            query.order_by(ChatConversation.last_message_at.desc(), ChatConversation.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return items, total

    def list_messages(
        self,
        *,
        conversation: ChatConversation,
        user_id: str,
        page: int,
        page_size: int,
    ) -> tuple[list[ChatMessage], int]:
        query = select(ChatMessage).where(
            ChatMessage.workspace_id == conversation.workspace_id,
            ChatMessage.conversation_id == conversation.id,
            ChatMessage.user_id == user_id,
        )
        total = int(self.db.scalar(select(func.count()).select_from(query.subquery())) or 0)
        items = self.db.scalars(
            query.order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return items, total

    def rename_conversation(self, conversation: ChatConversation, title: str) -> ChatConversation:
        conversation.title = self._title_from_prompt(title)
        self.db.commit()
        self.db.refresh(conversation)
        return conversation

    def delete_conversation(self, conversation: ChatConversation) -> None:
        conversation.deleted_at = utcnow()
        TaskService(self.db).cancel_active_tasks(
            workspace_id=conversation.workspace_id,
            resource_type="chat_conversation",
            resource_id=conversation.id,
            task_types=("openclaw_agent_run",),
            reason="Conversation was deleted",
            commit=False,
        )
        self.db.commit()

    def history_for_task(
        self,
        task: Task,
        *,
        document_contexts: dict[str, dict] | None = None,
    ) -> list[dict[str, str]]:
        conversation_id = task.payload.get("conversation_id") if isinstance(task.payload, dict) else None
        if not isinstance(conversation_id, str):
            return []
        conversation = self.db.scalar(
            select(ChatConversation).where(
                ChatConversation.id == conversation_id,
                ChatConversation.workspace_id == task.workspace_id,
                ChatConversation.deleted_at.is_(None),
            )
        )
        if conversation is None:
            return []
        history_enabled = task.payload.get("ai_history_enabled")
        if isinstance(history_enabled, bool):
            if not history_enabled:
                return []
        else:
            user = self.db.get(User, conversation.user_id)
            if user is None or not user.ai_history_enabled:
                return []
        messages = self.db.scalars(
            select(ChatMessage)
            .where(
                ChatMessage.workspace_id == task.workspace_id,
                ChatMessage.conversation_id == conversation.id,
                ChatMessage.user_id == conversation.user_id,
                ChatMessage.status == "succeeded",
                ChatMessage.role.in_(("user", "assistant")),
            )
            .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
            .limit(self.settings.ai_chat_history_message_limit)
        ).all()
        current_user_message_id = task.payload.get("user_message_id")
        history: list[dict[str, str]] = []
        for message in reversed(messages):
            if not message.content.strip():
                continue
            content = message.content
            if message.role == "user" and message.id != current_user_message_id and message.attachments:
                content = self._content_with_attachments(
                    content,
                    self.resolve_attachments(message.attachments, document_contexts or {}),
                )
            history.append({"role": message.role, "content": content})
        return history

    @staticmethod
    def resolve_attachments(attachments: object, document_contexts: dict[str, dict]) -> list[dict]:
        if not isinstance(attachments, list):
            return []
        resolved: list[dict] = []
        for item in attachments:
            if not isinstance(item, dict):
                continue
            document_id = item.get("document_id")
            if not isinstance(document_id, str):
                continue
            context = document_contexts.get(document_id)
            merged = dict(item)
            if context is None:
                merged["availability"] = "unavailable"
            else:
                merged.update(context)
                merged["availability"] = "available"
            resolved.append(merged)
        return resolved

    def mark_assistant_running(self, task: Task) -> None:
        message = self._assistant_message_for_task(task)
        if message is not None and message.status == "queued":
            message.status = "running"
            self.db.commit()

    def mark_assistant_succeeded(
        self,
        task: Task,
        answer: str,
        *,
        citations: list[dict] | None = None,
        model_id: str | None = None,
    ) -> None:
        message = self._assistant_message_for_task(task)
        if message is None:
            return
        message.status = "succeeded"
        message.content = answer
        message.error_message = None
        message.citations = [self._safe_citation(item) for item in (citations or [])]
        message.source_status = "available"
        message.model_id = model_id
        self._touch_conversation(message.conversation_id)

    def set_assistant_model(self, task: Task, model_id: str) -> None:
        message = self._assistant_message_for_task(task)
        if message is not None:
            message.model_id = model_id

    def mark_assistant_failed(self, task: Task, error: str) -> None:
        message = self._assistant_message_for_task(task)
        if message is None:
            return
        message.status = "failed"
        message.error_message = error[:2000]
        self._touch_conversation(message.conversation_id)

    def mark_assistant_queued(self, task: Task) -> None:
        message = self._assistant_message_for_task(task)
        if message is not None:
            message.status = "queued"
            message.error_message = None

    def _assistant_message_for_task(self, task: Task) -> ChatMessage | None:
        message_id = task.payload.get("assistant_message_id") if isinstance(task.payload, dict) else None
        if not isinstance(message_id, str):
            return None
        return self.db.scalar(
            select(ChatMessage).where(
                ChatMessage.id == message_id,
                ChatMessage.workspace_id == task.workspace_id,
                ChatMessage.task_id == task.id,
                ChatMessage.role == "assistant",
            )
        )

    def _create_conversation(self, *, workspace_id: str, user_id: str, prompt: str) -> ChatConversation:
        conversation = ChatConversation(
            workspace_id=workspace_id,
            user_id=user_id,
            title=self._title_from_prompt(prompt),
        )
        self.db.add(conversation)
        self.db.flush()
        return conversation

    def _normalize_attachments(self, *, workspace_id: str, raw_attachments: object) -> list[dict]:
        if raw_attachments is None:
            return []
        if not isinstance(raw_attachments, list):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="input.attachments must be a list",
            )
        if len(raw_attachments) > 20:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="A chat message can reference at most 20 attachments",
            )
        document_ids: list[str] = []
        for item in raw_attachments:
            document_id = item.get("document_id") if isinstance(item, dict) else None
            if not isinstance(document_id, str) or not document_id.strip():
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Each attachment must contain document_id",
                )
            normalized_id = document_id.strip()
            if normalized_id not in document_ids:
                document_ids.append(normalized_id)
        if not document_ids:
            return []
        documents = self.db.scalars(
            select(Document).where(
                Document.workspace_id == workspace_id,
                Document.id.in_(document_ids),
                Document.status.in_(("uploaded", "ready")),
            )
        ).all()
        by_id = {document.id: document for document in documents}
        if any(document_id not in by_id for document_id in document_ids):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment document not found")
        return [self._attachment_from_document(by_id[document_id]) for document_id in document_ids]

    @staticmethod
    def _attachment_from_document(document: Document) -> dict:
        return {
            "document_id": document.id,
            "filename": document.original_filename,
            "title": document.title,
            "mime_type": document.mime_type,
            "file_type": document.file_type,
            "file_size": document.file_size,
            "status": document.status,
            "availability": "available",
        }

    @staticmethod
    def _content_with_attachments(content: str, attachments: list[dict]) -> str:
        if not attachments:
            return content
        lines = [content, "", "NotePatch attachments referenced by this message:"]
        for item in attachments:
            label = item.get("filename") or item.get("title") or item.get("document_id")
            line = (
                f"- {label} (document_id={item.get('document_id')}, "
                f"availability={item.get('availability')})"
            )
            original_path = item.get("original_path")
            if isinstance(original_path, str) and original_path:
                line += f"\n  original: {original_path}"
            ocr_markdown_path = item.get("ocr_markdown_path")
            if isinstance(ocr_markdown_path, str) and ocr_markdown_path:
                line += f"\n  OCR markdown: {ocr_markdown_path}"
            ocr_text_path = item.get("ocr_text_path")
            if isinstance(ocr_text_path, str) and ocr_text_path:
                line += f"\n  OCR text: {ocr_text_path}"
            lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def _safe_citation(item: dict) -> dict:
        return {
            key: item[key]
            for key in ("chunk_id", "document_id", "score", "metadata")
            if key in item
        }

    def _touch_conversation(self, conversation_id: str) -> None:
        conversation = self.db.get(ChatConversation, conversation_id)
        if conversation is not None:
            conversation.last_message_at = utcnow()

    @staticmethod
    def _title_from_prompt(value: str) -> str:
        normalized = re.sub(r"\s+", " ", value).strip()
        return normalized[:160] or "新对话"

from __future__ import annotations

import re

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from notepatch.platform.config import get_settings
from notepatch.platform.database import utcnow
from notepatch.modules.ai.models.chat import ChatConversation, ChatMessage
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
        now = utcnow()
        user_message = ChatMessage(
            workspace_id=workspace_id,
            conversation_id=conversation.id,
            user_id=user.id,
            role="user",
            content=prompt.strip(),
            status="succeeded",
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
                "input": input_payload,
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

    def history_for_task(self, task: Task) -> list[dict[str, str]]:
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
        return [
            {"role": message.role, "content": message.content}
            for message in reversed(messages)
            if message.content.strip()
        ]

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

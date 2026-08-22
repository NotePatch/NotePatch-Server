from __future__ import annotations

import re

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from notepatch.platform.config import get_settings
from notepatch.platform.database import utcnow
from notepatch.modules.ai.models.chat import ChatConversation, ChatMessage
from notepatch.modules.documents.models.document import Document
from notepatch.modules.documents.services.purge import DocumentPurgeService
from notepatch.modules.tasks.models.task import Task
from notepatch.modules.identity.models.user import User
from notepatch.modules.identity.services.ai_preferences import AI_ONBOARDING_VERSION, AiPreferenceService
from notepatch.modules.tasks.services.task import TaskService
from notepatch.platform.storage import StorageService


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
        client_locale: str,
        task_service: TaskService,
    ) -> Task:
        self._require_onboarding(user)
        conversation = (
            self.get_active_conversation(workspace_id=workspace_id, user_id=user.id, conversation_id=conversation_id)
            if conversation_id
            else self._create_conversation(workspace_id=workspace_id, user_id=user.id, prompt=prompt)
        )
        normalized_input = dict(input_payload)
        attachments = self._normalize_attachments(
            workspace_id=workspace_id,
            conversation_id=conversation.id,
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
                "client_locale": client_locale,
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

    @staticmethod
    def _require_onboarding(user: User) -> None:
        if AiPreferenceService.is_completed(user):
            return
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "ai_onboarding_required",
                "version": AI_ONBOARDING_VERSION,
                "onboarding_url": "/api/v1/auth/ai-onboarding",
            },
        )

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
        include_superseded: bool = False,
    ) -> tuple[list[ChatMessage], int]:
        query = select(ChatMessage).where(
            ChatMessage.workspace_id == conversation.workspace_id,
            ChatMessage.conversation_id == conversation.id,
            ChatMessage.user_id == user_id,
        )
        if not include_superseded:
            query = query.where(ChatMessage.superseded_at.is_(None))
        total = int(self.db.scalar(select(func.count()).select_from(query.subquery())) or 0)
        items = self.db.scalars(
            query.order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return items, total

    def rename_conversation(self, conversation: ChatConversation, title: str) -> ChatConversation:
        conversation.title = self._title_from_prompt(title)
        conversation.title_source = "manual"
        conversation.title_generated_at = None
        self.db.commit()
        self.db.refresh(conversation)
        return conversation

    def title_context_for_task(self, task: Task, *, limit: int) -> tuple[ChatConversation, list[dict[str, str]]] | None:
        conversation_id = task.payload.get("conversation_id") if isinstance(task.payload, dict) else None
        if not isinstance(conversation_id, str):
            return None
        conversation = self.db.scalar(
            select(ChatConversation).where(
                ChatConversation.id == conversation_id,
                ChatConversation.workspace_id == task.workspace_id,
                ChatConversation.deleted_at.is_(None),
            )
        )
        if conversation is None or conversation.title_source != "prompt":
            return None
        messages = self.db.scalars(
            select(ChatMessage)
            .where(
                ChatMessage.workspace_id == task.workspace_id,
                ChatMessage.conversation_id == conversation.id,
                ChatMessage.status == "succeeded",
                ChatMessage.role.in_(("user", "assistant")),
                ChatMessage.superseded_at.is_(None),
            )
            .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
            .limit(max(limit, 2))
        ).all()
        context = [
            {"role": message.role, "content": message.content.strip()[:1200]}
            for message in messages
            if message.content.strip()
        ]
        if not any(item["role"] == "user" for item in context) or not any(
            item["role"] == "assistant" for item in context
        ):
            return None
        return conversation, context

    def apply_generated_title(
        self,
        *,
        conversation_id: str,
        generated_title: str,
        max_length: int,
    ) -> ChatConversation | None:
        conversation = self.db.scalar(
            select(ChatConversation)
            .where(
                ChatConversation.id == conversation_id,
                ChatConversation.deleted_at.is_(None),
            )
            .with_for_update()
        )
        if conversation is None or conversation.title_source != "prompt":
            return None
        title = self._normalize_generated_title(generated_title, max_length=max_length)
        if not title:
            return None
        conversation.title = title
        conversation.title_source = "ai"
        conversation.title_generated_at = utcnow()
        return conversation

    def delete_conversation(
        self,
        conversation: ChatConversation,
        *,
        storage: StorageService | None = None,
    ) -> None:
        ephemeral_documents = self.db.scalars(
            select(Document).where(
                Document.workspace_id == conversation.workspace_id,
                Document.chat_conversation_id == conversation.id,
                Document.retention_scope == "conversation",
                Document.status != "deleted",
            )
        ).all()
        if ephemeral_documents and storage is None:
            raise RuntimeError("Storage service is required to delete conversation attachments")
        for document in ephemeral_documents:
            DocumentPurgeService(self.db, storage).request_purge(
                conversation.workspace_id,
                document.id,
            )
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
                ChatMessage.superseded_at.is_(None),
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

    def attachment_document_ids_for_task(self, task: Task) -> set[str]:
        """Return only documents needed to resolve current and enabled chat history attachments."""
        if not isinstance(task.payload, dict):
            return set()
        conversation_id = task.payload.get("conversation_id")
        current_user_message_id = task.payload.get("user_message_id")
        if not isinstance(conversation_id, str) or not isinstance(current_user_message_id, str):
            return set()

        message_ids = [current_user_message_id]
        if task.payload.get("ai_history_enabled") is not False:
            historical_ids = self.db.scalars(
                select(ChatMessage.id)
                .where(
                    ChatMessage.workspace_id == task.workspace_id,
                    ChatMessage.conversation_id == conversation_id,
                    ChatMessage.status == "succeeded",
                    ChatMessage.role == "user",
                    ChatMessage.superseded_at.is_(None),
                )
                .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
                .limit(self.settings.ai_chat_history_message_limit)
            ).all()
            message_ids.extend(historical_ids)

        messages = self.db.scalars(
            select(ChatMessage).where(
                ChatMessage.workspace_id == task.workspace_id,
                ChatMessage.conversation_id == conversation_id,
                ChatMessage.id.in_(set(message_ids)),
                ChatMessage.role == "user",
                ChatMessage.superseded_at.is_(None),
            )
        ).all()
        document_ids: set[str] = set()
        for message in messages:
            for attachment in message.attachments or []:
                document_id = attachment.get("document_id") if isinstance(attachment, dict) else None
                if isinstance(document_id, str) and document_id:
                    document_ids.add(document_id)
        return document_ids

    def revise_message(
        self,
        *,
        workspace_id: str,
        user: User,
        conversation_id: str,
        message_id: str,
        prompt: str,
        input_payload: dict | None,
        options: dict,
        client_locale: str,
        task_service: TaskService,
    ) -> Task:
        self._require_onboarding(user)
        conversation = self.db.scalar(
            select(ChatConversation)
            .where(
                ChatConversation.id == conversation_id,
                ChatConversation.workspace_id == workspace_id,
                ChatConversation.user_id == user.id,
                ChatConversation.deleted_at.is_(None),
            )
            .with_for_update()
        )
        target = self.db.scalar(
            select(ChatMessage)
            .where(
                ChatMessage.id == message_id,
                ChatMessage.workspace_id == workspace_id,
                ChatMessage.conversation_id == conversation_id,
                ChatMessage.user_id == user.id,
                ChatMessage.role == "user",
                ChatMessage.superseded_at.is_(None),
            )
            .with_for_update()
        )
        if conversation is None or target is None:
            raise ChatConversationNotFoundError("Message not found")

        active_messages = self.db.scalars(
            select(ChatMessage)
            .where(
                ChatMessage.workspace_id == workspace_id,
                ChatMessage.conversation_id == conversation_id,
                ChatMessage.user_id == user.id,
                ChatMessage.superseded_at.is_(None),
            )
            .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
            .with_for_update()
        ).all()
        try:
            target_index = next(index for index, message in enumerate(active_messages) if message.id == target.id)
        except StopIteration as exc:
            raise ChatConversationNotFoundError("Message not found") from exc

        normalized_input = dict(input_payload or {})
        if input_payload is None or "attachments" not in normalized_input:
            normalized_input["attachments"] = [
                {"document_id": item["document_id"]}
                for item in target.attachments
                if isinstance(item, dict) and isinstance(item.get("document_id"), str)
            ]
        attachments = self._normalize_attachments(
            workspace_id=workspace_id,
            conversation_id=conversation.id,
            raw_attachments=normalized_input.get("attachments"),
        )
        normalized_input["attachments"] = attachments

        task_service.cancel_active_tasks(
            workspace_id=workspace_id,
            resource_type="chat_conversation",
            resource_id=conversation.id,
            reason="Conversation message was revised",
            task_types=("openclaw_agent_run",),
            commit=False,
        )
        replacement = ChatMessage(
            workspace_id=workspace_id,
            conversation_id=conversation.id,
            user_id=user.id,
            role="user",
            content=prompt.strip(),
            status="succeeded",
            attachments=attachments,
            revision_of_message_id=target.id,
        )
        assistant = ChatMessage(
            workspace_id=workspace_id,
            conversation_id=conversation.id,
            user_id=user.id,
            role="assistant",
            content="",
            status="queued",
        )
        self.db.add_all((replacement, assistant))
        self.db.flush()
        now = utcnow()
        for old_message in active_messages[target_index:]:
            old_message.superseded_at = now
            old_message.superseded_by_message_id = replacement.id
        conversation.last_message_at = now
        if conversation.title_source != "manual":
            conversation.title = self._title_from_prompt(prompt)
            conversation.title_source = "prompt"
            conversation.title_generated_at = None

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
                "user_message_id": replacement.id,
                "assistant_message_id": assistant.id,
                "revised_message_id": target.id,
                "ai_history_enabled": bool(user.ai_history_enabled),
                "client_locale": client_locale,
            },
            enqueue=False,
        )
        assistant.task_id = task.id
        self.db.commit()
        self.db.refresh(task)
        if not task_service.enqueue_task(task.id):
            self.mark_assistant_failed(task, "Task queue is unavailable")
            self.db.commit()
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Task queue is unavailable")
        return task

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
        if message is not None and message.status in {"queued", "running"}:
            message.status = "running"
            message.content = ""
            message.error_message = None
            self.db.commit()

    def append_assistant_stream_delta(self, task: Task, delta: str) -> None:
        if not delta:
            return
        message = self._assistant_message_for_task(task)
        if message is None or message.status != "running":
            return
        message.content = f"{message.content}{delta}"
        self._touch_conversation(message.conversation_id)

    def mark_assistant_cancelled(self, task: Task, reason: str) -> None:
        message = self._assistant_message_for_task(task)
        if message is None or message.status in {"succeeded", "failed", "cancelled"}:
            return
        message.status = "cancelled"
        message.error_message = reason[:2000]
        self._touch_conversation(message.conversation_id)

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
            title_source="prompt",
        )
        self.db.add(conversation)
        self.db.flush()
        return conversation

    def _normalize_attachments(
        self,
        *,
        workspace_id: str,
        conversation_id: str,
        raw_attachments: object,
    ) -> list[dict]:
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
            ).with_for_update()
        ).all()
        by_id = {
            document.id: document
            for document in documents
            if document.retention_scope == "workspace"
            or document.chat_conversation_id in {None, conversation_id}
        }
        if any(document_id not in by_id for document_id in document_ids):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment document not found")
        for document in by_id.values():
            if document.retention_scope == "conversation" and document.chat_conversation_id is None:
                document.chat_conversation_id = conversation_id
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
            "save_to_documents": document.save_to_documents,
            "retention_scope": document.retention_scope,
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

    @staticmethod
    def _normalize_generated_title(value: str, *, max_length: int) -> str:
        normalized = value.strip()
        if normalized.startswith("{"):
            try:
                import json

                parsed = json.loads(normalized)
                if isinstance(parsed, dict) and isinstance(parsed.get("title"), str):
                    normalized = parsed["title"]
            except (ValueError, TypeError):
                pass
        normalized = normalized.splitlines()[0].strip()
        normalized = re.sub(r"^(?:标题|title)\s*[:：]\s*", "", normalized, flags=re.IGNORECASE)
        normalized = normalized.strip(" \t\r\n\"'`#《》")
        normalized = re.sub(r"\s+", " ", normalized)
        limit = max(1, min(max_length, 160))
        if len(normalized) <= limit:
            return normalized
        truncated = normalized[:limit].rstrip()
        if " " in truncated and limit < len(normalized) and normalized[limit : limit + 1].isalnum():
            word_boundary = truncated.rfind(" ")
            if word_boundary >= max(8, limit // 2):
                truncated = truncated[:word_boundary]
        return truncated

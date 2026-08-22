from abc import ABC, abstractmethod
import base64
import json
from pathlib import Path, PurePosixPath
import re
import threading
import time
from collections.abc import Callable

import httpx

from notepatch.modules.identity.services.ai_preferences import AiPreferenceService
from notepatch.platform.config import get_settings
from notepatch.platform.errors import RetryableTaskError, TaskCancelledError


class OpenClawRunnerError(RetryableTaskError):
    """Raised when an OpenClaw runner cannot complete a task."""


class OpenClawRunner(ABC):
    @abstractmethod
    def prepare_task_dir(self, workspace_id: str, task_id: str) -> Path:
        raise NotImplementedError

    @abstractmethod
    def run_task(self, workspace_id: str, task_id: str, payload: dict) -> dict:
        raise NotImplementedError

    @abstractmethod
    def collect_output(self, workspace_id: str, task_id: str) -> dict:
        raise NotImplementedError

    @abstractmethod
    def cleanup(self, workspace_id: str, task_id: str) -> None:
        raise NotImplementedError

    def generate_conversation_title(
        self,
        workspace_id: str,
        conversation_id: str,
        messages: list[dict[str, str]],
        *,
        runtime: dict,
        provider_model: str,
        client_locale: str,
        max_length: int,
        timeout_seconds: float,
    ) -> str | None:
        return None

    def generate_image_remark(
        self,
        workspace_id: str,
        document_id: str,
        ocr_text: str,
        *,
        original_filename: str,
        runtime: dict,
        provider_model: str,
        max_length: int,
        timeout_seconds: float,
    ) -> str | None:
        return None

    def run_chat_task(
        self,
        workspace_id: str,
        task_id: str,
        payload: dict,
        *,
        on_stream_event: Callable[[str, str], None] | None = None,
        is_cancel_requested: Callable[[], bool] | None = None,
    ) -> dict:
        """Run a chat task.

        Non-gateway fakes deliberately retain the regular task contract. The
        task handler turns their final answer into one persisted stream delta.
        """
        if is_cancel_requested and is_cancel_requested():
            raise TaskCancelledError("Task cancellation was requested")
        return self.run_task(workspace_id, task_id, payload)


class LocalTaskDirMixin:
    def __init__(self) -> None:
        self.base_dir = Path(get_settings().openclaw_workdir)

    def prepare_task_dir(self, workspace_id: str, task_id: str) -> Path:
        task_dir = self.base_dir / "workspaces" / workspace_id / "tasks" / task_id
        (task_dir / "input").mkdir(parents=True, exist_ok=True)
        (task_dir / "output").mkdir(parents=True, exist_ok=True)
        return task_dir

    def collect_output(self, workspace_id: str, task_id: str) -> dict:
        task_dir = self.prepare_task_dir(workspace_id, task_id)
        return {
            "output_dir": str(task_dir / "output"),
            "files": [path.name for path in (task_dir / "output").iterdir()],
        }

    def cleanup(self, workspace_id: str, task_id: str) -> None:
        return None


class OpenClawGatewayRunner(LocalTaskDirMixin, OpenClawRunner):
    _EMBEDDED_ERROR_PATTERNS = (
        re.compile(r"api rate limit reached", re.IGNORECASE),
        re.compile(r"all credentials.+cooling down", re.IGNORECASE),
        re.compile(r"no api key found", re.IGNORECASE),
        re.compile(r"provider credentials? (?:is|are) unavailable", re.IGNORECASE),
    )

    def __init__(self, client: httpx.Client | None = None) -> None:
        super().__init__()
        self.settings = get_settings()
        self.base_url = self.settings.openclaw_gateway_base_url.rstrip("/")
        self.model = self.settings.openclaw_gateway_model
        self.scopes = self.settings.openclaw_gateway_scopes
        self._client = client or httpx.Client(timeout=self.settings.openclaw_gateway_timeout_seconds)
        self._task_output_dirs: dict[tuple[str, str], Path] = {}

    def run_task(self, workspace_id: str, task_id: str, payload: dict) -> dict:
        task_dir = self.prepare_task_dir(workspace_id, task_id)
        runtime = payload.get("_openclaw") if isinstance(payload.get("_openclaw"), dict) else {}
        gateway_url = self._gateway_url(runtime)
        gateway_token = runtime.get("gateway_token") if isinstance(runtime.get("gateway_token"), str) else None
        output_dir = runtime.get("host_task_output_dir") if isinstance(runtime.get("host_task_output_dir"), str) else None
        if output_dir:
            self._task_output_dirs[(workspace_id, task_id)] = Path(output_dir)
            Path(output_dir).mkdir(parents=True, exist_ok=True)
        request_body = self._build_request_body(payload)
        provider_model = self._provider_model(payload)
        session_key = runtime.get("session_key") if isinstance(runtime.get("session_key"), str) else None
        timeout_seconds = self._request_timeout_seconds(runtime)
        response_json = self._post_chat_completion(
            request_body,
            gateway_url=gateway_url,
            gateway_token=gateway_token,
            session_key=session_key,
            provider_model=provider_model,
            timeout_seconds=timeout_seconds,
        )
        answer = self._extract_answer(response_json)
        self._raise_embedded_gateway_error(answer)
        result = {
            "runner": "gateway",
            "answer": answer,
            "model": request_body["model"],
            "gateway_model": request_body["model"],
            "provider_model": provider_model,
            "response": response_json,
            "gateway_url": gateway_url,
            "gateway_container": runtime.get("gateway_container"),
            "user_workspace_dir": runtime.get("user_workspace_dir"),
        }
        result_path = (Path(output_dir) if output_dir else task_dir / "output") / "result.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return result

    def run_chat_task(
        self,
        workspace_id: str,
        task_id: str,
        payload: dict,
        *,
        on_stream_event: Callable[[str, str], None] | None = None,
        is_cancel_requested: Callable[[], bool] | None = None,
    ) -> dict:
        task_dir = self.prepare_task_dir(workspace_id, task_id)
        runtime = payload.get("_openclaw") if isinstance(payload.get("_openclaw"), dict) else {}
        gateway_url = self._gateway_url(runtime)
        gateway_token = runtime.get("gateway_token") if isinstance(runtime.get("gateway_token"), str) else None
        output_dir = runtime.get("host_task_output_dir") if isinstance(runtime.get("host_task_output_dir"), str) else None
        if output_dir:
            self._task_output_dirs[(workspace_id, task_id)] = Path(output_dir)
            Path(output_dir).mkdir(parents=True, exist_ok=True)
        request_body = self._build_request_body(payload)
        request_body["stream"] = True
        request_body.update(self._thinking_request_options(payload))
        provider_model = self._provider_model(payload)
        session_key = runtime.get("session_key") if isinstance(runtime.get("session_key"), str) else None
        timeout_seconds = self._request_timeout_seconds(runtime)
        answer, reasoning = self._stream_chat_completion(
            request_body,
            gateway_url=gateway_url,
            gateway_token=gateway_token,
            session_key=session_key,
            provider_model=provider_model,
            timeout_seconds=timeout_seconds,
            on_stream_event=on_stream_event,
            is_cancel_requested=is_cancel_requested,
        )
        self._raise_embedded_gateway_error(answer)
        result = {
            "runner": "gateway",
            "answer": answer,
            "reasoning_summary": reasoning,
            "model": request_body["model"],
            "gateway_model": request_body["model"],
            "provider_model": provider_model,
            "gateway_url": gateway_url,
            "gateway_container": runtime.get("gateway_container"),
            "user_workspace_dir": runtime.get("user_workspace_dir"),
        }
        result_path = (Path(output_dir) if output_dir else task_dir / "output") / "result.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    def generate_conversation_title(
        self,
        workspace_id: str,
        conversation_id: str,
        messages: list[dict[str, str]],
        *,
        runtime: dict,
        provider_model: str,
        client_locale: str,
        max_length: int,
        timeout_seconds: float,
    ) -> str | None:
        transcript = [
            {
                "role": item["role"],
                "content": item["content"][:1200],
            }
            for item in messages
            if item.get("role") in {"user", "assistant"} and item.get("content")
        ]
        if not transcript:
            return None
        gateway_url = self._gateway_url(runtime)
        gateway_token = runtime.get("gateway_token") if isinstance(runtime.get("gateway_token"), str) else None
        title_instruction = (
            "This is a title-generation task, not a request to continue or answer the conversation. "
            "Determine the dominant language using only user-role messages and write a concise noun-phrase "
            "title in that language. If the user text is too short, mixed-language, or otherwise ambiguous, "
            f"use the client locale {client_locale}. Use at most {max_length} characters. "
            "Return only the title. Never answer the transcript, acknowledge the request, narrate your intent, "
            "use first person, add quotes, terminal punctuation, Markdown, a prefix, or an explanation."
        )
        request_body = {
            "model": self._gateway_request_model({}),
            "stream": False,
            "reasoning_effort": "none",
            "messages": [
                {
                    "role": "system",
                    "content": title_instruction,
                },
                {
                    "role": "user",
                    "content": (
                        f"{title_instruction}\n\n"
                        "Conversation transcript JSON:\n"
                        f"{json.dumps(transcript, ensure_ascii=False)}\n\n"
                        "Title only:"
                    ),
                },
            ],
        }
        response_json = self._post_chat_completion(
            request_body,
            gateway_url=gateway_url,
            gateway_token=gateway_token,
            session_key=f"notepatch:{workspace_id}:chat-title:{conversation_id}",
            provider_model=provider_model,
            timeout_seconds=timeout_seconds,
        )
        title = self._extract_answer(response_json).strip()
        self._raise_embedded_gateway_error(title)
        return title or None

    def generate_image_remark(
        self,
        workspace_id: str,
        document_id: str,
        ocr_text: str,
        *,
        original_filename: str,
        runtime: dict,
        provider_model: str,
        max_length: int,
        timeout_seconds: float,
    ) -> str | None:
        instruction = (
            "Write a concise user-facing remark for an uploaded image using only its OCR text. "
            "Summarize the recognizable subject or topic; do not describe visual details that OCR cannot prove. "
            "Use the dominant language of the OCR text. Do not repeat the upload filename, infer private identity, "
            "or foreground school, company, manufacturer, or notebook branding unless it is the actual subject. "
            f"Use at most {max_length} characters. Return only the remark without quotes, "
            "Markdown, prefixes, explanations, or terminal punctuation."
        )
        request_body = {
            "model": self._gateway_request_model({}),
            "stream": False,
            "reasoning_effort": "minimal",
            "reasoning_mode": "off",
            "messages": [
                {"role": "system", "content": instruction},
                {
                    "role": "user",
                    "content": (
                        f"{instruction}\n\n"
                        f"Original filename (context only, never copy it): {original_filename}\n"
                        f"OCR text:\n{ocr_text}"
                    ),
                },
            ],
        }
        response_json = self._post_chat_completion(
            request_body,
            gateway_url=self._gateway_url(runtime),
            gateway_token=(
                runtime.get("gateway_token")
                if isinstance(runtime.get("gateway_token"), str)
                else None
            ),
            session_key=f"notepatch:{workspace_id}:image-remark:{document_id}",
            provider_model=provider_model,
            timeout_seconds=timeout_seconds,
        )
        remark = self._extract_answer(response_json).strip()
        self._raise_embedded_gateway_error(remark)
        return remark or None

    def collect_output(self, workspace_id: str, task_id: str) -> dict:
        output_dir = self._task_output_dirs.get((workspace_id, task_id))
        if output_dir is None:
            return super().collect_output(workspace_id, task_id)
        output_dir.mkdir(parents=True, exist_ok=True)
        files = [
            str(path.relative_to(output_dir))
            for path in sorted(output_dir.rglob("*"))
            if path.is_file()
        ]
        return {"output_dir": str(output_dir), "files": files}

    def _build_request_body(self, payload: dict) -> dict:
        options = payload.get("options") if isinstance(payload.get("options"), dict) else {}
        model = self._gateway_request_model(options)
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise OpenClawRunnerError("OpenClaw task payload.prompt must be a non-empty string")

        messages = self._conversation_messages(payload)
        preference_snapshot = payload.get("ai_preferences")
        if isinstance(preference_snapshot, dict):
            preference_domain = payload.get("preference_domain")
            if not isinstance(preference_domain, str) or not preference_domain.strip():
                preference_domain = "chat"
            messages.insert(
                0,
                {
                    "role": "system",
                    "content": AiPreferenceService.system_instruction(
                        preference_snapshot,
                        domain=preference_domain,
                        client_locale=(
                            payload.get("client_locale")
                            if isinstance(payload.get("client_locale"), str)
                            else None
                        ),
                    ),
                },
            )
        current_user_index = self._current_user_message_index(messages, prompt.strip())
        if current_user_index is None:
            messages.append({"role": "user", "content": prompt.strip()})
            current_user_index = len(messages) - 1

        user_content = messages[current_user_index]["content"]
        input_payload = payload.get("input")
        if input_payload:
            user_content = (
                f"{user_content}\n\n"
                "Context JSON:\n"
                f"{json.dumps(input_payload, ensure_ascii=False, indent=2)}"
            )
        runtime = payload.get("_openclaw") if isinstance(payload.get("_openclaw"), dict) else {}
        context_note = self._context_note(runtime)
        if context_note:
            user_content = f"{user_content}\n\n{context_note}"

        image_parts = self._image_parts(input_payload, runtime)
        message_content: str | list[dict] = user_content
        if image_parts:
            message_content = [{"type": "text", "text": user_content}, *image_parts]

        messages[current_user_index] = {"role": "user", "content": message_content}
        request_body = {
            "model": model,
            "stream": False,
            "messages": messages,
        }
        temperature = options.get("temperature")
        if isinstance(temperature, (int, float)) and not isinstance(temperature, bool):
            request_body["temperature"] = float(temperature)
        return request_body

    @staticmethod
    def _thinking_request_options(payload: dict) -> dict[str, str]:
        options = payload.get("options") if isinstance(payload.get("options"), dict) else {}
        thinking = options.get("thinking") if isinstance(options.get("thinking"), dict) else {}
        enabled = bool(thinking.get("enabled"))
        effort = thinking.get("effort") if isinstance(thinking.get("effort"), str) else "low"
        if not enabled:
            return {"reasoning_effort": "none", "reasoning_mode": "off"}
        return {"reasoning_effort": effort, "reasoning_mode": "stream"}

    @staticmethod
    def _conversation_messages(payload: dict) -> list[dict[str, str]]:
        raw_messages = payload.get("conversation_messages")
        if not isinstance(raw_messages, list):
            return []
        messages: list[dict[str, str]] = []
        for item in raw_messages:
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            content = item.get("content")
            if role not in {"user", "assistant"} or not isinstance(content, str) or not content.strip():
                continue
            messages.append({"role": role, "content": content.strip()})
        return messages

    @staticmethod
    def _image_parts(input_payload: object, runtime: dict) -> list[dict]:
        if not isinstance(input_payload, dict):
            return []
        attachments = input_payload.get("attachments")
        if not isinstance(attachments, list):
            return []
        images = [
            item
            for item in attachments
            if isinstance(item, dict)
            and item.get("file_type") == "image"
            and isinstance(item.get("original_path"), str)
        ]
        if not images:
            return []
        if len(images) > 8:
            raise OpenClawRunnerError("A chat request can include at most 8 image attachments")

        host_input_dir = runtime.get("host_task_input_dir")
        container_documents_root = runtime.get("documents_root_path")
        if not isinstance(host_input_dir, str) or not isinstance(container_documents_root, str):
            raise OpenClawRunnerError("OpenClaw image attachment runtime paths are unavailable")

        host_documents_root = (Path(host_input_dir) / "documents").resolve()
        container_root = PurePosixPath(container_documents_root)
        parts: list[dict] = []
        total_bytes = 0
        allowed_mime_types = {"image/jpeg", "image/png", "image/webp", "image/gif"}
        for item in images:
            mime_type = item.get("mime_type")
            if mime_type not in allowed_mime_types:
                raise OpenClawRunnerError(f"Unsupported chat image MIME type: {mime_type}")
            try:
                relative_path = PurePosixPath(item["original_path"]).relative_to(container_root)
            except ValueError as exc:
                raise OpenClawRunnerError("Chat image path is outside the task document snapshot") from exc
            image_path = (host_documents_root / Path(*relative_path.parts)).resolve()
            if not image_path.is_relative_to(host_documents_root):
                raise OpenClawRunnerError("Chat image path escapes the task document snapshot")
            if not image_path.is_file():
                raise OpenClawRunnerError(f"Chat image snapshot is missing: {item.get('document_id')}")

            image_bytes = image_path.read_bytes()
            if len(image_bytes) > 10 * 1024 * 1024:
                raise OpenClawRunnerError("A chat image exceeds the 10 MB gateway limit")
            total_bytes += len(image_bytes)
            if total_bytes > 20 * 1024 * 1024:
                raise OpenClawRunnerError("Chat images exceed the 20 MB gateway request limit")
            encoded = base64.b64encode(image_bytes).decode("ascii")
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
                }
            )
        return parts


    @staticmethod
    def _current_user_message_index(messages: list[dict[str, str]], prompt: str) -> int | None:
        for index in range(len(messages) - 1, -1, -1):
            if messages[index]["role"] == "user" and messages[index]["content"] == prompt:
                return index
        return None

    def _gateway_request_model(self, options: dict) -> str:
        for key in ("gateway_model", "model"):
            value = options.get(key)
            if isinstance(value, str) and self._is_valid_gateway_model(value):
                return value.strip()
        if self._is_valid_gateway_model(self.model):
            return self.model.strip()
        return "openclaw"

    @staticmethod
    def _is_valid_gateway_model(value: str) -> bool:
        normalized = value.strip().lower()
        return normalized == "openclaw" or normalized.startswith("openclaw/")

    def _headers(
        self,
        gateway_token: str | None = None,
        session_key: str | None = None,
        provider_model: str | None = None,
    ) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        token = gateway_token or self.settings.openclaw_gateway_token
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if self.scopes:
            headers["x-openclaw-scopes"] = self.scopes
        if session_key:
            headers["x-openclaw-session-key"] = session_key
        if provider_model:
            headers["x-openclaw-model"] = provider_model
        return headers

    def _post_chat_completion(
        self,
        request_body: dict,
        *,
        gateway_url: str | None = None,
        gateway_token: str | None = None,
        session_key: str | None = None,
        provider_model: str | None = None,
        timeout_seconds: float | None = None,
    ) -> dict:
        base_url = (gateway_url or self.base_url).rstrip("/")
        self._wait_until_ready(base_url)
        url = f"{base_url}/v1/chat/completions"
        try:
            response = self._client.post(
                url,
                headers=self._headers(gateway_token, session_key, provider_model),
                json=request_body,
                timeout=timeout_seconds or self.settings.openclaw_gateway_timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise OpenClawRunnerError(f"OpenClaw gateway request timed out: {url}") from exc
        except httpx.HTTPError as exc:
            raise OpenClawRunnerError(f"OpenClaw gateway request failed: {exc}") from exc

        if response.status_code < 200 or response.status_code >= 300:
            detail = response.text[:500]
            raise OpenClawRunnerError(f"OpenClaw gateway returned HTTP {response.status_code}: {detail}")

        try:
            parsed = response.json()
        except ValueError as exc:
            raise OpenClawRunnerError("OpenClaw gateway returned a non-JSON response") from exc
        if not isinstance(parsed, dict):
            raise OpenClawRunnerError("OpenClaw gateway returned an invalid JSON response")
        return parsed

    def _stream_chat_completion(
        self,
        request_body: dict,
        *,
        gateway_url: str,
        gateway_token: str | None,
        session_key: str | None,
        provider_model: str | None,
        timeout_seconds: float,
        on_stream_event: Callable[[str, str], None] | None,
        is_cancel_requested: Callable[[], bool] | None,
    ) -> tuple[str, str]:
        self._wait_until_ready(gateway_url)
        url = f"{gateway_url.rstrip('/')}/v1/chat/completions"
        if is_cancel_requested and is_cancel_requested():
            raise TaskCancelledError("Task cancellation was requested")
        stop_watcher = threading.Event()
        cancelled = threading.Event()
        response_holder: dict[str, httpx.Response] = {}

        def watch_cancellation() -> None:
            while not stop_watcher.wait(max(self.settings.ai_chat_cancel_poll_seconds, 0.05)):
                if is_cancel_requested and is_cancel_requested():
                    cancelled.set()
                    response = response_holder.get("response")
                    if response is not None:
                        response.close()
                    return

        watcher = threading.Thread(target=watch_cancellation, name=f"chat-cancel-{session_key or 'task'}", daemon=True)
        answer_parts: list[str] = []
        reasoning_parts: list[str] = []
        try:
            with self._client.stream(
                "POST",
                url,
                headers={**self._headers(gateway_token, session_key, provider_model), "Accept": "text/event-stream"},
                json=request_body,
                timeout=timeout_seconds,
            ) as response:
                response_holder["response"] = response
                if response.status_code < 200 or response.status_code >= 300:
                    detail = response.read().decode("utf-8", errors="replace")[:500]
                    raise OpenClawRunnerError(f"OpenClaw gateway returned HTTP {response.status_code}: {detail}")
                watcher.start()
                for line in response.iter_lines():
                    if cancelled.is_set() or (is_cancel_requested and is_cancel_requested()):
                        raise TaskCancelledError("Task cancellation was requested")
                    if not line or not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        break
                    try:
                        event = json.loads(raw)
                    except ValueError as exc:
                        raise OpenClawRunnerError("OpenClaw gateway returned invalid stream JSON") from exc
                    self._raise_stream_error(event)
                    for stream, delta in self._stream_deltas(event):
                        if stream == "answer":
                            answer_parts.append(delta)
                        else:
                            reasoning_parts.append(delta)
                        if on_stream_event is not None:
                            on_stream_event(stream, delta)
        except TaskCancelledError:
            raise
        except httpx.TimeoutException as exc:
            if cancelled.is_set():
                raise TaskCancelledError("Task cancellation was requested") from exc
            raise OpenClawRunnerError(f"OpenClaw gateway request timed out: {url}") from exc
        except httpx.HTTPError as exc:
            if cancelled.is_set() or (is_cancel_requested and is_cancel_requested()):
                raise TaskCancelledError("Task cancellation was requested") from exc
            raise OpenClawRunnerError(f"OpenClaw gateway stream failed: {exc}") from exc
        finally:
            stop_watcher.set()
            if watcher.is_alive():
                watcher.join(timeout=1)
        if cancelled.is_set() or (is_cancel_requested and is_cancel_requested()):
            raise TaskCancelledError("Task cancellation was requested")
        answer = "".join(answer_parts)
        if not answer.strip():
            raise OpenClawRunnerError("OpenClaw gateway stream returned no answer")
        return answer, "".join(reasoning_parts)

    @staticmethod
    def _raise_stream_error(event: object) -> None:
        if not isinstance(event, dict):
            return
        error = event.get("error")
        if not isinstance(error, dict):
            return
        message = error.get("message")
        detail = message.strip()[:500] if isinstance(message, str) and message.strip() else "unknown gateway error"
        raise OpenClawRunnerError(f"OpenClaw gateway stream error: {detail}")

    @staticmethod
    def _stream_deltas(event: object) -> list[tuple[str, str]]:
        if not isinstance(event, dict):
            return []
        choices = event.get("choices")
        if not isinstance(choices, list):
            return []
        deltas: list[tuple[str, str]] = []
        for choice in choices:
            delta = choice.get("delta") if isinstance(choice, dict) else None
            if not isinstance(delta, dict):
                continue
            content = delta.get("content")
            if isinstance(content, str) and content:
                deltas.append(("answer", content))
            for key in ("reasoning_content", "reasoning_summary", "reasoning"):
                reasoning = delta.get(key)
                if isinstance(reasoning, str) and reasoning:
                    deltas.append(("reasoning", reasoning))
                    break
        return deltas

    def _request_timeout_seconds(self, runtime: dict) -> float:
        value = runtime.get("timeout_seconds")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return float(value)
        return float(self.settings.openclaw_gateway_timeout_seconds)

    def _wait_until_ready(self, gateway_url: str) -> None:
        timeout = self.settings.openclaw_gateway_ready_timeout_seconds
        if timeout <= 0:
            return
        deadline = time.monotonic() + timeout
        last_error = "gateway is not ready"
        health_url = f"{gateway_url.rstrip('/')}/healthz"
        while time.monotonic() <= deadline:
            try:
                response = self._client.get(health_url)
                if 200 <= response.status_code < 300:
                    return
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
            except httpx.HTTPError as exc:
                last_error = str(exc)
            time.sleep(max(self.settings.openclaw_gateway_ready_poll_seconds, 0.1))
        raise OpenClawRunnerError(f"OpenClaw gateway was not ready at {health_url}: {last_error}")

    @staticmethod
    def _extract_answer(response_json: dict) -> str:
        choices = response_json.get("choices")
        if not isinstance(choices, list) or not choices:
            raise OpenClawRunnerError("OpenClaw gateway response is missing choices")
        first = choices[0]
        if not isinstance(first, dict):
            raise OpenClawRunnerError("OpenClaw gateway response choice is invalid")
        message = first.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise OpenClawRunnerError("OpenClaw gateway response is missing message content")
        return content

    @classmethod
    def _raise_embedded_gateway_error(cls, answer: str) -> None:
        normalized = answer.strip()
        for pattern in cls._EMBEDDED_ERROR_PATTERNS:
            if pattern.search(normalized):
                raise OpenClawRunnerError(f"OpenClaw provider error: {normalized[:500]}")

    def _gateway_url(self, runtime: dict) -> str:
        configured = runtime.get("gateway_url")
        if isinstance(configured, str) and configured.strip():
            return configured.rstrip("/")
        return self.base_url

    @staticmethod
    def _provider_model(payload: dict) -> str | None:
        value = payload.get("ai_model")
        if value is None:
            return None
        if not isinstance(value, str):
            raise OpenClawRunnerError("OpenClaw provider model must be a string")
        normalized = value.strip()
        if not normalized or len(normalized) > 255 or any(character.isspace() for character in normalized):
            raise OpenClawRunnerError("OpenClaw provider model is invalid")
        if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
            raise OpenClawRunnerError("OpenClaw provider model is invalid")
        return normalized

    @staticmethod
    def _context_note(runtime: dict) -> str:
        documents_index = runtime.get("documents_index_path")
        documents_root = runtime.get("documents_root_path")
        task_output = runtime.get("task_output_path")
        attachments = runtime.get("attachment_files")
        if not any(isinstance(value, str) and value for value in (documents_index, documents_root, task_output)):
            return ""
        lines = [
            "NotePatch user data is mounted in the OpenClaw workspace.\n"
            f"- Documents index: {documents_index}\n"
            f"- Documents root: {documents_root}\n"
            f"- Write task outputs to: {task_output}\n"
            "Use only the files under these paths for this NotePatch task."
        ]
        if isinstance(attachments, list) and attachments:
            lines.append("\nFiles explicitly attached to this message:")
            for item in attachments:
                if not isinstance(item, dict):
                    continue
                document_id = item.get("document_id")
                filename = item.get("filename") or "attachment"
                preferred = (
                    item.get("ocr_markdown_path")
                    or item.get("ocr_text_path")
                    or item.get("original_path")
                )
                lines.append(f"- {filename} (document_id={document_id}): {preferred}")
            lines.append(
                "Prefer OCR Markdown/text when listed. For an original binary file, use "
                f"`notepatch-file inspect` and write `notepatch-file extract` output under {task_output}/parser/."
            )
        return "\n".join(lines)


def get_openclaw_runner() -> OpenClawRunner:
    return OpenClawGatewayRunner()

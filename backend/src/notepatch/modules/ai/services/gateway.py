from abc import ABC, abstractmethod
import json
from pathlib import Path
import re
import time

import httpx

from notepatch.platform.config import get_settings
from notepatch.platform.errors import RetryableTaskError


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

        messages[current_user_index] = {"role": "user", "content": user_content}
        return {
            "model": model,
            "stream": False,
            "messages": messages,
        }

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
        if not any(isinstance(value, str) and value for value in (documents_index, documents_root, task_output)):
            return ""
        return (
            "NotePatch user data is mounted in the OpenClaw workspace.\n"
            f"- Documents index: {documents_index}\n"
            f"- Documents root: {documents_root}\n"
            f"- Write task outputs to: {task_output}\n"
            "Use only the files under these paths for this NotePatch task."
        )


def get_openclaw_runner() -> OpenClawRunner:
    return OpenClawGatewayRunner()

import base64
import json

import httpx
import pytest

from notepatch.platform.config import get_settings
from notepatch.modules.ai.services.gateway import OpenClawGatewayRunner, OpenClawRunnerError, get_openclaw_runner
from notepatch.platform.errors import TaskCancelledError


@pytest.fixture
def openclaw_settings(tmp_path):
    settings = get_settings()
    old = {
        "openclaw_workdir": settings.openclaw_workdir,
        "openclaw_gateway_base_url": settings.openclaw_gateway_base_url,
        "openclaw_gateway_token": settings.openclaw_gateway_token,
        "openclaw_gateway_model": settings.openclaw_gateway_model,
        "openclaw_agent_model": settings.openclaw_agent_model,
        "openclaw_gateway_timeout_seconds": settings.openclaw_gateway_timeout_seconds,
        "openclaw_skill_timeout_seconds": settings.openclaw_skill_timeout_seconds,
        "openclaw_gateway_ready_timeout_seconds": settings.openclaw_gateway_ready_timeout_seconds,
        "openclaw_gateway_ready_poll_seconds": settings.openclaw_gateway_ready_poll_seconds,
        "openclaw_gateway_scopes": settings.openclaw_gateway_scopes,
        "openclaw_user_runtime_root": settings.openclaw_user_runtime_root,
    }
    settings.openclaw_workdir = str(tmp_path)
    settings.openclaw_gateway_base_url = "http://openclaw.local"
    settings.openclaw_gateway_token = "test-token"
    settings.openclaw_gateway_model = "openclaw"
    settings.openclaw_agent_model = "openai/gpt-5.4"
    settings.openclaw_gateway_timeout_seconds = 5
    settings.openclaw_skill_timeout_seconds = 300
    settings.openclaw_gateway_ready_timeout_seconds = 0
    settings.openclaw_gateway_ready_poll_seconds = 0.01
    settings.openclaw_gateway_scopes = "operator.write"
    settings.openclaw_user_runtime_root = str(tmp_path / "users")
    try:
        yield settings
    finally:
        for key, value in old.items():
            setattr(settings, key, value)


def test_gateway_runner_posts_chat_completion_and_writes_output(openclaw_settings, tmp_path):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        captured["scopes"] = request.headers.get("x-openclaw-scopes")
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "choices": [{"message": {"role": "assistant", "content": "答案来了"}}],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    runner = OpenClawGatewayRunner(client=client)
    result = runner.run_task(
        "workspace-1",
        "task-1",
        {
            "prompt": "批改这份作业",
            "input": {"document_id": "doc-1", "score": 88},
            "options": {"model": "openclaw/main"},
        },
    )

    assert captured["url"] == "http://openclaw.local/v1/chat/completions"
    assert captured["authorization"] == "Bearer test-token"
    assert captured["scopes"] == "operator.write"
    assert captured["body"]["model"] == "openclaw/main"
    assert captured["body"]["stream"] is False
    assert "批改这份作业" in captured["body"]["messages"][0]["content"]
    assert '"document_id": "doc-1"' in captured["body"]["messages"][0]["content"]
    assert result["runner"] == "gateway"
    assert result["answer"] == "答案来了"

    output = tmp_path / "workspaces" / "workspace-1" / "tasks" / "task-1" / "output" / "result.json"
    assert json.loads(output.read_text(encoding="utf-8"))["answer"] == "答案来了"


def test_gateway_runner_streams_answer_and_reasoning_summary(openclaw_settings):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            content=(
                'data: {"choices":[{"delta":{"reasoning_content":"Plan: inspect the image."}}]}\n\n'
                'data: {"choices":[{"delta":{"content":"It is a waterfall."}}]}\n\n'
                "data: [DONE]\n\n"
            ),
            headers={"content-type": "text/event-stream"},
        )

    events: list[tuple[str, str]] = []
    runner = OpenClawGatewayRunner(client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = runner.run_chat_task(
        "workspace-1",
        "task-stream",
        {
            "prompt": "Describe the picture",
            "input": {},
            "options": {"thinking": {"enabled": True, "effort": "low"}, "temperature": 0.65},
            "ai_model": "openai/gpt-5.4",
        },
        on_stream_event=lambda stream, delta: events.append((stream, delta)),
    )

    assert captured["body"]["stream"] is True
    assert captured["body"]["reasoning_effort"] == "low"
    assert captured["body"]["reasoning_mode"] == "stream"
    assert captured["body"]["temperature"] == 0.65
    assert events == [
        ("reasoning", "Plan: inspect the image."),
        ("answer", "It is a waterfall."),
    ]
    assert result["answer"] == "It is a waterfall."
    assert result["reasoning_summary"] == "Plan: inspect the image."


def test_gateway_runner_surfaces_structured_stream_error(openclaw_settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=(
                'data: {"error":{"message":"Sandbox mode requires Docker, but the docker command was not found",'
                '"type":"api_error"}}\n\n'
                "data: [DONE]\n\n"
            ),
            headers={"content-type": "text/event-stream"},
        )

    runner = OpenClawGatewayRunner(client=httpx.Client(transport=httpx.MockTransport(handler)))

    with pytest.raises(OpenClawRunnerError, match="Sandbox mode requires Docker"):
        runner.run_chat_task(
            "workspace-1",
            "task-stream-error",
            {"prompt": "hello", "input": {}, "options": {}},
        )


def test_gateway_runner_stops_before_stream_when_cancelled(openclaw_settings):
    runner = OpenClawGatewayRunner(
        client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    )

    with pytest.raises(TaskCancelledError):
        runner.run_chat_task(
            "workspace-1",
            "task-cancelled",
            {"prompt": "hello", "input": {}, "options": {}},
            is_cancel_requested=lambda: True,
        )


def test_gateway_runner_can_use_user_runtime_context(openclaw_settings, tmp_path):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        captured["session_key"] = request.headers.get("x-openclaw-session-key")
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "user gateway ok"}}]},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    output_dir = tmp_path / "user-output"
    runner = OpenClawGatewayRunner(client=client)
    result = runner.run_task(
        "workspace-1",
        "task-1",
        {
            "prompt": "读取用户资料",
            "input": {},
            "options": {},
            "_openclaw": {
                "gateway_url": "http://notepatch-openclaw-user:18789",
                "gateway_token": "user-token",
                "gateway_container": "notepatch-openclaw-user",
                "user_workspace_dir": str(tmp_path / "workspace"),
                "documents_index_path": "/workspace/notepatch/documents/index.json",
                "documents_root_path": "/workspace/notepatch/documents",
                "task_output_path": "/workspace/notepatch/openclaw/tasks/task-1/output",
                "host_task_output_dir": str(output_dir),
                "session_key": "notepatch:workspace-1:task-1",
            },
        },
    )

    assert captured["url"] == "http://notepatch-openclaw-user:18789/v1/chat/completions"
    assert captured["authorization"] == "Bearer user-token"
    assert captured["session_key"] == "notepatch:workspace-1:task-1"
    assert "/workspace/notepatch/documents/index.json" in captured["body"]["messages"][0]["content"]
    assert result["gateway_container"] == "notepatch-openclaw-user"
    assert json.loads((output_dir / "result.json").read_text(encoding="utf-8"))["answer"] == "user gateway ok"


def test_gateway_runner_uses_conversation_messages_and_augments_only_current_prompt(openclaw_settings):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"choices": [{"message": {"content": "继续回答"}}]})

    runner = OpenClawGatewayRunner(client=httpx.Client(transport=httpx.MockTransport(handler)))
    runner.run_task(
        "workspace-1",
        "task-1",
        {
            "prompt": "接着讲",
            "input": {"subject": "math"},
            "options": {},
            "conversation_messages": [
                {"role": "user", "content": "什么是二次函数？"},
                {"role": "assistant", "content": "它是次数为二的函数。"},
                {"role": "user", "content": "接着讲"},
            ],
        },
    )

    messages = captured["body"]["messages"]
    assert messages[:2] == [
        {"role": "user", "content": "什么是二次函数？"},
        {"role": "assistant", "content": "它是次数为二的函数。"},
    ]
    assert messages[2]["role"] == "user"
    assert messages[2]["content"].startswith("接着讲")
    assert '"subject": "math"' in messages[2]["content"]


def test_gateway_runner_ignores_provider_model_for_gateway_request(openclaw_settings):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        captured["provider_model"] = request.headers.get("x-openclaw-model")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    runner = OpenClawGatewayRunner(client=client)
    result = runner.run_task(
        "workspace-1",
        "task-1",
        {
            "prompt": "hello",
            "input": {},
            "options": {"model": "openai/ignored-provider-model"},
            "ai_model": "openai/gpt-5.4",
        },
    )

    assert captured["body"]["model"] == "openclaw"
    assert captured["provider_model"] == "openai/gpt-5.4"
    assert result["model"] == "openclaw"
    assert result["provider_model"] == "openai/gpt-5.4"


def test_gateway_runner_generates_title_in_isolated_session(openclaw_settings):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["session_key"] = request.headers.get("x-openclaw-session-key")
        captured["provider_model"] = request.headers.get("x-openclaw-model")
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"choices": [{"message": {"content": "二次函数复习"}}]})

    runner = OpenClawGatewayRunner(client=httpx.Client(transport=httpx.MockTransport(handler)))
    title = runner.generate_conversation_title(
        "workspace-1",
        "conversation-1",
        [
            {"role": "user", "content": "请解释二次函数"},
            {"role": "assistant", "content": "二次函数的一般形式是……"},
        ],
        runtime={"gateway_url": "http://user-gateway:18789", "gateway_token": "user-token"},
        provider_model="openai/gpt-5.4-mini",
        client_locale="pt-BR",
        max_length=40,
        timeout_seconds=12,
    )

    assert title == "二次函数复习"
    assert captured["url"] == "http://user-gateway:18789/v1/chat/completions"
    assert captured["session_key"] == "notepatch:workspace-1:chat-title:conversation-1"
    assert captured["provider_model"] == "openai/gpt-5.4-mini"
    assert captured["body"]["model"] == "openclaw"
    assert captured["body"]["reasoning_effort"] == "none"
    assert captured["body"]["messages"][0]["role"] == "system"
    assert "client locale pt-BR" in captured["body"]["messages"][0]["content"]
    assert "using only user-role messages" in captured["body"]["messages"][0]["content"]
    assert "not a request to continue or answer" in captured["body"]["messages"][1]["content"]
    assert "请解释二次函数" in captured["body"]["messages"][1]["content"]



def test_gateway_runner_honors_per_task_timeout(openclaw_settings):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["timeout"] = request.extensions["timeout"]["read"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    runner = OpenClawGatewayRunner(client=httpx.Client(transport=httpx.MockTransport(handler)))
    runner.run_task(
        "workspace-1",
        "task-1",
        {
            "prompt": "generate notes",
            "input": {},
            "options": {},
            "_openclaw": {"timeout_seconds": 300},
        },
    )

    assert captured["timeout"] == 300.0

def test_gateway_runner_raises_readable_error_for_http_failure(openclaw_settings):
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(401, text="bad token"))
    )
    runner = OpenClawGatewayRunner(client=client)

    with pytest.raises(OpenClawRunnerError, match="HTTP 401: bad token"):
        runner.run_task("workspace-1", "task-1", {"prompt": "hello", "input": {}, "options": {}})


def test_gateway_runner_raises_readable_error_for_timeout(openclaw_settings):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    runner = OpenClawGatewayRunner(client=client)

    with pytest.raises(OpenClawRunnerError, match="timed out"):
        runner.run_task("workspace-1", "task-1", {"prompt": "hello", "input": {}, "options": {}})


def test_gateway_runner_treats_rate_limit_text_as_retryable_error(openclaw_settings):
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": "API rate limit reached. Please try again later."}}
                    ]
                },
            )
        )
    )
    runner = OpenClawGatewayRunner(client=client)

    with pytest.raises(OpenClawRunnerError, match="OpenClaw provider error: API rate limit reached"):
        runner.run_task("workspace-1", "task-1", {"prompt": "hello", "input": {}, "options": {}})


def test_gateway_runner_waits_for_gateway_health(openclaw_settings):
    openclaw_settings.openclaw_gateway_ready_timeout_seconds = 1
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path == "/healthz":
            if calls.count("GET /healthz") == 1:
                return httpx.Response(503, text="starting")
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ready"}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    runner = OpenClawGatewayRunner(client=client)
    result = runner.run_task("workspace-1", "task-1", {"prompt": "hello", "input": {}, "options": {}})

    assert result["answer"] == "ready"
    assert calls[:3] == ["GET /healthz", "GET /healthz", "POST /v1/chat/completions"]


def test_get_openclaw_runner_is_always_gateway(openclaw_settings):
    assert isinstance(get_openclaw_runner(), OpenClawGatewayRunner)


def test_gateway_runner_sends_current_image_attachment_as_multimodal_input(
    openclaw_settings, tmp_path
):
    captured: dict = {}
    image_bytes = b"not-a-real-jpeg-but-stable-test-bytes"
    host_input_dir = tmp_path / "task-input"
    image_path = host_input_dir / "documents" / "doc-1" / "original" / "photo.jpg"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(image_bytes)

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"choices": [{"message": {"content": "a waterfall"}}]})

    runner = OpenClawGatewayRunner(client=httpx.Client(transport=httpx.MockTransport(handler)))
    runner.run_task(
        "workspace-1",
        "task-1",
        {
            "prompt": "What is in this image?",
            "input": {
                "attachments": [
                    {
                        "document_id": "doc-1",
                        "file_type": "image",
                        "mime_type": "image/jpeg",
                        "original_path": (
                            "/workspace/notepatch/openclaw/tasks/task-1/input/"
                            "documents/doc-1/original/photo.jpg"
                        ),
                    }
                ]
            },
            "_openclaw": {
                "host_task_input_dir": str(host_input_dir),
                "documents_root_path": (
                    "/workspace/notepatch/openclaw/tasks/task-1/input/documents"
                ),
            },
        },
    )

    content = captured["body"]["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert "What is in this image?" in content[0]["text"]
    image_url = content[1]["image_url"]["url"]
    prefix = "data:image/jpeg;base64,"
    assert image_url.startswith(prefix)
    assert base64.b64decode(image_url.removeprefix(prefix)) == image_bytes

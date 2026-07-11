# Frontend Integration Guide

本文档说明前端如何接入 NotePatch 文件管理 MVP。当前上传方案是 FastAPI + tusd + SeaweedFS：业务接口走 FastAPI，大文件内容走 tusd，文件最终由后端 webhook 搬到 SeaweedFS S3。

## Base URL

```bash
VITE_API_BASE_URL=http://192.168.100.123:8001/api/v1
VITE_TUSD_BASE_URL=http://192.168.100.123:1080/files/
```

本机开发可用：

```bash
VITE_API_BASE_URL=http://localhost:8001/api/v1
VITE_TUSD_BASE_URL=http://localhost:1080/files/
```

前端只需要配置 FastAPI 和 tusd 两个地址。不要配置或调用 `docserver`、SeaweedFS S3、OpenClaw Gateway；这些都是后端/worker 内部依赖，直接暴露给浏览器会绕过 NotePatch 的权限校验。

后端不保留未版本化兼容路由。Android/Web 必须把所有 auth、presence、workspace、document、task、learning 和 AI 请求拼到 `VITE_API_BASE_URL`（即 `/api/v1`）之后；例如心跳是 `/api/v1/presence/heartbeat`，旧 `/presence/heartbeat` 会返回 `404`。

## Backend Snapshot

当前后端是 personal workspace-only 架构。前端只面向“当前登录用户的个人空间”，不要展示 family/class/school、成员邀请或角色管理入口。

```text
Frontend / Android
  -> FastAPI API: auth, workspace, document metadata, tasks, artifacts, AI
  -> tusd: resumable file upload only

Backend internal
  -> PostgreSQL: metadata, task status, workspace isolation
  -> SeaweedFS S3: original files and artifacts
  -> Redis: task queues and online presence
  -> worker(default): OpenClaw、grading、knowledge、notes 与 purge tasks
  -> ocr-worker(ocr profile): document_processing_pipeline、ocr_document 与真实 PaddleOCR
  -> docserver: DocTr image rectification, internal only
  -> OpenClaw per-user gateway: internal only
```

前端需要知道的边界：

- 文件上传走 tusd；文件下载必须先向 FastAPI 请求短期 `download-url`。
- OCR、DocTr、OpenClaw、SeaweedFS、Redis queue 都是后端内部实现，前端不直接调用。
- 当前公开触发入口仍是 `POST /documents/{document_id}/process`；没有公开 `POST /ocr`。
- `ocr-worker` 是否启动不改变前端 API。真实 PaddleOCR 上线后，前端仍只轮询 task/events 和读取 `/ocr`。

## Web Admin Frontend

仓库内置运维管理后台在 `web/admin/`，是独立 Vite React 应用。它面向运维管理员，不是普通用户 Web 端；移动端和用户端不应调用 `/admin/*`。

启动：

```bash
ADMIN_EMAILS=ops@example.com
docker compose up -d --build api admin-web
```

访问：

```text
http://localhost:5173
```

管理后台使用 `${VITE_API_BASE_URL}/auth/login`，然后调用 `${VITE_API_BASE_URL}/admin/me` 校验登录邮箱是否在后端 `.env` 的 `ADMIN_EMAILS` 白名单中。`ADMIN_EMAILS` 为空时，后台 API 默认禁用并返回 403。

后台接口只读优先：

```http
GET /admin/overview
GET /admin/users
GET /admin/users/{user_id}
GET /admin/documents
GET /admin/documents/{document_id}
GET /admin/documents/{document_id}/artifacts
GET /admin/documents/{document_id}/download-url
GET /admin/artifacts/{artifact_id}/download-url
GET /admin/tasks
GET /admin/tasks/{task_id}
GET /admin/tasks/{task_id}/events
GET /admin/queues
GET /admin/services
```

后台可跨 personal workspace 做排障查询，但不会改变用户端 workspace 隔离规则。第一版不提供删除用户、删除文件、重跑 OCR 或任务重试等破坏性操作。

## Recommended App Flow

```text
register/login
  -> heartbeat
  -> GET /workspaces
  -> create upload-session
  -> tus upload
  -> complete-upload
  -> POST /documents/{id}/process
  -> poll task
  -> poll task events
  -> GET /documents/{id}/ocr
  -> GET artifact download-url
```

## Auth Client

```ts
const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8001/api/v1";

const tokenState = {
  accessToken: localStorage.getItem("access_token"),
  refreshToken: localStorage.getItem("refresh_token"),
};

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (!headers.has("Content-Type") && init.body) headers.set("Content-Type", "application/json");
  if (tokenState.accessToken) headers.set("Authorization", `Bearer ${tokenState.accessToken}`);

  let res = await fetch(`${API_BASE_URL}${path}`, { ...init, headers });

  if (res.status === 401 && tokenState.refreshToken) {
    const refreshed = await fetch(`${API_BASE_URL}/auth/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: tokenState.refreshToken }),
    });
    if (refreshed.ok) {
      const data = await refreshed.json();
      tokenState.accessToken = data.access_token;
      tokenState.refreshToken = data.refresh_token;
      localStorage.setItem("access_token", data.access_token);
      localStorage.setItem("refresh_token", data.refresh_token);
      headers.set("Authorization", `Bearer ${data.access_token}`);
      res = await fetch(`${API_BASE_URL}${path}`, { ...init, headers });
    }
  }

  if (!res.ok) {
    const error = await res.json().catch(() => ({}));
    throw new Error(error.detail ?? `Request failed: ${res.status}`);
  }
  return res.json();
}
```

## Auth APIs

```http
POST /auth/register
POST /auth/login
POST /auth/refresh
POST /auth/logout
GET  /auth/me
POST /presence/heartbeat
POST /presence/offline
```

登录/注册返回 `access_token` 和 `refresh_token`。业务接口都带：

```http
Authorization: Bearer <access_token>
```

登录或注册成功后立即发送在线心跳，并把返回的 `client_id` 存到 localStorage。后续每 `heartbeat_interval_seconds` 秒续一次；页面关闭、主动登出时尽力调用 `offline`。

```ts
const CLIENT_ID_KEY = "notepatch_client_id";

async function heartbeat() {
  const clientId = localStorage.getItem(CLIENT_ID_KEY);
  const data = await apiFetch<{
    client_id: string;
    online_until: string;
    heartbeat_interval_seconds: number;
  }>("/presence/heartbeat", {
    method: "POST",
    body: JSON.stringify({ client_id: clientId }),
  });
  localStorage.setItem(CLIENT_ID_KEY, data.client_id);
  return data;
}

async function goOffline() {
  const clientId = localStorage.getItem(CLIENT_ID_KEY);
  if (!clientId) return;
  await apiFetch("/presence/offline", {
    method: "POST",
    body: JSON.stringify({ client_id: clientId }),
  });
}
```

只要用户任一客户端持续 heartbeat，后端 `openclaw-supervisor` 会保持该用户 OpenClaw gateway 容器运行。停止心跳超过 10 分钟后，容器会被停止；running 的 OpenClaw task 不会被中断。

## Workspace APIs

```http
GET  /workspaces
POST /workspaces
GET  /workspaces/{workspace_id}
POST /workspaces/{workspace_id}/members
```

注册/登录后调用 `GET /workspaces` 获取当前用户的个人 workspace。当前版本只支持一个 `personal` workspace，不支持 family/class/school，也不支持邀请成员；`POST /workspaces/{workspace_id}/members` 会返回 `410`。`POST /workspaces` 只作为异常恢复接口，用户已有个人 workspace 时会返回 `409`。

```ts
type Workspace = {
  id: string;
  name: string;
  type: "personal";
  owner_user_id: string;
};

async function getPersonalWorkspace() {
  const workspaces = await apiFetch<Workspace[]>("/workspaces");
  if (workspaces.length === 0) {
    return apiFetch<Workspace>("/workspaces", {
      method: "POST",
      body: JSON.stringify({ name: "My Workspace" }),
    });
  }
  return workspaces[0];
}
```

前端建议把这个 `workspace_id` 存在全局状态中，所有业务列表都以 workspace 为入口。

## Create Upload Session

```ts
type UploadSessionResponse = {
  document: {
    id: string;
    workspace_id: string;
    status: "created" | "uploading" | "uploaded" | "processing" | "ready" | "failed" | "deleted";
    original_filename: string;
    mime_type: string | null;
    file_size: number | null;
    file_type: "image" | "pdf" | "docx" | "pptx" | "audio" | "video" | "other";
    document_kind: "homework" | "corrected_homework" | "courseware" | "note" | "exam" | "other";
    bucket: string;
    object_key: string;
  };
  upload_session: {
    id: string;
    status: "created" | "uploading" | "completed" | "failed" | "cancelled";
  };
  tus_endpoint: string;
  tus_metadata: Record<string, string>;
  tus_metadata_header: string;
  bucket: string;
  object_key: string;
};

async function createUploadSession(workspaceId: string, file: File, documentKind = "other") {
  return apiFetch<UploadSessionResponse>(`/workspaces/${workspaceId}/documents/upload-session`, {
    method: "POST",
    body: JSON.stringify({
      filename: file.name,
      mime_type: file.type || "application/octet-stream",
      file_size: file.size,
      document_kind: documentKind,
      title: file.name,
      metadata: {},
    }),
  });
}
```

## Upload With tus-js-client

安装：

```bash
npm install tus-js-client
```

示例：

```ts
import * as tus from "tus-js-client";

async function uploadDocument(workspaceId: string, file: File) {
  const session = await createUploadSession(workspaceId, file, "homework");

  return new Promise<UploadSessionResponse>((resolve, reject) => {
    const upload = new tus.Upload(file, {
      endpoint: session.tus_endpoint,
      metadata: session.tus_metadata,
      retryDelays: [0, 1000, 3000, 5000],
      onError: reject,
      onProgress(bytesUploaded, bytesTotal) {
        const progress = Math.round((bytesUploaded / bytesTotal) * 100);
        console.log("upload progress", progress);
      },
      async onSuccess() {
        await apiFetch(`/workspaces/${workspaceId}/documents/complete-upload`, {
          method: "POST",
          body: JSON.stringify({
            upload_session_id: session.upload_session.id,
            tus_upload_url: upload.url,
            tus_upload_id: upload.url?.split("/").pop(),
            file_size: file.size,
            mime_type: file.type || "application/octet-stream",
          }),
        });
        resolve(session);
      },
    });

    upload.start();
  });
}
```

tusd 也会通过 webhook 自动完成上传；前端主动调用 `complete-upload` 是兜底同步，接口是幂等的。

上传成功后，建议重新读取 document 详情或列表。`complete-upload` 返回最新 `Document`，状态通常为 `uploaded`；随后才调用 `process` 进入异步处理。

## Documents

```http
GET    /workspaces/{workspace_id}/documents?page=1&page_size=20&status=uploaded&document_kind=homework&file_type=image
GET    /workspaces/{workspace_id}/documents/{document_id}
DELETE /workspaces/{workspace_id}/documents/{document_id}
GET    /workspaces/{workspace_id}/documents/{document_id}/download-url
```

下载时不要直接拼 object key；必须请求 `download-url`，后端会先做 workspace 权限校验再返回短期 URL。

删除返回 `202 Accepted`，不是 `204`：

```ts
type DocumentDeleteResponse = {
  ok: true;
  document_id: string;
  status: "deleted";
  purge_status: "queued" | "running" | "succeeded" | "failed";
  purge_task_id: string;
};

async function deleteDocument(workspaceId: string, documentId: string) {
  return apiFetch<DocumentDeleteResponse>(
    `/workspaces/${workspaceId}/documents/${documentId}`,
    { method: "DELETE" },
  );
}
```

收到响应后立即从本地列表移除文档，并轮询 `purge_task_id`。重复 DELETE 会返回同一个活动/已完成 purge task；若之前清理失败，后端会创建新的 purge task。清理固定删除原件与派生数据，不存在“只删 metadata”模式。

## Artifacts

```http
POST /workspaces/{workspace_id}/documents/{document_id}/artifacts
GET  /workspaces/{workspace_id}/documents/{document_id}/artifacts
GET  /workspaces/{workspace_id}/documents/{document_id}/artifacts/{artifact_id}/download-url
GET  /workspaces/{workspace_id}/documents/{document_id}/ocr?include_download_url=true
```

artifact `object_key` 必须在：

```text
workspaces/{workspace_id}/documents/{document_id}/artifacts/
```

普通前端通常只读 artifacts，不直接创建 artifacts。`POST artifacts` 是给后续 OCR/预处理 worker 或受信任后台工具写 metadata 用的。
创建 metadata 前对象必须已经存在于当前 SeaweedFS bucket；跨 bucket、跨 document 前缀或悬空 object key 会被拒绝。

常见 artifact 类型：

```ts
type ArtifactType =
  | "original"
  | "deskewed_image"
  | "ocr_json"
  | "ocr_markdown"
  | "ocr_text"
  | "questions_json"
  | "grading_report"
  | "summary"
  | "flashcards"
  | "other";

type DocumentArtifact = {
  id: string;
  workspace_id: string;
  document_id: string;
  artifact_type: ArtifactType | string;
  bucket: string;
  object_key: string;
  mime_type: string | null;
  file_size: number | null;
  metadata: Record<string, unknown>;
  created_at: string;
};
```

不要直接用 `object_key` 拼下载地址。用户下载原始 document 时走 document `download-url`；下载 artifact 时走 artifact `download-url`。后端会先校验 personal workspace、document 和 artifact 三者的归属，再返回短期 URL。

```ts
type ArtifactDownloadUrl = {
  artifact_id: string;
  document_id: string;
  artifact_type: string;
  filename: string;
  mime_type: string | null;
  expires_in: number;
  download_url: string;
};

async function getArtifactDownloadUrl(workspaceId: string, documentId: string, artifactId: string) {
  return apiFetch<ArtifactDownloadUrl>(
    `/workspaces/${workspaceId}/documents/${documentId}/artifacts/${artifactId}/download-url`,
  );
}

type OcrArtifact = {
  id: string;
  artifact_type: "ocr_json" | "ocr_markdown" | "ocr_text" | string;
  mime_type: string | null;
  file_size: number | null;
  created_at: string;
  download_url: string | null;
};

async function getOcrArtifacts(workspaceId: string, documentId: string, includeDownloadUrl = false) {
  return apiFetch<{ document_id: string; artifacts: OcrArtifact[] }>(
    `/workspaces/${workspaceId}/documents/${documentId}/ocr?include_download_url=${includeDownloadUrl}`,
  );
}
```

## Task Polling

```http
POST /workspaces/{workspace_id}/documents/{document_id}/process
POST /workspaces/{workspace_id}/ai/chat
GET  /workspaces/{workspace_id}/tasks/{task_id}
GET  /workspaces/{workspace_id}/tasks/{task_id}/events
```

当前任务进度用轮询。后续可以加 SSE/WebSocket。

```ts
type TaskStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";

type Task = {
  id: string;
  workspace_id: string;
  task_type: string;
  status: TaskStatus;
  resource_type: string | null;
  resource_id: string | null;
  payload: Record<string, unknown>;
  result: Record<string, unknown> | null;
  error_message: string | null;
  progress: number;
  cancel_requested_at: string | null;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
};

type TaskEvent = {
  id: string;
  workspace_id: string;
  task_id: string;
  event_type: string;
  level: "info" | "warning" | "error" | string;
  message: string;
  progress: number | null;
  data: Record<string, unknown>;
  created_at: string;
};

async function processDocument(workspaceId: string, documentId: string, forceReprocess = false) {
  return apiFetch<Task>(`/workspaces/${workspaceId}/documents/${documentId}/process`, {
    method: "POST",
    body: JSON.stringify({ options: { force_reprocess: forceReprocess } }),
  });
}

async function waitForTask(workspaceId: string, taskId: string) {
  for (;;) {
    const task = await apiFetch<Task>(`/workspaces/${workspaceId}/tasks/${taskId}`);
    if (["succeeded", "failed", "cancelled"].includes(task.status)) return task;
    await new Promise((resolve) => setTimeout(resolve, 1200));
  }
}
```

只有 `uploaded/ready/failed` 且 SeaweedFS 原对象存在的文档可触发处理；`created/uploading` 返回 `409`。同文档已有 queued/running 任务且未 force 时会返回原 task；force 与活动任务冲突时返回 `409`。

## AI Chat History

`POST /workspaces/{workspace_id}/ai/chat` 会自动创建或继续一条持久化对话。请求不传 `conversation_id` 时后端创建新会话；响应 task 的 `payload.conversation_id`、`user_message_id` 和 `assistant_message_id` 用于刷新当前会话。assistant message 会先以 `queued` 出现，worker 处理期间变为 `running`，最终为 `succeeded` 或 `failed`。

```ts
type ChatConversation = {
  id: string;
  workspace_id: string;
  title: string;
  last_message_at: string | null;
  created_at: string;
  updated_at: string;
};

type ChatMessage = {
  id: string;
  conversation_id: string;
  role: "user" | "assistant";
  content: string;
  task_id: string | null;
  status: "queued" | "running" | "succeeded" | "failed";
  error_message: string | null;
  created_at: string;
};

async function sendChat(workspaceId: string, prompt: string, conversationId?: string) {
  return apiFetch<Task>(`/workspaces/${workspaceId}/ai/chat`, {
    method: "POST",
    body: JSON.stringify({ prompt, conversation_id: conversationId, input: {}, options: {} }),
  });
}

async function listConversations(workspaceId: string) {
  return apiFetch<{ items: ChatConversation[]; page: number; page_size: number; total: number }>(
    `/workspaces/${workspaceId}/ai/conversations?page=1&page_size=20`,
  );
}

async function listChatMessages(workspaceId: string, conversationId: string) {
  return apiFetch<{ items: ChatMessage[]; page: number; page_size: number; total: number }>(
    `/workspaces/${workspaceId}/ai/conversations/${conversationId}/messages`,
  );
}
```

可用接口：

```http
POST   /workspaces/{workspace_id}/ai/chat
GET    /workspaces/{workspace_id}/ai/conversations
GET    /workspaces/{workspace_id}/ai/conversations/{conversation_id}
GET    /workspaces/{workspace_id}/ai/conversations/{conversation_id}/messages
PATCH  /workspaces/{workspace_id}/ai/conversations/{conversation_id}
DELETE /workspaces/{workspace_id}/ai/conversations/{conversation_id}
PATCH  /auth/preferences
```

`GET /auth/me`、登录和 refresh 响应都会给出 `user.ai_history_enabled`。前端用 `PATCH /auth/preferences` 传 `{ "ai_history_enabled": false }` 关闭全局上下文注入；关闭后历史仍保留并可查看，但后续 OpenClaw 调用只发送当前 prompt。重新开启后，后端会自动传入该会话最近 `AI_CHAT_HISTORY_MESSAGE_LIMIT`（默认 20）条成功消息。删除会话是软删除，删除后不可继续发送或读取，关联的 queued/running OpenClaw task 会被协作取消。

任务成功后再拉取 artifacts：

```ts
async function getDocumentArtifacts(workspaceId: string, documentId: string) {
  return apiFetch<DocumentArtifact[]>(`/workspaces/${workspaceId}/documents/${documentId}/artifacts`);
}
```

如果只关心 OCR 结果，优先调用 `/ocr`，它会返回最新一组完整 `ocr_json`、`ocr_markdown`、`ocr_text` metadata；`include_download_url=true` 时可直接拿到下载地址。

## Automatic Learning Workflow

后端现在可以开启自动学习流水线：`AUTO_LEARNING_PIPELINE=true`。前端上传完成后不需要额外编排 OpenClaw、OCR、知识库或批改服务，只需要跟随 task 状态和读取结果 API。

资料类文档推荐设置：

```ts
type DocumentKind = "courseware" | "note" | "exam" | "homework" | "corrected_homework" | "other";

type LearningMetadata = {
  learning_unit_id?: string;
  learning_unit_title?: string;
  subject?: string;
  grade_level?: string;
  topic?: string;
};
```

上传课件/笔记/试卷后，后端流程是：

```text
complete-upload
  -> document_processing_pipeline
  -> OCR artifacts
  -> build_knowledge_base
  -> generate_study_notes
```

上传作业后，如果 metadata 带 `learning_unit_id`，后端会把作业挂到已有学习单元；否则会自动创建/归类学习单元：

```text
complete-upload
  -> document_processing_pipeline
  -> OCR artifacts
  -> grade_homework
  -> mistakes + mistake knowledge chunks
  -> highlight_study_notes
```

前端查询学习结果：

```ts
type LearningUnit = {
  id: string;
  title: string;
  subject: string | null;
  grade_level: string | null;
  topic: string | null;
};

type StudyNoteVersion = {
  id: string;
  learning_unit_id: string;
  version_no: number;
  title: string;
  markdown_object_key: string;
  json_object_key: string;
  highlighted_object_key: string | null;
  highlight_map_object_key: string | null;
  download_urls?: Record<string, string>;
};

async function listLearningUnits(workspaceId: string) {
  return apiFetch<LearningUnit[]>(`/workspaces/${workspaceId}/learning-units`);
}

async function listStudyNotes(workspaceId: string, learningUnitId: string) {
  return apiFetch<StudyNoteVersion[]>(
    `/workspaces/${workspaceId}/learning-units/${learningUnitId}/notes?include_download_url=true`,
  );
}
```

可用接口：

```http
GET /workspaces/{workspace_id}/learning-units
GET /workspaces/{workspace_id}/learning-units/{learning_unit_id}?include_download_url=true
GET /workspaces/{workspace_id}/learning-units/{learning_unit_id}/knowledge-chunks
GET /workspaces/{workspace_id}/learning-units/{learning_unit_id}/notes?include_download_url=true
GET /workspaces/{workspace_id}/learning-units/{learning_unit_id}/notes/{note_version_id}/download-url?kind=highlighted
```

UI 建议：

- 文档处理 task 成功后，刷新 learning units 和 notes。
- `grade_homework` 成功后，刷新 mistakes、knowledge chunks 和 latest note。
- 如果 latest note 有 `highlighted` 下载 URL，优先展示高亮版；否则展示普通 markdown。
- 前端不要直接调用 OpenClaw skill；当前 skill 执行和后续替换都由后端 worker 管理。

后端内部区分 `default` 和 `ocr` 两个 worker queue：`process` 创建的 `document_processing_pipeline` 与独立 `ocr_document` 都进入 `ocr` queue，其他 AI/学习任务进入 `default` queue。这个拆分不改变前端 API；前端仍只调用 `process`、轮询 task/events、读取 `/ocr` 和 artifact download-url。真实 PaddleOCR worker 由部署侧通过 `docker compose --profile ocr up -d --build ocr-worker` 启动，前端不需要也不能选择 worker。

`POST /workspaces/{workspace_id}/ai/chat` 是唯一的 AI 对话入口。它创建后端异步 OpenClaw 任务，前端不直接调用 OpenClaw Gateway，也不要启动/停止容器。请求体使用 `{ "prompt": string, "conversation_id"?: string, "input": object, "options": object }`，响应是 `TaskRead`；随后轮询 task 与 events 获取 `task.result.answer` 或失败原因。会话历史由后端保存，是否注入 OpenClaw 由用户全局 `ai_history_enabled` 控制。后端会为每个用户维护独立 OpenClaw gateway 配置和用户数据目录；用户在线时 supervisor 保持 gateway 运行，worker 在任务前把该用户 personal workspace 的文档镜像到 OpenClaw workspace，再把 OpenClaw 输出上传回 SeaweedFS。

OpenClaw 模型凭据和 provider base URL 由后端部署级 `.env` 管理。当前 MVP 使用共享 `OPENAI_API_KEY` 注入每个用户 gateway 容器，`OPENAI_BASE_URL` 可由后端配置为 OpenAI-compatible 代理地址；前端不提交、不保存、不展示模型 provider key 或 base URL。若后端未配置 key，task 会失败并在 `task.error_message` 中出现 `OPENAI_API_KEY` 配置提示；若 gateway 返回 HTTP 500，前端只展示失败原因，排查应看 task events 和后端 OpenClaw gateway/worker 日志。

任务成功后 `task.result` 常见字段：

```ts
type OpenClawTaskResult = {
  runner: "gateway";
  answer?: string;
  output_key?: string;
  output_keys?: string[];
  citations?: Array<{
    chunk_id: string;
    document_id?: string;
    score: number;
    metadata: Record<string, unknown>;
  }>;
  gateway_container?: string;
  user_workspace_dir?: string;
};
```

前端只展示 `answer`、任务状态和必要的 output metadata。不要展示或依赖 `user_workspace_dir` 的本机路径，也不要直接访问 `gateway_container`；这些字段主要用于后端排查。失败时读取 `task.error_message` 和 events 展示原因。

图片文档调用 `process` 后，worker 会先通过内网 DocTr 无状态推理服务生成 `deskewed_image.png` artifact，然后优先用该图片做 OCR；如果 DocTr 失败，后端会写 warning event 并 fallback 到原图。PDF 会在 worker 中渲染后 OCR；DOCX/PPTX 第一版会提示需先转换为 PDF 或图片。前端只需要轮询 task/events，不需要直接调用 DocTr、OCR engine、SeaweedFS 或 OpenClaw。

图片处理成功时，events 通常包含：

```text
queued -> running -> gpu_lease_waiting -> gpu_lease_acquired -> doctr_running -> gpu_lease_released -> ocr_started -> ocr_rendered -> ocr_page_completed -> ocr_artifacts_uploaded -> succeeded
```

其中 `doctr_*` 只表示后端图片矫正阶段，不代表 OCR 完成。最终可在 artifacts 中看到：

```text
original          image/jpeg 或 image/png
deskewed_image    image/png, metadata.processor = "doctr"
ocr_json          application/json, 稳定结构化 OCR 输出
ocr_markdown      text/markdown, OCR Markdown
ocr_text          text/plain, OCR 纯文本
layout_json       application/json, PP-StructureV3 版面结果
formula_json      application/json, PP-FormulaNet 公式结果
tables_json       application/json, PP-StructureV3 表格结果
questions_json    application/json, OpenClaw question extractor 的结构化题目
```

如果任务失败，优先展示 `task.error_message`，并可拉取 `/events` 展示最后一个 `level="error"` 或最近的进度事件。

## Knowledge Search And Grading References

以下接口都需要 `Authorization: Bearer <access_token>`，并严格限制在当前 personal workspace。权威 OpenAPI 可从 `GET /openapi.json` 获取；局域网开发环境为 `http://192.168.100.123:8001/api/v1/openapi.json`。

### Knowledge Search

知识库检索由后端调用 BGE-M3：

```http
POST /workspaces/{workspace_id}/knowledge/search
Content-Type: application/json

{
  "query": "一次函数斜率是什么意思？",
  "learning_unit_id": "optional-learning-unit-id",
  "subject": "math",
  "limit": 6
}
```

`learning_unit_id`、`subject` 可省略或传 `null`；`limit` 默认为 `6`，范围为 `1..20`。成功返回 `200`：

```json
{
  "items": [
    {
      "id": "f69fc9bf-08c0-4e95-a9f7-83091635d05e",
      "workspace_id": "b3f4f628-b0e8-4737-b6b1-35480e6d5e1b",
      "document_id": "f31a8f57-4727-4b21-ac84-ee7f3dae5fc8",
      "subject": "math",
      "grade_level": "IGCSE",
      "source_type": "courseware",
      "content": "一次函数的斜率表示因变量相对于自变量的变化率。",
      "metadata": {
        "learning_unit_id": "linear-functions",
        "page_refs": [2],
        "title": "一次函数的斜率"
      },
      "score": 0.8731,
      "created_at": "2026-07-10T07:10:00Z"
    }
  ]
}
```

无命中时返回 `{"items":[]}`。`score` 是 pgvector cosine similarity，越大越相关；不要直接格式化成准确率百分比。`document_id/subject/grade_level/source_type` 均可能为 `null`，`metadata` 是开放 JSON object。

### Grading Config

```http
PATCH /workspaces/{workspace_id}/homeworks/{homework_id}/grading-config
Content-Type: application/json

{
  "rubric_text": "每题 10 分；计算过程 4 分，答案 6 分。",
  "max_score": 100.0
}
```

成功返回 `200 HomeworkRead`：

```json
{
  "id": "2cb41530-904b-4242-b898-5224876f3935",
  "workspace_id": "b3f4f628-b0e8-4737-b6b1-35480e6d5e1b",
  "title": "代数作业 01",
  "description": "一次函数练习",
  "document_id": "f31a8f57-4727-4b21-ac84-ee7f3dae5fc8",
  "due_at": "2026-07-15T12:00:00Z",
  "status": "draft",
  "rubric_text": "每题 10 分；计算过程 4 分，答案 6 分。",
  "max_score": 100.0,
  "metadata": {},
  "created_by_user_id": "582d4cfc-57be-4fba-8370-d4283df4365c",
  "created_at": "2026-07-10T07:00:00Z",
  "updated_at": "2026-07-10T07:12:00Z"
}
```

`description/document_id/due_at/rubric_text` 可能为 `null`。该接口是真正的部分更新：省略字段会保持原值；显式传 `rubric_text: null` 或空白字符串才会清除 rubric；空对象返回 `422`；`max_score` 必须大于 `0`。更新配置会取消尚未完成的评分 task，前端应重新触发评分。

### Homework References

reference 文档必须先作为普通 Document 上传并处理，其 `document_kind` 必须与 `reference_type` 一致：`answer_key` 或 `rubric`。

新增 reference：

```http
POST /workspaces/{workspace_id}/homeworks/{homework_id}/references
Content-Type: application/json

{
  "document_id": "6498d617-771d-416e-817e-8a69ea720e10",
  "reference_type": "answer_key"
}
```

成功返回 `201 HomeworkReferenceRead`：

```json
{
  "id": "cae45a30-63ca-41a1-ac14-2378a685d0de",
  "workspace_id": "b3f4f628-b0e8-4737-b6b1-35480e6d5e1b",
  "homework_id": "2cb41530-904b-4242-b898-5224876f3935",
  "document_id": "6498d617-771d-416e-817e-8a69ea720e10",
  "reference_type": "answer_key",
  "created_at": "2026-07-10T07:15:00Z"
}
```

列表接口成功返回 `200`，响应体直接是数组，不带 `items` 包装：

```http
GET /workspaces/{workspace_id}/homeworks/{homework_id}/references
```

```json
[
  {
    "id": "cae45a30-63ca-41a1-ac14-2378a685d0de",
    "workspace_id": "b3f4f628-b0e8-4737-b6b1-35480e6d5e1b",
    "homework_id": "2cb41530-904b-4242-b898-5224876f3935",
    "document_id": "6498d617-771d-416e-817e-8a69ea720e10",
    "reference_type": "answer_key",
    "created_at": "2026-07-10T07:15:00Z"
  }
]
```

无 reference 时返回 `[]`。删除接口：

```http
DELETE /workspaces/{workspace_id}/homeworks/{homework_id}/references/{reference_id}
```

成功返回 `204 No Content`，Android 不应尝试解析 JSON body。常见业务错误：workspace/homework/reference/document 不存在返回 `404`；reference 文档类型不匹配返回 `400`；重复添加同一 reference 返回 `409`；请求字段不合法返回 `422`。

新增或删除 reference 会取消当前 queued/running 评分 task。删除 reference 文档本身时，后端 purge 还会清理受影响的旧评分、错题与高亮笔记，并基于剩余资料异步重建。

前端展示成绩时必须读取 `grading_mode`：`official` 可显示“正式评分”，`provisional` 必须显示“诊断性评分”，同时展示 `confidence`，不能把它伪装成正式成绩。

## Current Product Limits

前端当前需要明确这些产品边界：

- OCR、layout、table、formula 都依赖 GPU worker 与真实模型；服务不可用时任务会重试并最终明确失败，不返回替代结果。
- DOCX/PPTX 第一版不会直接 OCR，会失败并提示先转换为 PDF 或图片。
- AI 结果来自 OpenClaw skills，必须通过后端 schema 校验，但不代表结果天然绝对正确。
- 无答案或 rubric 的评分是 `provisional` 诊断性结果；只有存在评分依据时才显示为 `official`。
- 任务进度目前是轮询，没有 SSE/WebSocket。

推荐 UI 处理：

- `uploaded`: 可以展示“开始处理”。
- `processing`: 展示 task progress 和最近 events。
- `ready`: 展示 OCR markdown/text/json 下载入口。
- `failed`: 展示 `task.error_message` 和最近 error event，允许用户重新处理。
- `deleted`: 从普通列表隐藏。
- `cancelled`: 停止轮询结果写入；可读取 events 中的取消原因。

## Error Handling

```text
401 token 缺失、过期或无效
403 不是该个人 workspace 的 owner，或无权访问该 workspace
404 workspace 内资源不存在，常见于猜其他 workspace 的 document id
409 个人 workspace 已存在，或上传尚未完成
410 当前接口已禁用，常见于个人 workspace 下邀请成员
422 请求体校验失败
```

前端建议：

- `401`: refresh token，失败则跳登录
- `403`: 回 workspace 列表并提示无权访问
- `404`: 提示资源不存在或已删除
- `409`: 继续等待 tusd 上传完成后重试
- `500`: 展示后端错误摘要，并提示查看 task events；OpenClaw/PaddleOCR/DocTr 失败通常不是前端渲染问题

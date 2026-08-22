# Frontend Integration Guide

本文档是 NotePatch Android、Web 用户端和管理后台的当前接入契约，覆盖认证、文件上传、异步任务、OCR、学习业务和 AI 对话。业务接口走 FastAPI，大文件内容走 tusd，文件最终由后端 webhook 搬到 SeaweedFS S3。

## Base URL

```bash
VITE_API_BASE_URL=http://LAN_OR_VPN_HOST:8001/api/v1
VITE_TUSD_BASE_URL=http://LAN_OR_VPN_HOST:1080/files/
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
  -> worker(default): scan、purge、merge 等短编排任务
  -> ocr-worker(ocr profile): document_processing_pipeline、ocr_document 与真实 PaddleOCR
  -> chat-worker(chat): 仅处理交互式 OpenClaw chat
  -> ai-worker(ai): 题目提取、知识库、笔记、批改、高亮与闪卡
  -> docserver: DocTr image rectification, internal only
  -> OpenClaw per-user gateway: internal only
```

前端需要知道的边界：

- 文件上传走 tusd；文件下载必须先向 FastAPI 请求短期 `download-url`。
- OCR、DocTr、OpenClaw、SeaweedFS、Redis queue 都是后端内部实现，前端不直接调用。
- 当前公开触发入口仍是 `POST /documents/{document_id}/process`；没有公开 `POST /ocr`。
- `ocr-worker` 是否启动不改变前端 API。真实 PaddleOCR 上线后，前端仍只轮询 task/events 和读取 `/ocr`。

## Web Admin Frontend

仓库内置运营管理后台在 `web/admin/`，是独立 Vite React 应用。它覆盖用户、文档、任务、学习笔记、作业、错题、会话、异步操作和审计；移动端和用户端不应调用 `/admin/*`。

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

后台使用专用管理员接口，主要能力包括：

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
POST /admin/tasks/{task_id}/cancel
POST /admin/tasks/{task_id}/retry
POST /admin/documents/{document_id}/process
DELETE /admin/documents/{document_id}
GET /admin/learning-units
POST /admin/learning-units/{learning_unit_id}/notes/{base_version_id}/revisions
GET /admin/homeworks
GET /admin/mistakes
GET /admin/conversations
GET /admin/operations
GET /admin/audit-logs
GET /admin/queues
GET /admin/services
```

后台可跨 personal workspace 管理数据，但不会改变用户端 workspace 隔离规则。所有写操作记录审计；文档和用户删除为可轮询的异步清理，危险操作需要前端明确确认。管理员不能冒充用户发送聊天，也不能代用户上传文件。

## Recommended App Flow

```text
register/login
  -> heartbeat
  -> GET /workspaces
  -> create upload-session
  -> tus upload
  -> complete-upload
  -> auto pipeline for explicit learning kinds
     or POST /documents/{id}/process for manual/other processing
  -> poll task
     or stream task events with SSE
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
let refreshPromise: Promise<boolean> | null = null;

function clearTokenState() {
  tokenState.accessToken = null;
  tokenState.refreshToken = null;
  localStorage.removeItem("access_token");
  localStorage.removeItem("refresh_token");
}

function refreshOnce(): Promise<boolean> {
  if (!refreshPromise) {
    // Read storage again so another tab can publish a newer rotated token.
    const attempted = localStorage.getItem("refresh_token");
    tokenState.refreshToken = attempted;
    if (!attempted) return Promise.resolve(false);
    refreshPromise = fetch(`${API_BASE_URL}/auth/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: attempted }),
    }).then(async (response) => {
      if (!response.ok) {
        // A stale concurrent response must not clear a newer token pair.
        if (localStorage.getItem("refresh_token") === attempted) clearTokenState();
        return false;
      }
      const data = await response.json();
      tokenState.accessToken = data.access_token;
      tokenState.refreshToken = data.refresh_token;
      localStorage.setItem("access_token", data.access_token);
      localStorage.setItem("refresh_token", data.refresh_token);
      return true;
    }).catch(() => false).finally(() => { refreshPromise = null; });
  }
  return refreshPromise;
}

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  tokenState.accessToken = localStorage.getItem("access_token");
  tokenState.refreshToken = localStorage.getItem("refresh_token");
  const headers = new Headers(init.headers);
  const isFormData = typeof FormData !== "undefined" && init.body instanceof FormData;
  if (!headers.has("Content-Type") && init.body && !isFormData) {
    headers.set("Content-Type", "application/json");
  }
  if (tokenState.accessToken) headers.set("Authorization", `Bearer ${tokenState.accessToken}`);

  let res = await fetch(`${API_BASE_URL}${path}`, { ...init, headers });

  if (res.status === 401 && tokenState.refreshToken) {
    if (await refreshOnce()) {
      headers.set("Authorization", `Bearer ${tokenState.accessToken}`);
      res = await fetch(`${API_BASE_URL}${path}`, { ...init, headers });
    }
  }

  if (!res.ok) {
    const error = await res.json().catch(() => ({}));
    throw new Error(error.message ?? error.detail ?? `Request failed: ${res.status}`);
  }
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  return (text ? JSON.parse(text) : undefined) as T;
}
```

Web、Android 和多标签页客户端都必须把 refresh 做成 single-flight。后端允许同一枚因轮换失效的 token 在短暂宽限期内处理并发请求，但 logout、修改密码和管理员禁用产生的撤销没有宽限。

## Auth APIs

```http
POST /auth/register
POST /auth/login
POST /auth/refresh
POST /auth/logout
GET  /auth/me
POST /auth/change-password
PATCH /auth/preferences
GET  /user/profile
PUT  /user/profile
POST /user/avatar/upload
GET  /user/avatar/download-url
GET  /user/avatar/content
DELETE /user/avatar
POST /presence/heartbeat
POST /presence/offline
```

登录/注册返回 `access_token` 和 `refresh_token`。业务接口都带：

```http
Authorization: Bearer <access_token>
```

资料更新前先请求 `GET /user/profile` 并保存响应 `ETag`。`PUT /user/profile`、头像上传和头像删除都必须发送：

```http
If-Match: "profile-3"
Idempotency-Key: <本次操作稳定且唯一的 key>
```

新资料接口响应为 `{code,message,data}`。`412 profile_version_mismatch` 时重新读取资料后让用户确认；`409 idempotency_conflict` 表示同一 key 被用于不同内容。修改邮箱必须同时提交 `current_password`，成功响应的 `data.reauthentication_required=true`，此后旧 access/refresh token 都不可用，客户端必须回到登录页。头像使用 multipart 字段 `file`，只支持真实 JPEG/PNG；展示时先获取 `/user/avatar/download-url`，不要缓存已经过期的 SeaweedFS 签名 URL。

资料读取响应示例：

```http
HTTP/1.1 200 OK
ETag: "profile-3"

{
  "code": "ok",
  "message": "Profile loaded",
  "data": {
    "id": "user-uuid",
    "name": "Alice",
    "email": "alice@example.com",
    "avatar_url": "/api/v1/user/avatar/content?v=avatar-version",
    "profile_version": 3,
    "reauthentication_required": false
  }
}
```

登录、注册、refresh 和 `GET /auth/me` 返回的 `user` 还包含图片备注开关：

```ts
type User = {
  id: string;
  email: string;
  full_name: string | null;
  auto_image_remark_enabled: boolean;
  // 其余字段省略
};
```

读取该值初始化 App 设置项。修改开关使用：

```http
PATCH /api/v1/auth/preferences
Authorization: Bearer <access_token>
Content-Type: application/json

{"auto_image_remark_enabled":false}
```

响应是更新后的 `UserRead`。该设置是用户全局偏好，只影响之后创建的图片上传；不会批量改写已经存在的备注。不要只在客户端本地保存开关。

更新姓名或邮箱：

```http
PUT /api/v1/user/profile
Authorization: Bearer <access_token>
If-Match: "profile-3"
Idempotency-Key: profile-edit-550e8400-e29b-41d4-a716-446655440000
Content-Type: application/json

{"name":"Alice Chen"}
```

邮箱实际发生变化时，请求必须额外包含 `current_password`。成功响应会返回新的 `ETag`；客户端应以响应中的 `data` 原子替换本地用户资料。若 `reauthentication_required=true`，不要再尝试 refresh，应立即清除当前 token 并重新登录。

头像上传使用 multipart，不能把图片编码成 JSON/base64：

```http
POST /api/v1/user/avatar/upload
Authorization: Bearer <access_token>
If-Match: "profile-3"
Idempotency-Key: avatar-upload-550e8400-e29b-41d4-a716-446655440000
Content-Type: multipart/form-data

file=<JPEG or PNG>
```

后端会实际解码并重新编码图片，移除 EXIF、文件名和尾随数据。响应中的 `avatar_url` 是稳定的鉴权入口；`GET /user/avatar/download-url` 返回短期 SeaweedFS URL，本地存储模式则返回稳定内容入口。替换头像后只保存新 URL，不缓存旧签名 URL。

资料接口稳定错误码：

| HTTP | `code` | 客户端处理 |
| --- | --- | --- |
| 400 | `invalid_if_match` / `invalid_idempotency_key` | 修正请求头，不自动重试 |
| 409 | `email_conflict` / `idempotency_conflict` / `last_admin` | 展示冲突；不同 payload 必须换 key |
| 412 | `profile_version_mismatch` | 重新 GET profile，并由用户确认覆盖 |
| 413 | `avatar_too_large` | 压缩或选择更小图片 |
| 422 | `validation_error` / `avatar_invalid` / `avatar_dimensions_exceeded` | 展示字段或图片错误 |
| 428 | `precondition_required` | 补充 `If-Match` |
| 503 | `storage_unavailable` | 保留本地选择，稍后以相同 payload 和相同 key 重试 |

同一个 `Idempotency-Key` 和完全相同的内容在 24 小时内会返回第一次结果，并设置 `Idempotent-Replayed: true`；不要为一次操作的网络重试生成新 key。

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
    status: "created" | "uploading" | "uploaded" | "scanning" | "processing" | "ready" | "failed" | "deleted";
    original_filename: string;
    remark: string | null;
    remark_source: "user" | "ai_ocr" | "original_filename" | null;
    mime_type: string | null;
    file_size: number | null;
    file_type: "image" | "pdf" | "docx" | "pptx" | "audio" | "video" | "other";
    document_kind: "homework" | "corrected_homework" | "courseware" | "note" | "exam" | "answer_key" | "rubric" | "chat_attachment" | "other";
    retention_scope: "workspace" | "conversation";
    chat_conversation_id: string | null;
    save_to_documents: boolean;
    image_remark_status: "waiting_upload" | "waiting_ocr" | "queued" | "running" | "succeeded" | "failed" | "empty_ocr" | "disabled" | "user" | null;
    image_remark_task_id: string | null;
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

async function createUploadSession(
  workspaceId: string,
  file: File,
  documentKind = "other",
  saveToDocuments = true,
  remark?: string,
) {
  return apiFetch<UploadSessionResponse>(`/workspaces/${workspaceId}/documents/upload-session`, {
    method: "POST",
    body: JSON.stringify({
      filename: file.name,
      mime_type: file.type || "application/octet-stream",
      file_size: file.size,
      document_kind: documentKind,
      save_to_documents: saveToDocuments,
      title: file.name,
      ...(remark ? { remark } : {}),
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


反向代理部署时，tusd 的 `Location` 必须保持完整公开入口，例如
`https://PUBLIC_IP/np-<prefix>/files/{upload_id}`。客户端必须使用 tus SDK 返回的
`upload.url` 继续 `HEAD/PATCH`，不要自行去掉 HTTPS、随机前缀或改写成 `/files/{id}`。
`POST` 创建成功但长期没有上传进度时，可先检查 tusd upload info：若
`Size > 0` 且 `Offset = 0`，表示资源已创建但文件字节尚未上传；此时不要调用
`complete-upload`，应让 tus SDK 重新上传或恢复该 URL。

上传成功后，重新读取 document 详情或列表。`complete-upload` 返回最新 `Document`：显式学习类型在 `AUTO_LEARNING_PIPELINE=true` 时会自动排入处理队列；`chat_attachment` 和未自动处理的 `other` 通常直接为 `ready`。若客户端需要取得自动任务 ID，可在完成上传后立即调用一次 `process`，后端会复用同文档仍在 queued/running 的处理任务；已经 `ready` 的文档不要无条件再次调用，除非用户明确要求重处理。

图片备注遵循固定优先级：upload-session 显式 `remark` > 用户后续编辑 > OCR 后 AI 自动备注 > 原文件名。用户全局开关由 `PATCH /auth/preferences` 的 `auto_image_remark_enabled` 控制，默认开启；关闭后新上传且未填写备注的文件直接使用 `original_filename`。开启时后端先完成 OCR，再创建 `generate_image_remark` AI 任务，固定使用 `openai/gpt-5.6-luna` 与 `minimal` 思考强度，只发送 OCR 文本，不发送原图或 DocTr 图片。OCR 为空时也回退原文件名。

客户端优先展示 `document.remark ?? document.original_filename`，原始文件名始终读取 `document.original_filename`。自动生成期间可以立即展示当前 `remark`（初始值为原文件名），根据 `image_remark_status` 显示非阻塞状态，并通过 `image_remark_task_id` 轮询 task/events。旧 `ai_image_name/ai_image_naming_*` 仅为旧版本兼容字段，新客户端不要读取。

状态建议：

| `image_remark_status` | 客户端行为 |
| --- | --- |
| `waiting_upload` / `waiting_ocr` / `queued` / `running` | 显示当前备注，可附加“正在识别备注”，不要阻塞文件使用 |
| `succeeded` | 刷新 Document，显示 AI OCR 备注 |
| `empty_ocr` / `disabled` | 正常显示原文件名，不展示错误 |
| `user` | 显示用户备注，不再等待 AI |
| `failed` | 保留当前备注，可提供稍后重试提示 |

编辑备注：

```http
PATCH /workspaces/{workspace_id}/documents/{document_id}
Content-Type: application/json

{"remark":"CPU 与寄存器课堂笔记"}
```

保存后 `remark_source=user`，后端会取消该文档尚未完成的自动备注任务，避免模型晚到覆盖用户内容。

App 编辑框应以 `remark` 为初值，长度限制为 255。提交成功后用完整响应替换本地 Document；不要同时修改 `original_filename` 或 `title`。空字符串会返回 `422`，如需“恢复原文件名”，提交 `original_filename` 作为新的用户备注即可。

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
  | "converted_pdf"
  | "deskewed_image"
  | "binary_image"
  | "ocr_json"
  | "ocr_markdown"
  | "ocr_text"
  | "layout_json"
  | "formula_json"
  | "tables_json"
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

轮询接口继续兼容。新客户端建议优先使用 SSE 事件流：

```http
GET /workspaces/{workspace_id}/tasks/{task_id}/events/stream
Authorization: Bearer <access_token>
Last-Event-ID: <last sequence>
Accept: text/event-stream
```

需要自定义 `Authorization` 请求头，因此浏览器端应使用基于 `fetch` 的 SSE 客户端。保存最后一个 event ID，断线重连时发送 `Last-Event-ID`，忽略 heartbeat comment，并在收到 `done` 事件后关闭连接。轮询仍可作为不支持 SSE 客户端的 fallback。

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
  attempt: number;
  max_attempts: number;
  next_attempt_at: string | null;
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
  sequence_no: number;
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

空会话页面的初始化问候由后端提供，不要再在客户端硬编码：

```http
GET /api/v1/workspaces/{workspace_id}/ai/greeting?client_locale=zh-CN
```

响应包含 `assistant_name/message/message_key/format/locale/onboarding_required/onboarding_version/questions`。前端在尚未选中会话或当前会话没有消息时展示 `message`；创建首条真实消息后隐藏。该问候不会写入 PostgreSQL、不会创建 conversation，也不会注入 OpenClaw 历史。`client_locale` 未传时后端使用 `Accept-Language`，再回退到部署默认 locale。

### AI 初始化问卷

注册、登录和 refresh 返回的 `user` 以及 `GET /auth/me` 包含：

```ts
type AiOnboardingState = {
  ai_onboarding_version: number;
  ai_onboarding_completed_at: string | null;
  ai_onboarding_completed: boolean;
  ai_preferences: AiPreferences | Record<string, never>;
};

type AiPreferences = {
  response_language: "match_user" | "client_locale" | "zh-CN" | "en-US" | "pt-BR";
  collaboration_style: "direct" | "collaborative" | "coach" | "socratic";
  response_depth: "concise" | "balanced" | "detailed";
  response_structure: "adaptive" | "steps" | "bullets" | "prose";
  clarification_policy: "ask_when_ambiguous" | "assume_when_safe" | "confirm_before_actions";
  feedback_tone: "gentle" | "neutral" | "strict";
  learning_guidance: "answer_first" | "explain_then_answer" | "hint_first";
  custom_instructions: string | null;
};
```

`learning_guidance` 必须按语义原样提交，不能按选项下标映射：

- `answer_first`：先给答案，再解释。
- `explain_then_answer`：先解释方法，再给答案。
- `hint_first`：题目首轮只给提示或引导问题，不直接泄露最终答案；询问通用方法时可讲策略，但完整例题必须停在最终结果之前；用户在获得引导后明确要求答案时才可给出。

客户端保存设置后应重新读取 `GET /api/v1/auth/ai-onboarding` 或 `/api/v1/auth/me`，以服务端返回值作为最终状态。AI task 会在创建时固化该偏好，修改设置只影响之后创建的 task。

进入 AI 页面时先检查 `ai_onboarding_completed`，并以服务端问题目录渲染七步表单：

```http
GET /api/v1/auth/ai-onboarding
PUT /api/v1/auth/ai-onboarding
Content-Type: application/json

{
  "version": 1,
  "answers": {
    "response_language": "match_user",
    "collaboration_style": "collaborative",
    "response_depth": "balanced",
    "response_structure": "adaptive",
    "clarification_policy": "ask_when_ambiguous",
    "feedback_tone": "neutral",
    "learning_guidance": "explain_then_answer",
    "custom_instructions": null
  }
}
```

`GET` 返回 `version/completed/completed_at/answers/questions`。问题及选项使用稳定的 `message_key/label_key`，客户端负责本地化；每项都必须选择，但可选择默认值，不能整体跳过。问卷不调用模型、不写聊天历史。未完成时发送 chat 返回：

```json
{
  "detail": {
    "code": "ai_onboarding_required",
    "version": 1,
    "onboarding_url": "/api/v1/auth/ai-onboarding"
  }
}
```

客户端收到该 `409` 时打开问卷，不要把它显示成普通聊天失败气泡。设置页可调用 `PATCH /api/v1/auth/preferences`，例如 `{"ai_preferences":{"response_depth":"detailed"}}`；修改只影响之后创建的任务。`custom_instructions` 最长 1000 字，不能覆盖安全、评分、知识来源、笔记忠实度和工具限制。

`POST /workspaces/{workspace_id}/ai/chat` 会自动创建或继续一条持久化对话。请求不传 `conversation_id` 时后端创建新会话；响应 task 的 `payload.conversation_id`、`user_message_id` 和 `assistant_message_id` 用于刷新当前会话。assistant message 会先以 `queued` 出现，worker 处理期间变为 `running`，最终为 `succeeded`、`failed` 或 `cancelled`。

```ts
type ChatConversation = {
  id: string;
  workspace_id: string;
  user_id: string;
  title: string;
  title_source: "prompt" | "ai" | "manual";
  title_generated_at: string | null;
  last_message_at: string | null;
  created_at: string;
  updated_at: string;
};

type ChatAttachment = {
  document_id: string;
  filename: string;
  title: string | null;
  mime_type: string | null;
  file_type: string;
  file_size: number | null;
  status: string;
  retention_scope: "workspace" | "conversation";
  save_to_documents: boolean;
  availability: "available" | "unavailable";
};

type ChatMessage = {
  id: string;
  workspace_id: string;
  conversation_id: string;
  user_id: string;
  role: "user" | "assistant";
  content: string;
  task_id: string | null;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  error_message: string | null;
  attachments: ChatAttachment[];
  citations: Array<{ chunk_id?: string; document_id?: string; score?: number; metadata?: object }>;
  source_status: "available" | "partially_unavailable" | "unavailable";
  model_id: string | null;
  revision_of_message_id: string | null;
  superseded_by_message_id: string | null;
  superseded_at: string | null;
  created_at: string;
  updated_at: string;
};

async function sendChat(
  workspaceId: string,
  prompt: string,
  conversationId?: string,
  attachmentDocumentIds: string[] = [],
  clientLocale = Intl.DateTimeFormat().resolvedOptions().locale,
) {
  return apiFetch<Task>(`/workspaces/${workspaceId}/ai/chat`, {
    method: "POST",
    body: JSON.stringify({
      prompt,
      client_locale: clientLocale,
      conversation_id: conversationId,
      input: { attachments: attachmentDocumentIds.map((document_id) => ({ document_id })) },
      options: {},
    }),
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


附件必须先通过 tusd 文档上传流程完成，聊天只提交 `document_id`。不要把 base64、对象键、客户端路径、文件名或 MIME 当作可信附件来源。消息列表返回的 `attachments` 用于重建聊天气泡；需要显示原图时，再通过该 document 的鉴权 download-url 获取短期 URL。启用历史后，后端会在每轮任务中把历史附件映射到新的 OpenClaw task snapshot，因此客户端无需重复上传同一张图。

AI 助手中的图片或文件必须以 `document_kind: "chat_attachment"` 创建上传会话，并向用户提供“保存到资料”开关：

- 开启：提交 `save_to_documents: true`（默认），文件保留在普通文档列表，删除对话不会删除它。
- 关闭：提交 `save_to_documents: false`，文件标记为 `retention_scope: "conversation"`，不出现在 `GET /documents`，首次发送时绑定当前会话，只能在该会话上下文中继续引用；删除会话会异步 purge 文件及 SeaweedFS 对象。

临时附件仍必须先上传到 SeaweedFS，不能只把 base64 留在请求或手机内存里；“只在上下文中储存”表示生命周期属于会话，而不是不落对象存储。`save_to_documents=false` 只允许用于 `chat_attachment`，对 homework/courseware/note 等类型会返回 `422`。聊天附件不触发 OCR、知识库、电子笔记、评分或闪卡。为兼容旧客户端，`other` 也不再自动进入学习流水线。真正的学习资料必须显式选择 `courseware`、`note`、`homework`、`corrected_homework` 或 `exam`。

不要在聊天附件上传完成后调用 `/documents/{document_id}/process`。服务端会对 `chat_attachment` 返回 `409`，防止客户端误操作。

### AI 附件支持格式与解析边界

`chat_attachment` 和 `document_kind=other` 可上传 PDF、Office/ODF、电子表格、文本数据、图片、EPUB、EML/MSG、IPYNB、常见压缩包和音视频。扩展格式在 Document 中仍返回 `file_type="other"`，旧客户端不需要增加枚举。后端会在每个 OpenClaw task 的无网络 sandbox 中优先读取已有 OCR artifact，否则使用 `notepatch-file` 解析原文件；前端不要调用 parser、OpenClaw 或对象存储。

音视频第一版只提供 metadata、音轨和关键帧，没有语音转写。加密 PDF、Office 和压缩包不接受密码，task 会返回明确错误。归档中的路径穿越、符号链接、设备文件、超量条目、压缩炸弹和过深嵌套会被拒绝。未知格式上传返回 `415`，其 `detail.code=unsupported_file_format`。

学习资料流水线仍只支持图片、PDF、DOCX 和 PPTX。EPUB、XLSX、邮件、Notebook、压缩包或音视频若声明为 `homework/note/courseware/exam`，上传会话返回 `422`，其 `detail.code=unsupported_learning_format`。这些文件若只需要 AI 阅读，应使用 `chat_attachment`；若需保留为普通资料但不进入 OCR，则使用 `other`。


可用接口：

```http
POST   /workspaces/{workspace_id}/ai/chat
GET    /workspaces/{workspace_id}/ai/conversations
GET    /workspaces/{workspace_id}/ai/conversations/{conversation_id}
GET    /workspaces/{workspace_id}/ai/conversations/{conversation_id}/messages
POST   /workspaces/{workspace_id}/ai/conversations/{conversation_id}/messages/{message_id}/revisions
PATCH  /workspaces/{workspace_id}/ai/conversations/{conversation_id}
DELETE /workspaces/{workspace_id}/ai/conversations/{conversation_id}
PATCH  /auth/preferences
```

`GET /auth/me`、登录和 refresh 响应都会给出 `user.ai_history_enabled`。前端用 `PATCH /auth/preferences` 传 `{ "ai_history_enabled": false }` 关闭历史消息注入；历史仍保留并可查看，后续 OpenClaw 调用仍会包含当前 prompt、当前附件以及本次启用的知识库检索结果，但不会附加以前的聊天消息。重新开启后，后端会自动传入该会话最近 `AI_CHAT_HISTORY_MESSAGE_LIMIT`（默认 20）条成功消息。删除会话是软删除，删除后不可继续发送或读取，关联的 queued/running OpenClaw task 会被协作取消。

### 聊天流与停止

`POST /api/v1/workspaces/{workspace_id}/ai/chat` 仍返回异步 task。每条消息可携带：

```json
{"prompt":"解释这道题","options":{"temperature":0.7,"thinking":{"enabled":true,"effort":"low"}}}
```

`temperature` 可选且范围为 `0..2`，只影响当前消息；未传时由模型使用默认值。未传 thinking 时思考默认关闭，`effort` 可为 `minimal`、`low`、`medium`、`high` 或 `adaptive`。随后连接 `GET /api/v1/workspaces/{workspace_id}/tasks/{task_id}/events/stream`，逐条处理 `event: task_event`：`chat_answer_delta.data.delta` 追加回答草稿，`chat_reasoning_delta.data.delta` 追加可展示的推理摘要。两类事件都包含 `stream`、`chunk_index`、`attempt`、`characters`；用 SSE `id` 作为 `Last-Event-ID` 重连游标。收到 `chat_stream_started` 时清空当前 attempt 的草稿，收到 `done` 后刷新 task 与会话消息。

流式增量的 `data` 固定为：

```json
{
  "stream": "answer",
  "delta": "本次增量文本",
  "chunk_index": 3,
  "attempt": 1,
  "characters": 2048
}
```

`stream` 可为 `answer` 或 `reasoning`。reasoning 是后端规范化后的安全进度摘要，不能当作模型原始思维链，也不写入下一轮聊天历史。客户端应接受 `chat_reasoning_unavailable`，此时继续展示回答即可；遇到 `chat_stream_truncated` 则保留已收到的片段，并在 task 完成后以 assistant message / `task.result.answer` 为最终正文。

停止按钮调用 `POST /api/v1/workspaces/{workspace_id}/tasks/{task_id}/cancel`。响应为 `202 TaskRead`；queued 会立即取消，running 则等待 SSE 的 `cancelled`/`done`。保留当前已经显示的正文，不把 reasoning summary 写入聊天历史。

编辑历史 user message 时调用：

```http
POST /api/v1/workspaces/{workspace_id}/ai/conversations/{conversation_id}/messages/{message_id}/revisions
Content-Type: application/json

{"prompt":"修改后的问题","input":null,"options":{"temperature":0.4}}
```

响应为 `{code,message,data}`，其中 `data` 是新的 `TaskRead`。后端会取消该会话活动任务并截断目标消息之后的旧分支；默认消息列表不再返回旧分支。需要审计视图时请求 `messages?include_superseded=true`。`input` 未提交或没有 `attachments` 时继承原消息附件；显式传 `attachments:[]` 才清空附件。

修订成功响应示例：

```json
{
  "code": "ok",
  "message": "Chat message revised",
  "data": {
    "id": "new-task-uuid",
    "task_type": "openclaw_agent_run",
    "status": "queued",
    "payload": {
      "conversation_id": "conversation-uuid",
      "revised_message_id": "old-user-message-uuid",
      "options": {"temperature": 0.4}
    }
  }
}
```

前端提交成功后应清除被修订消息之后的当前 UI 分支，使用 `data.id` 连接 task SSE；不要物理删除本地旧消息。审计页面可通过 `include_superseded=true` 展示 `revision_of_message_id`、`superseded_by_message_id` 和 `superseded_at`。

新会话先以首条 prompt 作为临时标题（`title_source="prompt"`）。首轮回答成功后，后端让 OpenClaw 根据最早几条成功消息生成短标题并更新为 `title_source="ai"`；前端只需刷新会话列表，不应自行生成标题。用户通过 conversation PATCH 手动改名后会变为 `title_source="manual"`，后端不会再自动覆盖。标题生成失败不会影响聊天 task 或 assistant 回答，仍保留临时标题并在后续对话中重试。

删除被引用的资料不会删除已经完成的问答正文。后端会从 message 中移除失效 citation，并把 `source_status` 改为 `partially_unavailable` 或 `unavailable`；前端应保留正文并显示“部分/全部来源资料已删除”的非阻断提示。

任务成功后再拉取 artifacts：

```ts
async function getDocumentArtifacts(workspaceId: string, documentId: string) {
  return apiFetch<DocumentArtifact[]>(`/workspaces/${workspaceId}/documents/${documentId}/artifacts`);
}
```

如果只关心 OCR 结果，优先调用 `/ocr`，它会返回最新一组完整 `ocr_json`、`ocr_markdown`、`ocr_text` metadata；`include_download_url=true` 时可直接拿到下载地址。

## Automatic Learning Workflow

客户端只负责上传、选择笔记策略、跟踪聚合 workflow 和读取结果，不直接编排 OCR、OpenClaw 或 worker。

```ts
type NoteContentEditLevel = "verbatim" | "spelling" | "conceptual" | "rewrite";
type NoteLayoutEditLevel = "preserve" | "minor" | "reorder" | "reflow";

type LearningMetadata = {
  learning_unit_id?: string;
  learning_unit_title?: string;
  subject?: string;
  grade_level?: string;
  topic?: string;
  auto_group_learning_unit?: boolean;
  note_set_id?: string;
  page_index?: number;
  note_content_edit_level?: NoteContentEditLevel;
  note_layout_edit_level?: NoteLayoutEditLevel;
};
```

资料分流：

- `note`：OCR → KB → 防抖 → 忠实电子笔记 → 闪卡。
- `courseware/other`：OCR → KB → 笔记缺口；不自动生成笔记。
- `homework/corrected_homework`：OCR → 切题 → 评分 → 缺口；已有笔记时才高亮。
- `exam`：OCR → 切题 → 缺口。
- `answer_key/rubric`：OCR 后作为评分依据。
- `chat_attachment`：只进入聊天上下文，不进入学习流水线。

### Note Preferences And One-Off Overrides

读取用户时会返回：

```ts
type NotePreferences = {
  note_content_edit_level: NoteContentEditLevel;
  note_layout_edit_level: NoteLayoutEditLevel;
  note_history_limit: number; // 0..100，指最新版本之外保留数量
};
```

更新全局默认值：

```http
PATCH /api/v1/auth/preferences
Content-Type: application/json

{"note_content_edit_level":"conceptual","note_layout_edit_level":"minor","note_history_limit":3}
```

上传 note 时可通过 upload-session 顶层同名字段覆盖本次生成策略。也可手动触发并覆盖：

```http
POST /api/v1/workspaces/{workspace_id}/learning-units/{unit_id}/notes/generate

{"content_edit_level":"verbatim","layout_edit_level":"preserve","force_reprocess":false}
```

四档内容语义：

- `verbatim`：只修复可由原图确认的 OCR 转录错误。
- `spelling`：额外允许错别字/拼写修复。
- `conceptual`：额外允许有可靠来源的严重概念修正，不改变表达风格。
- `rewrite`：重构整份原稿，而不是逐条搬运 OCR；会合并相关碎片、整理标题和段落、归纳并改写表达，并可使用同一学习单元中与原笔记主题直接相关的可靠资料补全缺失内容。若没有外部可靠资料，只重构原稿已有事实，不凭模型常识扩写。

四档排版语义：

- `preserve`：保持块顺序、分组和相对布局。
- `minor`：只把边缘批注、公式、图表和代码移回合理位置，不上下调换。
- `reorder`：允许调换上下顺序，但不删除内容。
- `reflow`：允许重新设计版式。

策略在 task 创建时固化。服务端会校验 Note IR 的逐块来源、代码缩进、公式、表格、箭头/圈选关系和纠错证据；客户端无需信任模型自行遵守策略。

`rewrite` 的资料补全只在用户主动生成或重生成笔记时执行。后端使用原稿标题、OCR 和已有知识点检索同 LearningUnit 的课件、知识块、答案/rubric、作业题目及评分知识点；作业题目只用于发现缺失主题，学生答案和 provisional 评分不能单独作为事实来源。无关内容不会被塞进笔记，每个补充 block 都必须有有效来源。

渲染后的补充段落会显示轻量“资料补充”标记，客户端不要自行追加或移除。若需要展示来源，可从笔记 JSON 读取：

```ts
type EvidenceSupplementBlock = {
  origin: "evidence_supplement";
  source_refs: Array<{
    evidence_id: string;
    document_id?: string;
    knowledge_chunk_id?: string;
    question_id?: string;
    page_index?: number;
    block_id?: string;
    relevance_score?: number;
  }>;
  supplement_reason: string;
};
```

笔记版本 metadata 会提供 `completion_count`、`completion_source_document_ids`、`completion_evidence_revision` 和 `completion_strategy`。前端只负责展示；上传课件或作业仍只更新知识库和缺口建议，不会自动改写已有笔记。BGE-M3 暂时不可用时生成任务会重试，而不是返回缺少补全内容的“成功”笔记。

### Continuous Multi-Image Notes

连续多图必须优先使用 NoteSet：

```http
POST /api/v1/workspaces/{workspace_id}/note-sets

{
  "title":"计算机网络第 5 讲",
  "expected_page_count":4,
  "learning_unit_id":null,
  "subject":"computer science",
  "content_edit_level":"conceptual",
  "layout_edit_level":"minor"
}
```

响应包含 `id/status/learning_unit_id/documents`。随后每张图片创建 upload-session：

```json
{
  "filename":"page-01.jpg",
  "mime_type":"image/jpeg",
  "file_size":123456,
  "document_kind":"note",
  "note_set_id":"...",
  "page_index":0
}
```

`page_index` 从 0 开始且组内唯一。所有 tus 上传完成后调用 `POST /api/v1/workspaces/{workspace_id}/note-sets/{note_set_id}/complete`。若缺页、页仍在上传或类型不是 note，会返回 `409/422`。每页独立 OCR；全部页面 KB 完成后按页序只生成一份笔记。不要把每张图片当作独立 note 单元提交。

### Knowledge Gap UX

```ts
type NoteGap = {
  id: string;
  knowledge_point_id: string;
  note_version_id: string | null;
  status: "pending" | "draft" | "no_base_note" | "accepted" | "rejected" | "stale";
  source_refs: Array<{
    document_id: string;
    page_index: number;
    block_id: string | null;
    bbox: number[] | null;
    excerpt: string;
  }>;
  target_section_id: string | null;
  target_anchor: string | null;
  insert_position: "before" | "after" | "inside";
};
```

接口：

```http
GET  /api/v1/workspaces/{workspace_id}/learning-units/{unit_id}/note-gaps
GET  /api/v1/workspaces/{workspace_id}/learning-units/{unit_id}/note-gaps/{gap_id}
POST /api/v1/workspaces/{workspace_id}/learning-units/{unit_id}/note-gaps/{gap_id}/draft
PATCH /api/v1/workspaces/{workspace_id}/learning-units/{unit_id}/note-gaps/{gap_id}/draft
POST /api/v1/workspaces/{workspace_id}/learning-units/{unit_id}/note-gaps/{gap_id}/draft/regenerate
POST /api/v1/workspaces/{workspace_id}/learning-units/{unit_id}/note-gaps/{gap_id}/accept
POST /api/v1/workspaces/{workspace_id}/learning-units/{unit_id}/note-gaps/{gap_id}/reject
POST /api/v1/workspaces/{workspace_id}/learning-units/{unit_id}/notes/from-gaps
```

创建草稿请求可包含 `selected_source_refs/target_section_id/insert_position/instruction`，返回异步 Task。PATCH 草稿可提交 `html/target_section_id/insert_position`；regenerate 提交 `{"feedback":"..." }`。接受草稿会锁定最新笔记并创建新版本；基础版本变化返回 `409`。通过 `rendered_html#target_anchor` 跳转建议位置。状态 `stale` 不允许继续插入。

没有基础笔记时只显示 `no_base_note` 建议。用户选择 gap 后调用 `notes/from-gaps`，后端才创建首版：

```json
{"gap_ids":["..."],"title":"补充笔记","content_edit_level":"conceptual","layout_edit_level":"minor"}
```

### Notes, Corrections And Rendering

```ts
type StudyNoteVersion = {
  id: string;
  version_no: number;
  title: string;
  note_ir_object_key: string | null;
  content_edit_level: NoteContentEditLevel;
  layout_edit_level: NoteLayoutEditLevel;
  knowledge_point_ids: string[];
  source_document_ids: string[];
  download_urls?: Record<string, string>;
  rendering: { theme_id: string; css_url: string; wrapper_class: string };
};
```

查询：

```http
GET  /api/v1/workspaces/{workspace_id}/learning-units/{unit_id}/notes?include_download_url=true
GET  /api/v1/workspaces/{workspace_id}/learning-units/{unit_id}/notes/{version_id}/download-url?kind=rendered_html
GET  /api/v1/workspaces/{workspace_id}/learning-units/{unit_id}/notes/{version_id}/corrections
POST /api/v1/workspaces/{workspace_id}/learning-units/{unit_id}/notes/{latest_version_id}/revisions
GET  /api/v1/workspaces/{workspace_id}/learning-units/{unit_id}/flashcard-decks/latest
```

### Flashcard Review Hints

`GET /api/v1/workspaces/{workspace_id}/learning-units/{unit_id}/flashcard-decks/latest` and historical deck detail responses include a structured `review_hint` on every card:

```json
{
  "priority_score": 0.72,
  "review_hint": {
    "primary": {
      "code": "recently_improving",
      "message_key": "flashcards.hints.recently_improving",
      "tone": "positive",
      "params": {"correct_streak": 3}
    },
    "badges": [
      {
        "code": "historical_errors",
        "message_key": "flashcards.badges.historical_errors",
        "tone": "neutral",
        "params": {"count": 4}
      }
    ],
    "data_quality": "complete"
  }
}
```

The backend returns codes and parameters rather than display text. Localize `message_key` in the client and interpolate `params`. Recommended examples:

| key | zh-CN | en |
| --- | --- | --- |
| `flashcards.hints.recently_improving` | 最近连续答对 {correct_streak} 次，正在进步 | Improving: {correct_streak} correct in a row |
| `flashcards.hints.just_missed` | 最近这类题有失误，建议趁热复习 | Recently missed; review while it is fresh |
| `flashcards.hints.frequent_recent_errors` | 近 {window_days} 天错了 {count} 次 | {count} misses in the last {window_days} days |
| `flashcards.hints.recently_correct` | 最近做对过，适合巩固 | Recently correct; reinforce it |
| `flashcards.hints.from_notes` | 来自笔记的核心知识点 | Key point from your notes |
| `flashcards.hints.historical_review` | 以前容易出错，建议复习 | Review due to earlier mistakes |
| `flashcards.hints.general_review` | 建议复习 | Suggested review |

Badge keys are `flashcards.badges.correct_streak`, `recent_errors`, `historical_errors`, and `latest_outcome`. Show the primary hint in the recommendation area and at most three compact badges. Unknown keys must fall back to “建议复习” / “Suggested review”. `data_quality=legacy` means the historical deck predates the complete statistics snapshot; do not combine it with live attempt data in the client. A new grading result creates a new deck, so hints and priority naturally refresh together.

优先在 sandboxed WebView 加载 `download_urls.rendered_html`；它会按版本套用不可变主题。历史版本保留原主题，新生成和完成确定性重排的版本使用 `notepatch-paper-v4`，客户端必须以 `rendering.theme_id/css_url` 为准，不要硬编码主题。Note IR 中的受限 Markdown 已由后端转换为段落、列表、标题、表格和代码 HTML；客户端不得再次执行 Markdown 解析。页面不会自动嵌入、裁剪或复制用户上传的原图/DocTr 矫正图，也不要自行执行 fragment 中的脚本或外部资源。手工编辑创建新版本；高亮只更新最新版本。历史超限删除是异步的，UI 不应假设版本号连续。

图片 note 同时使用 OCR 和 DocTr 矫正图：OCR 是文字事实基线，`deskewed_image` 用于确认代码、公式、圈选/箭头及布局。后端不会把原始上传图作为文档 Skill 的 `image_url`；矫正 artifact 缺失时会自动补跑 DocTr，失败则重试且不会偷偷回退原图。只有模型明确不支持多模态时才会 OCR-only 完成。非 `rewrite` 模式下课件知识库只用于概念纠错，缺失内容通过 gap UI 由用户确认；`rewrite` 模式则允许把同主题且有权威来源的内容作为带标记的资料补充写入新版本。

前端无需新增参数或调用 DocTr。可在 workflow/task events 中展示 `ai_visual_deskewed_reused`、`ai_visual_deskewed_regeneration_started`、`ai_visual_deskewed_regenerated` 和 `ai_visual_deskewed_original_missing` 的通用进度或错误摘要。普通 AI 聊天附件不属于文档 Skill，仍按用户原图发送和显示。


笔记本来源标识规则：当 `content_edit_level=verbatim` 时，客户端应预期学校、公司、制造商等印刷文字仍被忠实保留；`spelling/conceptual/rewrite` 模式会自动排除这些非学习内容。排除记录只用于审计，不需要客户端参与判断；如管理端展示结构化 JSON，可读取 `excluded_source_blocks`。客户端不要自行按“学校”“公司”等关键词过滤正文，避免误删真实学习内容。

### Workflow Tracking

上传响应中的 `workflow_run_id` 是首选进度入口：

```http
GET /api/v1/workspaces/{workspace_id}/workflows/{workflow_run_id}
GET /api/v1/workspaces/{workspace_id}/workflows/{workflow_run_id}/events
GET /api/v1/workspaces/{workspace_id}/workflows/{workflow_run_id}/events/stream
```

核心 OCR/KB/评分成功、增强笔记失败时总状态可能为 `partially_succeeded`；防抖阶段为 `waiting`。不要把单个 document processing task 成功等同于整条学习流程完成。

`POST /workspaces/{workspace_id}/ai/chat` 是唯一的 AI 对话入口。它创建后端异步 OpenClaw 任务，前端不直接调用 OpenClaw Gateway，也不要启动/停止容器。请求体使用 `{ "prompt": string, "client_locale"?: string, "conversation_id"?: string, "input": object, "options": object }`，其中 `client_locale` 必须是 BCP 47 language tag；Web 可用浏览器 locale，Android 使用当前应用语言或 `Locale.getDefault().toLanguageTag()`。响应是 `TaskRead`；随后轮询 task 与 events 获取 `task.result.answer` 或失败原因。会话历史由后端保存，是否注入 OpenClaw 由用户全局 `ai_history_enabled` 控制。后端会为每个用户维护独立 OpenClaw gateway 配置和用户数据目录；用户在线时 supervisor 保持 gateway 运行，worker 在任务前创建 task-local 文档快照，再把 OpenClaw 输出上传回 SeaweedFS。

镜像范围取决于附件：当前消息或启用历史后的消息显式引用附件时，worker 为该 task 创建仅包含这些附件的资料快照；无附件的普通聊天则在 task-local 目录镜像 personal workspace 全部 `uploaded/ready` 文档和可用 artifact。`openclaw_prepare.data.mirror_scope` 为 `attachments` 或 `workspace`；不可用对象会列在 `skipped_documents/skipped_artifacts`，而不是让 `documents/index.json` 静默为空。前端不需要、也不能直接访问该快照路径。

新会话的首条 prompt 是临时标题。回答成功后，后端使用固定低成本标题模型且关闭思考：用户消息存在明确主要语言时标题跟随该语言；内容过短、混合或难以判断时使用 `client_locale`。未显式提交 locale 时后端读取 `Accept-Language`，再回退到部署默认值。客户端只需在 task 终态后刷新会话列表；用户手动标题不会被自动覆盖。

OpenClaw 模型凭据和 provider base URL 由后端部署级 `.env` 管理。前端不提交、不保存、不展示 provider key 或 base URL，只使用以下 workspace 隔离接口：

```http
GET /workspaces/{workspace_id}/ai/models
PUT /workspaces/{workspace_id}/ai/model
Content-Type: application/json

{"model_id":"openai/model-id"}
```

`GET /ai/models` 返回 `items/default_model/selected_model/fetched_at/stale`。列表项的 `id` 是可回传的规范化模型 ID，前端不要自行拼接 provider 前缀。选择是用户全局偏好，会影响之后启动的聊天、知识库、题目提取、笔记、批改、高亮和闪卡任务；传 `{"model_id":null}` 恢复部署默认模型。已开始或正在重试的任务使用 `task.payload.ai_model` 快照，不会随选择变化。`stale=true` 表示 provider 暂时不可达，当前展示的是最后一次成功缓存，仍可使用其中模型。

```ts
type AiModel = {
  id: string;
  upstream_id: string;
  owned_by?: string | null;
  created?: number | null;
};

type AiModelCatalog = {
  provider: "openai";
  default_model: string;
  selected_model: string;
  items: AiModel[];
  fetched_at: string;
  stale: boolean;
};

async function selectAiModel(workspaceId: string, modelId: string | null) {
  return apiFetch(`/workspaces/${workspaceId}/ai/model`, {
    method: "PUT",
    body: JSON.stringify({ model_id: modelId }),
  });
}
```

后端向 OpenClaw 发送时，请求体仍使用 `model=openclaw`，provider 模型通过内部 `x-openclaw-model` header 传递。若后端未配置 key，task 会失败并在 `task.error_message` 中出现 `OPENAI_API_KEY` 配置提示；若 gateway 返回 HTTP 500，前端只展示失败原因，排查应看 task events 和后端 OpenClaw gateway/worker 日志。

任务成功后 `task.result` 常见字段：

```ts
type OpenClawTaskResult = {
  runner: "gateway";
  answer?: string;
  gateway_model?: string;
  provider_model?: string;
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

图片文档调用 `process` 后，worker 会先通过内网 DocTr 无状态推理服务生成 `deskewed_image.png` artifact，然后优先用该图片做 OCR；如果 DocTr 失败，后端会写 warning event 并 fallback 到原图。PDF 会在 worker 中渲染后 OCR；DOCX/PPTX 会先由内网 LibreOffice converter 转为 `converted_pdf` artifact，再进入同一 PDF OCR 流程。前端只需要轮询 task/events，不需要直接调用 DocTr、converter、OCR engine、SeaweedFS 或 OpenClaw。

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

以下接口都需要 `Authorization: Bearer <access_token>`，并严格限制在当前 personal workspace。权威 OpenAPI 为 `GET /api/v1/openapi.json`；局域网或 VPN 环境使用 `http://LAN_OR_VPN_HOST:8001/api/v1/openapi.json`。

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
  "updated_at": "2026-07-10T07:12:00Z",
  "latest_grading_result": null
}
```

`description/document_id/due_at/rubric_text` 可能为 `null`。该接口是真正的部分更新：省略字段会保持原值；显式传 `rubric_text: null` 或空白字符串才会清除 rubric；空对象返回 `422`；`max_score` 必须大于 `0`。更新配置会取消尚未完成的评分 task，前端应重新触发评分。

### Grading Results

`GET /workspaces/{workspace_id}/homeworks` 和 `GET /workspaces/{workspace_id}/homeworks/{homework_id}` 都包含 `latest_grading_result`。未评分时为 `null`；评分成功后包含 `score/max_score/grading_mode/confidence/feedback/created_at`，前端应从这里显示最新分数，而不是只读取 Homework 自身的 `max_score`。

完整评分历史：

```http
GET /workspaces/{workspace_id}/homeworks/{homework_id}/grading-results
```

返回按 `created_at` 倒序排列的 `GradingResultRead[]`。`grading_mode=provisional` 表示没有答案或 rubric 的诊断性评分；只有 `official` 才应显示为正式成绩。评分 task 成功后重新获取 Homework 详情或列表即可看到最新分数。

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
- DOCX/PPTX 会由内网 LibreOffice converter 转为 `converted_pdf` artifact，再进入 PDF OCR；转换损坏、超时或格式不受支持时任务会明确失败。
- AI 结果来自 OpenClaw skills，必须通过后端 schema 校验，但不代表结果天然绝对正确。
- 无答案或 rubric 的评分是 `provisional` 诊断性结果；只有存在评分依据时才显示为 `official`。
- 任务进度同时支持 SSE 与轮询；SSE 支持 `Last-Event-ID` 断线续传，旧客户端可继续轮询。

推荐 UI 处理：

- `uploaded`: 可以展示“开始处理”；自动流水线可能已经排队，避免重复提交。
- `scanning`: 仅在后端显式启用安全扫描时展示扫描进度。
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
409 资源状态冲突，例如 workspace 已存在、上传未完成、处理任务已运行或笔记版本过期
410 当前接口已禁用，常见于个人 workspace 下邀请成员
412 资料版本与 If-Match 不一致
413 上传或头像超过大小限制
422 请求体校验失败
429 触发登录、上传或 AI 限流
503 队列、存储、模型目录或其他后端依赖暂不可用
```

前端建议：

- `401`: refresh token，失败则跳登录
- `403`: 回 workspace 列表并提示无权访问
- `404`: 提示资源不存在或已删除
- `409`: 根据 `detail/code` 区分上传未完成、活动任务冲突、版本冲突等场景，不要统一自动重试
- `412`: 重新获取 profile 与 ETag，再让用户确认本次修改
- `429`: 遵循 `Retry-After`，不要立即循环重试
- `500/503`: 展示后端错误摘要；异步任务失败时同时读取 task events，OpenClaw/PaddleOCR/DocTr 失败通常不是前端渲染问题


## Aggregated Workflow Tracking

每次上传或手动重新处理都会产生一个 `WorkflowRun`。`upload-session` 返回顶层 `workflow_run_id`，Document 的 `latest_workflow_run_id` 指向最近一次运行。

```ts
type WorkflowRun = {
  id: string;
  workspace_id: string;
  document_id: string | null;
  learning_unit_id: string | null;
  trigger_type: "upload" | "manual_reprocess" | string;
  status: "waiting_upload" | "queued" | "running" | "waiting" | "succeeded" | "partially_succeeded" | "failed" | "cancelled";
  core_status: "not_started" | "queued" | "running" | "waiting" | "succeeded" | "failed" | "cancelled";
  enrichment_status: "not_started" | "queued" | "running" | "waiting" | "succeeded" | "failed" | "cancelled" | "not_applicable";
  current_stage: string | null;
  progress: number;
  waiting_until: string | null;
  error_message: string | null;
};
```

```http
GET /workspaces/{workspace_id}/workflows?page=1&page_size=50&status=waiting
GET /workspaces/{workspace_id}/workflows/{workflow_run_id}
GET /workspaces/{workspace_id}/workflows/{workflow_run_id}/events
GET /workspaces/{workspace_id}/workflows/{workflow_run_id}/events/stream
GET /workspaces/{workspace_id}/documents/{document_id}/workflow
```

详情响应为 `{ workflow, tasks }`，每个 task 项带 `stage/phase/required/task`。`core_status=succeeded` 表示 OCR、知识库或评分等核心结果已可用；增强流程失败时总状态是 `partially_succeeded`，不要把核心结果显示成整体失败。电子笔记在 OCR 和知识库完成后立即排队；`waiting_until` 仅用于 task 延迟重试或尚未完成的连续多图 NoteSet，不再表示固定 5 分钟防抖。

Workflow SSE 的事件名为 `workflow_event`，`data` 是完整 `WorkflowEvent`；使用 `sequence_no` 作为 `Last-Event-ID`。收到 `done` 后停止重连。鉴权、断线续传和 heartbeat 处理与 Task SSE 相同。

## Production Upload And Notes

文件安全扫描默认关闭。tus 上传完成后，文档会返回 `scan_status=skipped`，并直接进入 `uploaded`、`processing` 或 `ready`；客户端不得把 `skipped` 显示为“正在安全检查”。只有后端显式启用 ClamAV 且文档 `status=scanning` 时才展示扫描进度，此时 `scan_status=clean` 表示扫描通过。

未提供 `learning_unit_id` 时，后端会按“唯一精确匹配 → OCR 后高置信语义匹配 → 新建单元”的顺序自动归组。显式但无效或跨 workspace 的 `learning_unit_id` 返回 `404`，不会静默新建。手工合并学习单元使用：

```http
POST /workspaces/{workspace_id}/learning-units/{target_id}/merge
{"source_learning_unit_ids":["..."]}
```

接口返回异步 `TaskRead`，客户端按 Task Polling 章节跟踪。

展示学习笔记时，优先使用 `download_urls.rendered_html`，不要优先渲染原始 `html` 或 `highlighted_html`。该短期签名页面会选择可用的最新高亮 fragment，加载版本化 NotePatch paper CSS，并设置严格 CSP；过期后重新请求 URL。原始 HTML fragment 只供可信编辑器使用，客户端不要追加任意样式或执行其中内容。

富文本编辑器保存字号时必须提交受控 class，不要提交内联 `style`：

```html
<span class="np-font-size-24">文字</span>
```

后端只保留 `np-font-size-12/14/17/20/24/28/32/40`；`style="font-size: 24px"`、其他字号 class 和任意样式都会被 sanitizer 删除。手工 revision 保存后应重新读取服务端返回的笔记版本，不要继续使用未清洗的本地 HTML。加载主题时必须原样保留 `rendering.css_url` 的查询参数（例如 `?v=20260822-font-sizes`），不能去掉版本参数或自行拼接 CSS 地址，否则客户端可能继续命中不含字号规则的旧缓存。历史笔记不被改写；只有已保存受控 class 的新修订才能跨刷新和跨客户端保留字号。

## Public Random-Prefix Gateway

A public deployment may expose NotePatch below a deployment-only path such as `/np-<32-lowercase-hex>`. Clients must receive the exact prefix out of band; do not discover or hard-code the example value.

```bash
VITE_API_BASE_URL=https://PUBLIC_IP/np-<prefix>/api/v1
VITE_TUSD_BASE_URL=https://PUBLIC_IP/np-<prefix>/files/
```

The admin entry is `https://PUBLIC_IP/np-<prefix>/`. Its assets and deep links retain the same prefix. Task SSE uses the normal prefixed API URL. Signed object URLs intentionally use `https://PUBLIC_IP/notepatch/...` without the random prefix because SeaweedFS S3 signs the original Host, path, and query.

Requests to `/`, unprefixed `/api/v1`, or unprefixed `/files/` on the public TLS listener return `404`. Existing LAN/VPN clients may continue using the directly published ports. The random prefix is not authentication; clients must continue sending JWTs and must request short-lived download URLs from FastAPI.

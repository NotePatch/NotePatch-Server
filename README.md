# NotePatch Learning Backend

多用户学习软件后端 MVP。当前版本聚焦个人 workspace 下的文件管理基础设施：FastAPI 管业务 API 和权限，PostgreSQL 管 metadata，SeaweedFS 提供 S3-compatible 对象存储，tusd 提供弱网/大文件断点续传。

当前 worker 已接入真实学习流水线：图片先经 DocTr 矫正，PDF 由 PyMuPDF 渲染，PP-OCRv5/PP-StructureV3/PP-FormulaNet 生成结构化 OCR artifacts；OpenClaw skills 完成切题、知识整理、电子笔记、批改、高亮和闪卡；BGE-M3 与 pgvector 提供 workspace 隔离的语义检索。

## Stack

- FastAPI
- PostgreSQL 16 + pgvector
- SQLAlchemy 2.0
- Alembic
- Redis
- SeaweedFS S3-compatible API
- tusd resumable upload server
- DocTr image rectification
- PaddleOCR 3.7 GPU worker (PP-OCRv5 / PP-StructureV3 / PP-FormulaNet)
- BGE-M3 embedding service
- Per-user OpenClaw skills
- Docker Compose

## Monorepo Layout

```text
backend/src/notepatch/    FastAPI、worker、supervisor 与领域模块
backend/migrations/       Alembic migrations
backend/tests/            后端与架构契约测试
services/doctr/           无状态 DocTr GPU 服务与 vendored source
services/embedding/       BGE-M3 embedding 服务
web/admin/                Vite React 运维后台
openclaw/                 NotePatch skills、runtime 模板与镜像锁
infra/                    Dockerfile 与 SeaweedFS 配置
scripts/                  运维、迁移与清理工具
```

Python 包只有 `notepatch`、`doctr_service`、`embedding_service` 三个名称。运行数据不进仓库，统一位于 `${NOTEPATCH_DATA_ROOT:-/home/usr/notepatch-data}`。

## Quick Start

```bash
cp .env.example .env
docker compose --profile ocr up -d --build
docker compose exec api alembic upgrade head
```

从旧目录升级时，在启动新栈前额外执行：

```bash
python3 scripts/migrate_monorepo_runtime.py
python3 scripts/migrate_monorepo_runtime.py --apply
```

首次全量复制后，停掉旧 gateway 再用 `--apply --update-existing` 做最终增量同步。全新安装不需要运行该迁移脚本，只需把 DocTr 三个权重放到 `${NOTEPATCH_DATA_ROOT}/models/doctr`。

检查服务：

```bash
curl http://localhost:8001/health
```

默认端口：

- API: `http://localhost:8001/api/v1`
- Swagger: `http://localhost:8001/api/v1/docs`
- Admin Web: `http://localhost:5173`
- tusd: `http://localhost:1080/files/`
- SeaweedFS S3: `http://localhost:8333`
- SeaweedFS filer UI: `http://localhost:8888`
- SeaweedFS master UI: `http://localhost:9333`

前端接入说明见 [docs/frontend-integration.md](docs/frontend-integration.md)。

除 `/health` 外，本文中的业务路径均以 `http://HOST:8001/api/v1` 为 base URL；Android、Web 和管理后台不得再请求未版本化路径。

## Environment

核心配置在 `.env.example`：

```bash
DATABASE_URL=postgresql+psycopg://notepatch:notepatch@postgres:5432/notepatch
JWT_SECRET=change-me-in-production-use-at-least-32-bytes
ADMIN_EMAILS=
ADMIN_WEB_ORIGIN=http://localhost:5173
ADMIN_WEB_PORT=5173
VITE_API_BASE_URL=http://localhost:8001/api/v1
NOTEPATCH_DATA_ROOT=/home/usr/notepatch-data
REDIS_URL=redis://redis:6379/0
REDIS_TASK_QUEUE=notepatch:tasks
DEFAULT_QUEUE_NAME=default
OCR_QUEUE_NAME=ocr
CHAT_QUEUE_NAME=chat
WORKER_QUEUES=default
OCR_WORKER_QUEUES=ocr
CHAT_WORKER_QUEUES=chat

SEAWEEDFS_S3_ENDPOINT=http://seaweedfs-s3:8333
SEAWEEDFS_ACCESS_KEY=notepatch
SEAWEEDFS_SECRET_KEY=notepatch-secret
SEAWEEDFS_BUCKET=notepatch
SEAWEEDFS_PUBLIC_BASE_URL=http://localhost:8333

TUSD_BASE_URL=http://localhost:1080/files/
TUSD_INTERNAL_BASE_URL=http://tusd:1080/files/
TUSD_WEBHOOK_SECRET=change-me-tusd-webhook-secret
TUSD_DATA_DIR=/tusd-data
PURGE_TASK_MAX_ATTEMPTS=20
TASK_CANCELLATION_GRACE_SECONDS=600
PRESENCE_HEARTBEAT_INTERVAL_SECONDS=30
PRESENCE_SESSION_TTL_SECONDS=90
PRESENCE_OFFLINE_GRACE_SECONDS=600

OPENCLAW_GATEWAY_BASE_URL=http://host.docker.internal:18789
OPENCLAW_GATEWAY_TOKEN=
OPENCLAW_GATEWAY_MODEL=openclaw
OPENCLAW_AGENT_MODEL=openai/gpt-5.4
OPENCLAW_GATEWAY_TIMEOUT_SECONDS=120
OPENCLAW_GATEWAY_READY_TIMEOUT_SECONDS=30
OPENCLAW_GATEWAY_READY_POLL_SECONDS=2
OPENCLAW_GATEWAY_SCOPES=operator.write
OPENCLAW_ASSET_ROOT=/opt/notepatch/openclaw
OPENCLAW_USER_RUNTIME_ROOT=/home/usr/notepatch-data/openclaw
OPENCLAW_DOCKER_NETWORK=notepatch-server_default
OPENCLAW_USER_GATEWAY_IMAGE=openclaw-webui-node-docker:local
OPENCLAW_USER_GATEWAY_AUTOSTART=false
OPENCLAW_USER_GATEWAY_TOKEN_PREFIX=notepatch
OPENCLAW_USER_RUNTIME_UID=1000
OPENCLAW_USER_RUNTIME_GID=1000
OPENCLAW_SUPERVISOR_POLL_SECONDS=10
OPENCLAW_SUPERVISOR_CONTAINER_STOP_TIMEOUT_SECONDS=20
OPENAI_API_KEY=
# Optional OpenAI-compatible endpoint. Empty uses OpenAI's default API base URL.
OPENAI_BASE_URL=
AI_CHAT_HISTORY_MESSAGE_LIMIT=20

DOCTR_ENABLED=true
DOCTR_BASE_URL=http://docserver:8000
DOCTR_TIMEOUT_SECONDS=300
DOCTR_ILL_REC=true

OCR_ENGINE=paddleocr
OCR_TEMP_DIR=/tmp/ocr
OCR_MAX_PAGES=50
OCR_MAX_FILE_SIZE_MB=200
OCR_RENDER_DPI=200
OCR_SAVE_PAGE_IMAGES=false
OCR_ENABLE_PREPROCESS=true
OCR_ENABLE_LAYOUT=true
OCR_ENABLE_FORMULA=true
OCR_ENABLE_TABLE=true
OCR_WORKER_CONCURRENCY=1
OCR_TASK_TIMEOUT_SECONDS=300
PADDLEOCR_USE_GPU=true
PADDLEOCR_LANG=ch
PADDLEOCR_DET_MODEL_DIR=
PADDLEOCR_REC_MODEL_DIR=
PADDLEOCR_CLS_MODEL_DIR=
PADDLEOCR_STRUCTURE_MODEL=PP-StructureV3
PADDLEOCR_FORMULA_MODEL=PP-FormulaNet_plus-M

EMBEDDING_SERVICE_URL=http://embedding-service:8000
EMBEDDING_MODEL=BAAI/bge-m3
EMBEDDING_DIMENSION=1024

AUTO_LEARNING_PIPELINE=true
```

`SECRET_KEY` 仍兼容旧配置；新部署建议使用 `JWT_SECRET`。

## Web Admin

管理后台是运营与排障工具，位于 `web/admin/`，使用 Vite React。它不替代用户端，也不直接访问 SeaweedFS、Redis、DocTr 或 OpenClaw；所有读写都通过专用 FastAPI `/api/v1/admin/*` 接口并记录审计日志。

启用管理员账号：

```bash
ADMIN_EMAILS=ops@example.com,owner@example.com
```

管理员仍使用 `/api/v1/auth/login` 登录。只有登录用户邮箱在 `ADMIN_EMAILS` 中时，才能访问 `/api/v1/admin/*`；如果 `ADMIN_EMAILS` 为空，管理 API 默认返回 403。

启动后台：

```bash
docker compose up -d --build api admin-web
```

访问：

```text
http://localhost:5173
```

后台包含：

- 总览：用户、文档、任务、OCR artifact 和队列摘要。
- 用户：全局用户搜索、personal workspace 和数据计数。
- 文档：跨 workspace 文档检索、artifact metadata、原文件和 artifact download-url。
- 任务：全局 task 查询、payload/result、task_events 时间线。
- 学习：学习单元、知识块、HTML 笔记版本、安全预览、富文本编辑和加权闪卡。
- 作业与错题：评分配置、references、触发批改、状态维护。
- 会话：查看消息与软删除，不允许管理员冒充用户发送消息。
- 运营操作：创建/编辑/禁用/重置/物理删除用户，文档 purge、任务取消/重试及完整审计。
- 系统：Redis queue、DB、SeaweedFS、DocTr、OpenClaw 健康状态。

管理员创建或重置用户后只返回一次临时密码。用户登录后必须调用 `POST /api/v1/auth/change-password`，完成前其他业务接口返回 `403 Password change required`。物理删除用户是异步操作，可在 `/api/v1/admin/operations` 查询阶段和错误。

前端生产构建验证：

```bash
docker compose build admin-web
docker run --rm notepatch-server-admin-web npm run build
```

## Database

执行迁移：

```bash
docker compose exec api alembic upgrade head
```

本次文件管理迁移会：

- 将 document 字段迁到 `uploaded_by/original_filename/mime_type/file_size/object_key`
- 增加 `storage_backend/bucket/upload_id/tus_upload_url/file_type/document_kind`
- 增加 `upload_sessions`
- 将旧 `processed` document 状态迁为 `ready`

个人 workspace 迁移会：

- 将所有 workspace 统一为 `personal`
- 每个用户只保留一个 owner personal workspace
- 合并同一 owner 的重复 workspace metadata，不移动 SeaweedFS 对象
- 删除非 owner membership，并给 `workspaces.owner_user_id` 加唯一约束

## Auth And Workspace

注册：

```bash
curl -s http://localhost:8001/api/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"alice@example.com","password":"password123","full_name":"Alice"}'
```

登录：

```bash
curl -s http://localhost:8001/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"alice@example.com","password":"password123"}'
```

refresh token 使用 family 轮换，并通过 `REFRESH_TOKEN_ROTATION_GRACE_SECONDS`（默认 10 秒）容忍同一客户端的短暂并发刷新。客户端仍必须使用 single-flight，避免多个 401 同时发起 refresh；logout 会撤销当前登录 family，修改密码或禁用账号会撤销该用户全部 refresh token。

当前用户：

```bash
curl -s http://localhost:8001/api/v1/auth/me \
  -H "Authorization: Bearer $TOKEN"
```

在线心跳：

```bash
curl -s http://localhost:8001/api/v1/presence/heartbeat \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"client_id":"browser-tab-1"}'
```

前端登录后每 30 秒调用一次 heartbeat。停止心跳超过 10 分钟后，`openclaw-supervisor` 会停止该用户 gateway 容器；多端登录时任一客户端保持心跳，容器就保持运行。

查看个人 workspace：

```bash
curl -s http://localhost:8001/api/v1/workspaces \
  -H "Authorization: Bearer $TOKEN"
```

注册会自动创建个人 workspace。`POST /workspaces` 只作为异常恢复接口：如果用户已经有个人 workspace，会返回 `409 Personal workspace already exists`；如果缺失，则创建一个新的 `personal` workspace。

```bash
curl -s http://localhost:8001/api/v1/workspaces \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"Alice Workspace"}'
```

## tusd Upload Flow

后端不直接接收大文件。客户端先向 FastAPI 创建上传会话，再用 tus 协议上传到 tusd。

创建 upload session：

```bash
export WORKSPACE_ID='paste-workspace-id'

curl -s http://localhost:8001/api/v1/workspaces/$WORKSPACE_ID/documents/upload-session \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "filename":"../exam paper.pdf",
    "mime_type":"application/pdf",
    "file_size":12345,
    "document_kind":"exam",
    "title":"期中试卷",
    "metadata":{"subject":"math","grade_level":"grade_7"}
  }'
```

返回包含：

- `document.id`
- `upload_session.id`
- `tus_endpoint`
- `tus_metadata`
- `tus_metadata_header`
- `bucket`
- `object_key`

前端使用 tus client 上传时，将：

- endpoint 设为 `tus_endpoint`
- metadata 设为 `tus_metadata`

tusd 上传完成后会调用：

```http
POST /webhooks/tusd?secret=<TUSD_WEBHOOK_SECRET>
```

FastAPI 会校验 metadata 中的 `upload_session_id/document_id/upload_token`，然后从共享 volume 读取 tusd 文件并写入 SeaweedFS S3，最后：

- `upload_sessions.status = completed`
- `documents.status = uploaded`
- 创建 `document_artifacts.artifact_type = original`

客户端也可以主动通知后端完成上传：

```bash
curl -s http://localhost:8001/api/v1/workspaces/$WORKSPACE_ID/documents/complete-upload \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"upload_session_id":"paste-upload-session-id","tus_upload_id":"paste-tusd-upload-id"}'
```

如果 tusd 文件尚未完成，会返回 `409 Upload not finished`。

## Documents And Artifacts

列表，支持分页和过滤：

```bash
curl -s 'http://localhost:8001/api/v1/workspaces/$WORKSPACE_ID/documents?page=1&page_size=20&document_kind=exam&file_type=pdf' \
  -H "Authorization: Bearer $TOKEN"
```

详情：

```bash
curl -s http://localhost:8001/api/v1/workspaces/$WORKSPACE_ID/documents/$DOCUMENT_ID \
  -H "Authorization: Bearer $TOKEN"
```

下载 URL：

```bash
curl -s http://localhost:8001/api/v1/workspaces/$WORKSPACE_ID/documents/$DOCUMENT_ID/download-url \
  -H "Authorization: Bearer $TOKEN"
```

artifact 下载 URL：

```bash
curl -s http://localhost:8001/api/v1/workspaces/$WORKSPACE_ID/documents/$DOCUMENT_ID/artifacts/$ARTIFACT_ID/download-url \
  -H "Authorization: Bearer $TOKEN"
```

查询 OCR artifacts，可按需附带短期下载 URL：

```bash
curl -s 'http://localhost:8001/api/v1/workspaces/'"$WORKSPACE_ID"'/documents/'"$DOCUMENT_ID"'/ocr?include_download_url=true' \
  -H "Authorization: Bearer $TOKEN"
```

删除文档：

```bash
curl -X DELETE http://localhost:8001/api/v1/workspaces/$WORKSPACE_ID/documents/$DOCUMENT_ID \
  -H "Authorization: Bearer $TOKEN"
```

接口返回 `202 Accepted`，文档会立即从普通读取接口隐藏，并返回异步 purge task：

```json
{
  "ok": true,
  "document_id": "...",
  "status": "deleted",
  "purge_status": "queued",
  "purge_task_id": "..."
}
```

客户端用现有 task/events 接口轮询 `purge_task_id`。清理会取消上传和关联任务，删除 SeaweedFS 原件、派生内容及关联业务数据，并基于同学习单元剩余资料重建笔记、闪卡和必要评分。重复 DELETE 会复用正在执行或已完成的 purge task；失败后重复 DELETE 会创建重试任务。

给 OCR/预处理 worker 写 artifact metadata：

```bash
curl -s http://localhost:8001/api/v1/workspaces/$WORKSPACE_ID/documents/$DOCUMENT_ID/artifacts \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "artifact_type":"ocr_json",
    "object_key":"workspaces/'"$WORKSPACE_ID"'/documents/'"$DOCUMENT_ID"'/artifacts/artifact-id/ocr_json.json",
    "mime_type":"application/json",
    "metadata":{"engine":{"ocr":"pp-structure-v3","formula":"PP-FormulaNet_plus-M"}}
  }'
```

该接口只接受当前配置的 SeaweedFS bucket，且 `object_key` 对应对象必须已经存在；它不能用于创建悬空 metadata。

## Object Key Layout

数据库只保存 metadata、bucket 和 object key，真实文件在 SeaweedFS：

```text
workspaces/{workspace_id}/documents/{document_id}/original/{safe_filename}
workspaces/{workspace_id}/documents/{document_id}/artifacts/{artifact_id}/{artifact_type}.{ext}
workspaces/{workspace_id}/sandbox/tasks/{task_id}/input/...
workspaces/{workspace_id}/sandbox/tasks/{task_id}/output/...
```

用户传入文件名只用于生成安全文件名，不会直接作为路径。路径会移除 `../`、绝对路径、反斜杠和控制字符。

## Permissions

所有 workspace API 都会先检查当前用户是该 personal workspace 的 owner。所有 document/artifact 查询都带 `workspace_id`：

```sql
WHERE documents.id = :document_id
  AND documents.workspace_id = :workspace_id
```

当前版本不支持 family/class/school workspace，也不支持邀请成员。`POST /workspaces/{workspace_id}/members` 固定返回 `410 Workspace members are disabled for personal workspaces`。

访问其他用户的 workspace 返回 `403`；在自己 workspace 下猜其他 workspace 的 document id 返回 `404`。

## SeaweedFS And tusd Notes

当前 Compose 用 tusd 本地磁盘 backend：

```text
tusd /data volume -> api /tusd-data:ro -> SeaweedFS S3
```

这是为了让 MVP 清晰、稳定。后续可把 tusd 切换为 S3 backend，直接写 SeaweedFS 的同一个 bucket；FastAPI webhook 仍保留 metadata 校验、状态更新和 artifact 幂等逻辑。

## OpenClaw Gateway Runner

`POST /workspaces/{workspace_id}/ai/chat` 是唯一的 AI 对话入口，会创建 `openclaw_agent_run` 异步任务。请求体使用 `prompt`、可选 `conversation_id`、可选 `input` 和可选 `options`；不传 `conversation_id` 时会自动创建会话。响应 task payload 包含 conversation 和 message id，客户端通过 task 状态、events 以及会话消息接口获取最终 answer 或失败原因。注册用户时，notepatch 会为该用户生成一套独立 OpenClaw runtime 配置：

聊天历史保存在 PostgreSQL，可通过以下接口管理：

```text
GET    /workspaces/{workspace_id}/ai/conversations
GET    /workspaces/{workspace_id}/ai/conversations/{conversation_id}
GET    /workspaces/{workspace_id}/ai/conversations/{conversation_id}/messages
PATCH  /workspaces/{workspace_id}/ai/conversations/{conversation_id}
DELETE /workspaces/{workspace_id}/ai/conversations/{conversation_id}
PATCH  /auth/preferences {"ai_history_enabled": true|false}
```

`ai_history_enabled` 是用户全局开关，默认开启。关闭后历史仍可回看，但 worker 不会把它带入后续 OpenClaw 请求；开启时每次最多传入 `AI_CHAT_HISTORY_MESSAGE_LIMIT`（默认 20）条成功消息。它不写入或修改 OpenClaw `MEMORY.md`，后者仍是独立的 agent 长期记忆机制。

```text
/home/usr/notepatch-data/openclaw/users/{user_id}/
  home/.openclaw/openclaw.json
  home/.openclaw/agents/main/agent/auth-profiles.json
  notepatch-runtime.json
  workspace/skills/{skill_name}/SKILL.md
  workspace/notepatch/documents/
  workspace/notepatch/openclaw/tasks/
  docker-compose.yml
  .env
```

MVP 使用部署级共享 OpenAI provider key：在 notepatch `.env` 配置 `OPENAI_API_KEY`，supervisor 会把它作为环境变量注入每个用户 gateway 容器。`OPENCLAW_GATEWAY_MODEL` 是调用 gateway `/v1/chat/completions` 的模型名，必须是 `openclaw` 或 `openclaw/<agentId>`；`OPENCLAW_AGENT_MODEL` 才是写入 per-user `openclaw.json` 的 provider 模型，例如 `openai/gpt-5.4`。真实 key 不会写入每用户 `.env`、`openclaw.json` 或 `auth-profiles.json`；auth profile 只保存 `OPENAI_API_KEY` 的 env secret reference。若使用 OpenAI-compatible 代理或私有 endpoint，可配置 `OPENAI_BASE_URL=https://proxy.example.com/v1`，notepatch 会写入每用户 `openclaw.json` 的 `models.providers.openai.baseUrl`；空值表示使用 OpenClaw/OpenAI 默认 endpoint。若缺少 `OPENAI_API_KEY`，OpenClaw task 会在调用 gateway 前失败，并在 `task.error_message` 中提示配置后重启 `worker` 和 `openclaw-supervisor`。

每用户 gateway 默认由 `openclaw-supervisor` 按在线状态自动启动和停止，不暴露宿主端口，只加入 `notepatch-server_default` 网络。API/worker 不挂 Docker socket；只有 supervisor 挂 `/var/run/docker.sock`。supervisor 会自动读取 Docker socket 的 gid，并通过 `group_add` 授权用户 gateway 内的 `node` 用户访问 Docker API；如部署环境需要固定值，可设置 `OPENCLAW_DOCKER_SOCKET_GID=125` 一类的覆盖项。运维也可按用户手动启动：

生成 runtime 时会按 `OPENCLAW_USER_RUNTIME_UID/GID` 修正目录 ownership；默认值 `1000:1000` 对应当前 OpenClaw 镜像里的 `node` 用户。如果替换 gateway 镜像，请同步调整这两个值。

```bash
set -a
. /home/usr/notepatch-server/.env
set +a
docker compose \
  --env-file /home/usr/notepatch-data/openclaw/users/{user_id}/.env \
  -f /home/usr/notepatch-data/openclaw/users/{user_id}/docker-compose.yml \
  up -d
```

部署新增或变更 `OPENAI_API_KEY`、模型或 runtime 配置后，在线用户 gateway 会因 config hash 变化由 supervisor 重建。也可以手动停止并删除旧 `notepatch-openclaw-{user_id}` 容器，让下一次 heartbeat 触发重建。

历史用户可补齐配置：

```bash
docker compose exec api python /opt/notepatch/scripts/provision_openclaw_users.py
```

worker 运行 openclaw task 前，会把该用户 personal workspace 下 `uploaded/ready` 且 object key 仍属于当前 workspace 的 documents/artifacts 从 SeaweedFS 同步到任务独立快照：

```text
workspace/notepatch/openclaw/tasks/{task_id}/input/documents/
workspace/notepatch/openclaw/tasks/{task_id}/input/documents/index.json
```

每个 chat/skill task 使用自己的镜像目录，任务之间不会删除或覆盖彼此正在读取的文件。

未完成上传、旧 workspace object key、缺少 `file_size`、或对象存储返回 404 的记录会被跳过并写入 `index.json` 的 `skipped_documents/skipped_artifacts`，同时出现在 `openclaw_prepare` task event 中；单个坏对象不会拖垮整次 OpenClaw task。非 404 存储错误仍会让任务失败。

清理历史坏 document metadata 可先 dry-run：

```bash
docker compose exec api python /opt/notepatch/scripts/cleanup_invalid_documents.py \
  --workspace-id b3f4f628-b0e8-4737-b6b1-35480e6d5e1b \
  --old-object-key-workspace-id 33198cfd \
  --older-than-minutes 30
```

确认候选记录无误后再物理删除数据库 metadata：

```bash
docker compose exec api python /opt/notepatch/scripts/cleanup_invalid_documents.py \
  --workspace-id b3f4f628-b0e8-4737-b6b1-35480e6d5e1b \
  --old-object-key-workspace-id 33198cfd \
  --older-than-minutes 30 \
  --apply
```

然后调用该用户容器内网地址 `http://notepatch-openclaw-{user_id}:18789/v1/chat/completions`，并要求 OpenClaw 将任务产物写到：

```text
/workspace/notepatch/openclaw/tasks/{task_id}/output/
```

任务完成后，notepatch 会收集该 output 目录并上传回 SeaweedFS：

```text
workspaces/{workspace_id}/sandbox/tasks/{task_id}/output/...
```

如果该用户刚上线但 gateway 还在启动，runner 会先轮询 `/healthz` 等待 ready。若 gateway 未能启动、token 不正确、provider key 缺失、返回非 2xx 或 skill 输出不符合 schema，任务会按指数退避最多重试 3 次，错误与 attempt 都写入 task events。用户离线超过 10 分钟后 supervisor 会停止 gateway；只要仍有 queued/running 的聊天、切题、知识库、笔记、批改、高亮或闪卡任务，supervisor 就会启动并保活对应容器。

生产环境只提供 Gateway runner。单元测试通过依赖注入使用 `tests/fakes.py`，不会在生产代码中生成替代结果。

## DocTr Image Preprocessing

`POST /workspaces/{workspace_id}/documents/{document_id}/process` 对 `file_type=image` 的文档会先调用内网 DocTr 服务做几何矫正和光照校正，输出 `deskewed_image.png` artifact。DocTr 只做图片预处理，不做 OCR；如果 DocTr 失败，worker 会写 warning event 并 fallback 到原图继续 OCR。

notepatch Compose 会从仓库内 `services/doctr` 构建 `docserver` 内网服务，不暴露宿主端口。docserver 是无状态推理服务，只提供 `POST /v1/rectify` 和 `GET /healthz`：不认证、不保存业务图片、不维护用户或任务数据库，请求期间只使用临时目录，响应完成后清理。notepatch 仍是唯一的认证、权限、metadata、任务和 SeaweedFS artifact 来源。

该服务需要 NVIDIA runtime/GPU，并挂载权重：

```text
/home/usr/notepatch-data/models/doctr/seg.pth
/home/usr/notepatch-data/models/doctr/geotr.pth
/home/usr/notepatch-data/models/doctr/illtr.pth
```

排查：

```bash
docker compose exec docserver curl -s http://localhost:8000/healthz
docker compose logs -f docserver worker
```

`/healthz` 会返回 `weights_ready` 和 `missing_weights`。worker 会下载 SeaweedFS 原图，调用 docserver `/v1/rectify` 同步拿到 PNG，再上传为 `deskewed_image` artifact，随后优先使用该 PNG 作为 OCR 输入。

若要跳过 DocTr、直接对原图运行真实 OCR，可设置：

```bash
DOCTR_ENABLED=false
```

## Worker Queues

后端仍使用现有 Redis worker 架构，不新增第二套任务系统。现在按逻辑 queue 拆分消费面：

- `default` queue 使用 Redis key `notepatch:tasks`，普通 `worker` 默认只消费它。
- `ocr` queue 使用 Redis key `notepatch:tasks:ocr`，GPU `ocr-worker` 只消费它。
- `chat` queue 使用 Redis key `notepatch:tasks:chat`，常驻 `chat-worker` 独立消费交互式 OpenClaw 对话，避免被耗时学习任务阻塞。
- `ocr_document` 和 `document_processing_pipeline` 都进入 `ocr` queue，普通 worker 不会加载 PaddleOCR。
- `openclaw_agent_run` 进入 `chat` queue。
- `extract_questions`、`grade_homework`、`build_knowledge_base`、`generate_study_notes`、`generate_flashcards`、`highlight_study_notes` 进入 `default` queue。
- 可恢复错误进入 Redis delayed-retry zset，按指数退避重新投递；不会阻塞 worker loop。

worker 可用 CLI 或 env 指定队列：

```bash
python -m notepatch.entrypoints.worker --queues default
python -m notepatch.entrypoints.worker --queues ocr
python -m notepatch.entrypoints.worker --queues chat
WORKER_QUEUES=default python -m notepatch.entrypoints.worker
```

如果任务被误投到错误 Redis list，worker 会把它放回该 task type 对应的队列，不会直接处理。

## Automatic Learning Workflow

`AUTO_LEARNING_PIPELINE=true` 时，上传完成后会自动创建 `document_processing_pipeline` task。整个流程继续使用现有 worker/Task/Event 架构：

1. 用户上传课件、笔记、试卷或作业，文件内容进入 SeaweedFS，数据库只保存 metadata。
2. 上传完成后进入 GPU OCR queue：图片先尝试 DocTr，PDF 渲染后由 PP-StructureV3 执行文字、版面、表格和公式识别，输出六类 OCR artifacts。
3. `courseware/note/other` 经 `notepatch_kb_builder` 独立更新知识库；同一学习单元最后一次知识更新 5 分钟后，才开始执行 HTML 学霸笔记，笔记成功后再生成持久化闪卡。
4. `homework/corrected_homework/exam` 先经 `notepatch_question_extractor` 生成真实题目。作业随后执行 grading skill。
5. `answer_key/rubric` 只完成 OCR，作为 Homework references 的评分依据，不自动生成普通笔记。
6. 有答案或 rubric 时为 `official` 评分；没有依据时必须为 `provisional` 诊断性评分并带置信度。
7. 错题会写入 mistakes 和带 BGE-M3 embedding 的知识块，再由 `notepatch_note_highlighter` 更新电子笔记。

学习单元按 document metadata 自动归类：优先使用 `learning_unit_id`，否则使用 `learning_unit_title`、`topic`、`subject`，最后落到“未归类学习单元”。当前是个人 workspace-only，所有 learning tables 都带 `workspace_id`。

查询结果：

```http
GET /workspaces/{workspace_id}/learning-units
GET /workspaces/{workspace_id}/learning-units/{learning_unit_id}
GET /workspaces/{workspace_id}/learning-units/{learning_unit_id}/knowledge-chunks
GET /workspaces/{workspace_id}/learning-units/{learning_unit_id}/notes?include_download_url=true
GET /workspaces/{workspace_id}/learning-units/{learning_unit_id}/notes/{note_version_id}/download-url?kind=html
POST /workspaces/{workspace_id}/learning-units/{learning_unit_id}/notes/{latest_note_version_id}/revisions
GET /workspaces/{workspace_id}/learning-units/{learning_unit_id}/flashcard-decks
GET /workspaces/{workspace_id}/learning-units/{learning_unit_id}/flashcard-decks/latest
GET /workspaces/{workspace_id}/learning-units/{learning_unit_id}/flashcard-decks/{deck_id}
```

5 分钟是知识更新防抖窗口，不是笔记完成时限。到期后 `generate_study_notes` 还需要调用用户 OpenClaw gateway、校验结构化输出、清洗 HTML 并写入 SeaweedFS；可恢复错误最多重试 3 次，因此实际完成时间可能超过 5 分钟。客户端应按以下字段判断：

- `knowledge_revision > notes_generated_revision`：最新知识尚未生成笔记，应继续轮询。
- `note_generation_due_at != null`：笔记已安排或仍在生成，不应显示为永久无笔记。
- `knowledge_revision == notes_generated_revision` 且 notes 列表存在最新版本：本轮生成完成。

建议每 5-10 秒刷新 learning unit 和 notes，不要在防抖时间刚结束时停止等待。长时间未追平时，通过管理后台 task/events 检查 `generate_study_notes` 的 attempt 和错误；gateway 返回成功但未创建 `study_note.json` 时，worker 会记录错误并自动重试。

笔记是经过后端白名单清洗的 HTML fragment，禁止 script、iframe、事件属性、任意 style 和外部资源。修订请求使用 `{ "html": "<article>...</article>", "title": "...", "edit_summary": "..." }`。手动编辑总是创建递增版本；AI 高亮只更新最新版本关联的 `highlighted_html` artifact，不创建版本。管理后台使用富文本编辑器，Android 应使用受控 HTML 渲染。

知识点答题历史同时记录正确、部分正确和错误。闪卡优先级由后端确定性计算：错误次数与近期错误提高权重，时间会衰减，近期连续答对通过 success pressure 和 streak multiplier 降低旧错题权重。OpenClaw 只生成卡片文本；卡组、卡片、权重和权重因子持久化在 PostgreSQL。

首次升级到 HTML 笔记前先 dry-run，再执行清理和自动重建：

```bash
docker compose exec api python /opt/notepatch/scripts/reset_study_notes_to_html.py
docker compose exec api python /opt/notepatch/scripts/reset_study_notes_to_html.py --apply
```

该脚本永久删除旧 Markdown 笔记、高亮和闪卡输出，但保留原始文档、OCR 与仍存在的有效知识块，并安排 5 分钟后的 HTML 笔记生成。若某学习单元的历史知识块此前已经被清理，脚本不会凭空重建笔记；需要重新处理对应课件/笔记，使 `build_knowledge_base` 先恢复知识块。

后台会为每个用户 OpenClaw runtime 写入内置 skill 说明：

```text
notepatch_question_extractor
notepatch_kb_builder
notepatch_scholar_notes
notepatch_grading
notepatch_note_highlighter
notepatch_flashcards
```

每个 skill 安装在用户 `workspace/skills/`，因此启用 workspace-only 文件权限的 OpenClaw sandbox 可以读取，且不会越过 `/workspace` 边界。skill 使用固定 `input.json` 与输出文件、稳定 session key，并在输入中携带后端生成的严格 Pydantic JSON Schema。首次结构错误会在同一 session 请求校正；仍不合法则任务重试，绝不落库替代内容。Agent 仅允许读写用户 workspace，禁用 shell、浏览器和外部网络。

## OCR Pipeline

OCR 运行在现有异步 worker 内，不在 API 请求线程里执行。`document_processing_pipeline` 当前流程：

1. 下载原始 document。
2. 图片文档先尝试 DocTr，成功后用 `deskewed_image` OCR，失败则 fallback 原图。
3. PDF 用 PyMuPDF 按 `OCR_RENDER_DPI` 渲染，页数超过 `OCR_MAX_PAGES` 会失败。
4. 调用 PP-OCRv5、PP-StructureV3 和 PP-FormulaNet，生成稳定 OCR JSON、Markdown、纯文本、layout、formula 和 tables 结果。
5. 上传 `ocr_json`、`ocr_markdown`、`ocr_text`、`layout_json`、`formula_json`、`tables_json` artifacts。
6. 按 document kind 调度真实 OpenClaw skills。

生产不提供 OCR fallback。PaddleOCR/CUDA/模型不可用时任务进入重试，耗尽 3 次后明确失败，不生成假 OCR。

启动基础 API、普通 worker 与真实 OCR worker：

```bash
docker compose up -d --build api worker ocr-worker embedding-service
```

基础 API/普通 worker 不安装 PaddleOCR。`ocr-worker` 使用 `paddlepaddle/paddle:3.3.1-gpu-cuda12.6-cudnn9.5`、`paddleocr==3.7.0` 和固定的 `paddlex[ocr]` runtime；启动预检会验证 Paddle CUDA、OpenCV 和 PP-Structure 模型。

只重建真实 OCR worker：

```bash
docker compose --profile ocr up -d --build ocr-worker
```

手动 smoke，不纳入默认测试：

```bash
docker compose exec ocr-worker python /opt/notepatch/scripts/ocr_smoke_test.py /path/to/sample.png
```

`ocr-worker` 使用 `paddleocr-cache:/models/paddlex` 持久化模型；`embedding-service` 使用 `bge-cache`。DocTr、PaddleOCR 和 BGE-M3 通过 Redis GPU lease 全局串行，lease 带 token、TTL 和自动续租。`OCR_WORKER_CONCURRENCY=1`，单卡部署不要横向扩容 GPU worker。

DOCX/PPTX 第一版不会直接 OCR，会失败并提示先转换为 PDF 或图片。后续可在 `DocumentConverter` 接口中接 LibreOffice 转换。

知识检索与评分依据接口：

```http
POST   /workspaces/{workspace_id}/knowledge/search
PATCH  /workspaces/{workspace_id}/homeworks/{homework_id}/grading-config
GET    /workspaces/{workspace_id}/homeworks/{homework_id}/references
POST   /workspaces/{workspace_id}/homeworks/{homework_id}/references
DELETE /workspaces/{workspace_id}/homeworks/{homework_id}/references/{reference_id}
```

`grading-config` 是真正的部分更新：只修改请求中出现的字段，`{"rubric_text": null}` 才清空 rubric，空对象返回 `422`。修改 grading config 或增删 reference 会协作取消当前 queued/running 评分任务，避免旧配置结果落库。

历史替代产物清理脚本默认 dry-run；`--apply` 会先执行 `pg_dump`，再删除已标记的旧 OCR/questions/knowledge/grading/notes 对象和 metadata，将受影响文档重置并重新排入真实流水线：

```bash
docker compose exec api python /opt/notepatch/scripts/cleanup_generated_placeholder_data.py
docker compose exec api python /opt/notepatch/scripts/cleanup_generated_placeholder_data.py \
  --backup-dir /tmp/notepatch-backups --apply
```

升级到异步文档 purge 后，可用下面的脚本检查并回填历史上已经标记 `deleted`、但尚未彻底清理的记录：

```bash
docker compose exec api python /opt/notepatch/scripts/backfill_document_purges.py
docker compose exec api python /opt/notepatch/scripts/backfill_document_purges.py --apply
```

OpenClaw 文档镜像会优先把 `ocr_markdown` 和 `ocr_text` 同步到任务快照的：

```text
workspace/notepatch/openclaw/tasks/{task_id}/input/documents/{document_id}/ocr/ocr.md
workspace/notepatch/openclaw/tasks/{task_id}/input/documents/{document_id}/ocr/ocr.txt
```

并在 `index.json` 中写入 `ocr_markdown_path` / `ocr_text_path`，方便 OpenClaw 直接读取 OCR 文本。

## Local Tests

测试使用 SQLite 和 `tests/fakes.py` 的显式依赖注入，不下载模型，也不会改变生产 runner/engine 行为。

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
pytest
```
# NotePatch-Server

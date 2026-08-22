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

第三方许可证见 `services/doctr/THIRD_PARTY.md`。特别注意：vendored DocTr 当前许可证仅允许非商业使用；商业部署前必须取得上游作者的书面许可。NotePatch 仓库自身的发布许可证需由仓库所有者另行确定。

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
- SeaweedFS filer/master: Compose 内网服务，不发布宿主端口

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
AI_QUEUE_NAME=ai
WORKER_QUEUES=default
OCR_WORKER_QUEUES=ocr
CHAT_WORKER_QUEUES=chat
AI_WORKER_QUEUES=ai

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
OPENCLAW_SKILL_TIMEOUT_SECONDS=300
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
AI_CHAT_AUTO_TITLE_ENABLED=true
AI_CHAT_TITLE_MODEL=openai/gpt-5.4-mini
AI_CHAT_TITLE_FALLBACK_LOCALE=zh-CN
AI_CHAT_TITLE_MESSAGE_LIMIT=6
AI_CHAT_TITLE_MAX_LENGTH=40
AI_CHAT_TITLE_TIMEOUT_SECONDS=30
AI_IMAGE_REMARK_ENABLED=true
AI_IMAGE_REMARK_MODEL=openai/gpt-5.6-luna
AI_IMAGE_REMARK_MAX_LENGTH=24
AI_IMAGE_REMARK_TIMEOUT_SECONDS=60

OpenClaw Gateway 的 sandbox 会通过宿主 Docker daemon 创建隔离容器，因此 Gateway 镜像必须同时包含 Docker CLI。统一使用仓库脚本构建，避免出现 Gateway HTTP 200 但 SSE 无回答：

```bash
OPENCLAW_SOURCE_DIR=/home/usr/openclaw ./scripts/build_openclaw_gateway.sh
docker compose up -d openclaw-supervisor chat-worker
```

脚本固定传入 `OPENCLAW_INSTALL_DOCKER_CLI=1`，并在构建后执行 `docker --version` 预检。仅挂载 `/var/run/docker.sock` 不够；镜像缺少 CLI 时 OpenClaw 会在启动 sandbox 前失败。

DOCTR_ENABLED=true
DOCTR_BASE_URL=http://docserver:8000
DOCTR_TIMEOUT_SECONDS=300
DOCTR_ILL_REC=false

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

用户资料使用独立的版本化写接口：

```http
GET    /api/v1/user/profile
PUT    /api/v1/user/profile
POST   /api/v1/user/avatar/upload
GET    /api/v1/user/avatar/download-url
GET    /api/v1/user/avatar/content
DELETE /api/v1/user/avatar
```

`GET /user/profile` 返回 `ETag: "profile-{profile_version}"`。所有资料和头像写请求必须回传该值作为 `If-Match`，并提供 8-128 字符的 `Idempotency-Key`。同一 key 和相同请求会复用原结果；同一 key 配不同内容返回 `409`；资料版本已变化返回 `412`。邮箱变更还必须提交 `current_password`，成功后旧 access/refresh token 全部失效，客户端应清空 token 并重新登录。头像只接受实际可解码的 JPEG/PNG，默认最大 5 MB；默认写入 SeaweedFS，`AVATAR_STORAGE_BACKEND=local` 时写入 `${NOTEPATCH_DATA_ROOT}/avatars`。

相关部署配置：

```env
IDENTITY_IDEMPOTENCY_TTL_SECONDS=86400
AVATAR_STORAGE_BACKEND=seaweedfs
AVATAR_LOCAL_ROOT=
USER_AVATAR_MAX_SIZE_MB=5
USER_AVATAR_MAX_DIMENSION=4096
```

部署该功能前必须执行 `alembic upgrade head`，使数据库达到当前 head revision `202608220002`。头像替换后若旧对象暂时无法删除，后端会把 `purge_avatar_object` 放入 default queue 幂等重试，不影响新资料生效。

这些新接口返回统一 envelope：`{"code":"ok","message":"...","data":{...}}`。现有 task、document 和 chat 创建接口保持原响应结构。

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
POST /api/v1/webhooks/tusd?secret=<TUSD_WEBHOOK_SECRET>
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

图片备注与 `original_filename`、`title` 相互独立。用户偏好 `auto_image_remark_enabled=true` 且 upload-session 未提交 `remark` 时，后端先完成真实 OCR，再创建 AI queue 中的 `generate_image_remark` 任务。任务固定使用 `AI_IMAGE_REMARK_MODEL`（默认 `openai/gpt-5.6-luna`）与 `minimal` 思考强度，只把 `ocr_text` 发给模型，不发送原图或 DocTr 图片。AI 返回的是约 2–4 个词的短资料标签而不是摘要，默认硬上限 24 字符。语言优先遵守用户 `response_language`；`match_user` 才跟随 OCR，`client_locale` 使用 upload-session 的可选 BCP 47 `client_locale`，缺失时跟随 OCR而不是默认中文。OCR 无文本、开关关闭或全局功能关闭时，`remark` 使用原文件名。显式上传备注或随后通过 `PATCH /workspaces/{workspace_id}/documents/{document_id}` 编辑的备注始终优先，晚到的 AI 结果不得覆盖。旧 `ai_image_*` 响应字段仅作为兼容别名；新客户端使用 `remark/remark_source/image_remark_status/image_remark_task_id`。


公网反向代理必须让 tusd 返回的 `Location` 保留 HTTPS 与随机公开前缀：
`https://PUBLIC_IP/np-<prefix>/files/{upload_id}`。仓库中的 Nginx 模板会重写该响应头，
同时 tusd 以 `-behind-proxy` 启动。若 upload info 显示 `Size > 0`、`Offset = 0`，说明
只创建了 tus 资源但没有收到任何文件 PATCH；该记录不能进入 OCR 或评分，应由客户端
使用原 `upload.url` 恢复上传，或重新创建上传会话。

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

## OpenClaw File Tools Sandbox

OpenClaw 文档解析在 NotePatch 管理的隔离镜像 `notepatch-openclaw-sandbox:filetools-v1` 中执行。基础 gateway 不直接安装解析器；首次部署或工具版本变化时运行：

```bash
scripts/build_openclaw_sandbox.sh
# 也可只构建 Compose 的 tools profile 目标
docker compose --profile tools build openclaw-sandbox-image
```

候选镜像只有在 `network=none`、只读根文件系统、非 root、`capDrop=ALL` 和 `no-new-privileges` 条件下通过 `notepatch-file self-test` 后，才会更新正式标签。运行中的 sandbox 仍受 2 GB 内存、1.5 CPU、256 PID、执行超时和 workspace-only 文件边界限制；`exec/process` 只允许在 sandbox 内运行，不能回退到 gateway/宿主机，也没有 Docker socket、服务密钥或外网。

统一工具命令：

```bash
notepatch-file inspect /workspace/.../file.pdf
notepatch-file list /workspace/.../archive.zip
notepatch-file extract /workspace/.../file.docx   --output-dir /workspace/notepatch/openclaw/tasks/<task-id>/output/parser/<document-id>
```

支持 PDF，DOC/DOCX、PPT/PPTX、XLS/XLSX，ODF，文本/CSV/JSON/YAML/XML/HTML，图片，EPUB，EML/MSG，IPYNB，ZIP/7z/RAR/TAR 及常见压缩流。音视频只提取 metadata、音轨和关键帧，不提供 ASR。已有 `ocr.md/ocr.txt/converted_pdf` 时 `notepatch_file_reader` 优先读取业务 artifact；否则解析原文件，并生成规范化 `manifest.json/content.md/content.txt`。加密或密码保护文件会明确失败；归档还限制路径穿越、符号链接、设备文件、嵌套、文件数、展开大小和压缩比。

扩展格式仅用于 `chat_attachment` 或 `document_kind=other`，保持 `file_type=other`。学习 OCR 流水线仍只接受图片、PDF、DOCX 和 PPTX；把 EPUB、表格、邮件、压缩包或音视频声明为作业、笔记、课件时返回 `422 unsupported_learning_format`，未知格式返回 `415 unsupported_file_format`。

发布前先检查，再显式应用：

```bash
scripts/rollout_openclaw_sandbox.sh
scripts/rollout_openclaw_sandbox.sh --apply
```

脚本发现 queued/running AI task 时拒绝清理。更新后 supervisor 会按 runtime config hash 重建需要运行的用户 gateway；旧 sandbox 仅在无活动任务时删除。

## OpenClaw Gateway Runner

`POST /workspaces/{workspace_id}/ai/chat` 是唯一的 AI 对话入口，会创建 `openclaw_agent_run` 异步任务。请求体使用 `prompt`、可选 BCP 47 `client_locale`、可选 `conversation_id`、可选 `input` 和可选 `options`；不传 `conversation_id` 时会自动创建会话。未传 locale 时依次使用 `Accept-Language` 和部署 fallback。响应 task payload 包含 conversation、message id 和 locale 快照，客户端通过 task 状态、events 以及会话消息接口获取最终 answer 或失败原因。注册用户时，notepatch 会为该用户生成一套独立 OpenClaw runtime 配置：

空会话初始化问候通过 `GET /workspaces/{workspace_id}/ai/greeting?client_locale=zh-CN` 获取。该响应会按 locale 返回 NotePatch AI 的展示文案，并包含 `onboarding_required/onboarding_version/questions`；它不会创建会话、持久化消息或进入 OpenClaw 上下文。

首次使用 AI 前必须完成用户全局个性化问卷。注册用户和本次 migration 覆盖的历史用户初始均为未完成；登录、上传、历史浏览和后台学习任务不受影响，但发送聊天会返回 `409 ai_onboarding_required`。客户端流程为：

```text
GET /api/v1/auth/ai-onboarding
PUT /api/v1/auth/ai-onboarding
PATCH /api/v1/auth/preferences
```

问卷包含回答语言、协作方式、详略、结构、澄清策略、反馈语气和学习引导七项必答设置，可选 `custom_instructions` 最长 1000 字。提交必须带完整 `version=1` 和全部 answers；以后可通过 `PATCH /auth/preferences` 的 `ai_preferences` 部分更新。每个新 AI task 会固化当时的偏好，重试不受之后修改影响。聊天完整使用偏好；笔记、闪卡、批改、知识库和题目提取只读取各自允许的表达字段。偏好始终低于权限、安全、事实、评分依据、资料忠实度、Skill schema 和工具限制。

`learning_guidance` 必须按稳定字符串值提交，不能按选项下标转换：`answer_first` 表示先给答案，`explain_then_answer` 表示先讲方法再给答案，`hint_first` 表示题目首轮只给提示并等待学生尝试。后端会把 `hint_first` 展开为强制聊天契约，禁止首轮在标题、总结、算式结果、完整代码或例题中泄露答案；询问通用方法时可以讲策略和识别规则，但完整示例必须停在最终结果之前。客户端保存后应重新读取服务端偏好确认最终值。

电子笔记的 `rewrite` 内容策略会重构整份原稿，合并 OCR 碎片并形成连贯章节；`reflow` 模式下若输出仍是一对一 OCR 搬运，后端会拒绝该结果。新增事实仍必须来自同一学习单元的可靠课件、答案或知识库证据；只有原笔记时只重构已有内容，不使用模型常识伪造补充。

聊天图片通过 `input.attachments=[{"document_id":"..."}]` 引用已完成上传的文档。创建 `chat_attachment` 上传会话时可设置 `save_to_documents`：默认 `true` 会保留为 workspace 文档；设为 `false` 时只绑定首次引用它的 conversation，不进入普通文档列表或学习 Skill 的资料镜像，并在删除会话时异步 purge。临时附件仍存放于 SeaweedFS，区别是生命周期归属会话。后端不信任客户端文件名或 MIME，会按 `workspace_id + document_id` 重新校验并把规范化附件保存到 user message。后续轮次启用历史时，会把附件重新绑定到当前 task 的独立 OpenClaw 快照路径。

聊天历史保存在 PostgreSQL，可通过以下接口管理：

```text
GET    /workspaces/{workspace_id}/ai/conversations
GET    /workspaces/{workspace_id}/ai/conversations/{conversation_id}
GET    /workspaces/{workspace_id}/ai/conversations/{conversation_id}/messages
PATCH  /workspaces/{workspace_id}/ai/conversations/{conversation_id}
DELETE /workspaces/{workspace_id}/ai/conversations/{conversation_id}
PATCH  /auth/preferences {"ai_history_enabled": true|false}
GET    /workspaces/{workspace_id}/ai/models
PUT    /workspaces/{workspace_id}/ai/model {"model_id": "openai/model-id" | null}
```

`ai_history_enabled` 是用户全局开关，默认开启。关闭后历史仍可回看，但 worker 不会把它带入后续 OpenClaw 请求；开启时每次最多传入 `AI_CHAT_HISTORY_MESSAGE_LIMIT`（默认 20）条成功消息。它不写入或修改 OpenClaw `MEMORY.md`，后者仍是独立的 agent 长期记忆机制。

新会话先使用首条 prompt 作为临时标题。聊天回答成功落库后，worker 使用独立 OpenClaw title session 和固定低成本模型 `AI_CHAT_TITLE_MODEL`，关闭思考并根据前 `AI_CHAT_TITLE_MESSAGE_LIMIT` 条成功消息生成不超过 `AI_CHAT_TITLE_MAX_LENGTH` 字符的标题。标题优先采用用户消息的主要语言；消息太短、混合或无法判断时使用 task 中的 `client_locale`。标题调用失败不会回退到昂贵主模型，也不会让聊天任务失败；用户手动改名后自动标题永久停止覆盖该会话。

聊天任务通过已有的 task SSE 接口流式回传：`GET /api/v1/workspaces/{workspace_id}/tasks/{task_id}/events/stream`。`chat_answer_delta` 的 `data.stream` 为 `answer`；启用消息级思考后，`chat_reasoning_delta` 的 `data.stream` 为 `reasoning`，它只包含后端规范化后的安全进度摘要，不包含模型隐藏思维或原始推理文本。客户端使用 `sequence_no` / `Last-Event-ID` 续传，收到新的 `chat_stream_started` 时清空本次草稿；完成后仍以 task result 和 assistant message 为准。每条消息可传：`{"options":{"temperature":0.7,"thinking":{"enabled":true,"effort":"low"}}}`。`temperature` 范围为 `0..2`，只作用当前 task；思考强度支持 `minimal`、`low`、`medium`、`high`、`adaptive`，默认关闭。

用户可调用 `POST /api/v1/workspaces/{workspace_id}/tasks/{task_id}/cancel` 终止自己的进行中聊天任务。queued 任务立即变为 `cancelled`，running 任务会中断 gateway 流；已经接收的回答正文会保留在 assistant message 中并标记为 `cancelled`。此接口不接受非聊天 task，也不能取消其他用户会话的任务。

历史 user message 可通过 `POST /api/v1/workspaces/{workspace_id}/ai/conversations/{conversation_id}/messages/{message_id}/revisions` 修改并重新生成。后端保留旧记录但把目标消息及后续旧分支标记为 superseded；默认 messages 列表只返回当前分支，运维审计可使用 `include_superseded=true`。修订接口返回 envelope，新的异步 task 位于 `data`。

模型目录接口由 FastAPI 使用部署级凭据请求 `${OPENAI_BASE_URL}/models`，前端不会接触 provider key。`PUT /ai/model` 保存用户全局模型偏好；传 `null` 恢复 `OPENCLAW_AGENT_MODEL`。该选择影响随后创建的聊天、知识库、题目提取、学霸笔记、批改、高亮和闪卡任务。任务首次执行时会把实际模型固化到 `task.payload.ai_model`，因此重试不会因用户中途切换模型而漂移。目录默认缓存 300 秒；provider 临时不可用时可返回最后一次成功目录并标记 `stale=true`。

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

MVP 使用部署级共享 OpenAI provider key：在 notepatch `.env` 配置 `OPENAI_API_KEY`，supervisor 会把它作为环境变量注入每个用户 gateway 容器。`OPENCLAW_GATEWAY_MODEL` 是调用 gateway `/v1/chat/completions` 的 agent 目标，必须是 `openclaw` 或 `openclaw/<agentId>`；`OPENCLAW_AGENT_MODEL` 是用户未选择模型时的 provider 默认值。真实调用始终保持请求体 `model=openclaw`，并通过 `x-openclaw-model` 发送用户选择的 `openai/<model-id>`，避免把 provider 模型误传到 gateway model 字段。真实 key 不会写入每用户 `.env`、`openclaw.json` 或 `auth-profiles.json`；auth profile 只保存 `OPENAI_API_KEY` 的 env secret reference。若使用 OpenAI-compatible 代理或私有 endpoint，可配置 `OPENAI_BASE_URL=https://proxy.example.com/v1`，notepatch 会写入每用户 `openclaw.json` 的 `models.providers.openai.baseUrl`；空值表示使用 OpenClaw/OpenAI 默认 endpoint。若缺少 `OPENAI_API_KEY`，真实 OpenClaw task 会在调用 gateway 前失败，并在 `task.error_message` 中给出配置提示。

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

如果该用户刚上线但 gateway 还在启动，runner 会先轮询 `/healthz` 等待 ready。聊天请求使用 `OPENCLAW_GATEWAY_TIMEOUT_SECONDS`，较长的学习 Skill 使用独立的 `OPENCLAW_SKILL_TIMEOUT_SECONDS`（默认 300 秒）；同一任务重试时会复用已落盘且通过 schema 校验的迟到输出。若 gateway 未能启动、token 不正确、provider key 缺失、返回非 2xx 或 skill 输出不符合 schema，任务会按指数退避最多重试 3 次，错误与 attempt 都写入 task events。用户离线超过 10 分钟后 supervisor 会停止 gateway；只要仍有 queued/running 的聊天、切题、知识库、笔记、批改、高亮或闪卡任务，supervisor 就会启动并保活对应容器。

交互聊天使用两种明确的镜像范围：请求或历史消息含附件时，只镜像这些显式引用的附件；没有附件的普通聊天会镜像该 personal workspace 的全部 `uploaded/ready` 文档和可用 artifact，并在 `openclaw_prepare.data.mirror_scope` 中分别标识为 `attachments` 或 `workspace`。因此普通聊天的 task-local `documents/index.json` 不会因空附件集合而变成空索引。客户端可用 `input.use_knowledge_base=false` 显式关闭本次知识库检索，但这不会改变附件镜像的权限范围。

生产环境只提供 Gateway runner。单元测试通过依赖注入使用 `tests/fakes.py`，不会在生产代码中生成替代结果。

## DocTr Image Preprocessing

`POST /workspaces/{workspace_id}/documents/{document_id}/process` 对 `file_type=image` 的文档会先调用内网 DocTr 服务做几何矫正，输出 `deskewed_image.png` artifact。当前默认 `DOCTR_ILL_REC=false`，不执行光照修复；只有部署显式开启该配置时才额外运行 IllTr。DocTr 只做图片预处理，不做 OCR；如果 DocTr 失败，worker 会写 warning event 并 fallback 到原图继续 OCR。

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
- `chat` queue 使用 Redis key `notepatch:tasks:chat`，常驻 `chat-worker` 只消费交互式 `openclaw_agent_run`，避免被长时间学习任务阻塞。
- `ai` queue 使用 Redis key `notepatch:tasks:ai`，常驻 `ai-worker` 消费题目提取、知识库、笔记、批改、高亮和闪卡任务。
- `ocr_document` 和 `document_processing_pipeline` 都进入 `ocr` queue，普通 worker 不会加载 PaddleOCR；同一 OCR worker 进程会复用已加载模型。
- `openclaw_agent_run` 进入低延迟 `chat` queue。
- `extract_questions`、`grade_homework`、`build_knowledge_base`、`generate_study_notes`、`generate_note_supplement`、`generate_flashcards`、`highlight_study_notes` 进入后台 `ai` queue。
- `scan_document`、`detect_note_gaps`、`purge_study_note_history`、其他 purge 和 merge 等非模型编排任务进入 `default` queue。
- 可恢复错误进入 Redis delayed-retry zset，按指数退避重新投递；不会阻塞 worker loop。

worker 可用 CLI 或 env 指定队列：

```bash
python -m notepatch.entrypoints.worker --queues default
python -m notepatch.entrypoints.worker --queues ocr
python -m notepatch.entrypoints.worker --queues chat
python -m notepatch.entrypoints.worker --queues ai
WORKER_QUEUES=default python -m notepatch.entrypoints.worker
```

如果任务被误投到错误 Redis list，worker 会把它放回该 task type 对应的队列，不会直接处理。

## Automatic Learning Workflow

`AUTO_LEARNING_PIPELINE=true` 时，上传完成后会创建现有 `document_processing_pipeline` task。资料按类型分流：

1. `note`：OCR → 知识库 → 立即排队生成忠实电子笔记 → 加权闪卡；有错题时更新最新高亮。仅当部署显式把 `STUDY_NOTE_DEBOUNCE_SECONDS` 设为正数时才延迟生成。
2. `courseware/other`：OCR → 知识库 → `detect_note_gaps`，不会自动生成或改写笔记。
3. `homework/corrected_homework`：OCR → 题目提取 → 评分 → 答题记录/错题 → 缺口检测；已有笔记时才高亮和重建闪卡。
4. `exam`：OCR、题目提取和缺口检测，不自动创建普通 Homework。
5. `answer_key/rubric`：OCR 后作为评分依据，不生成笔记。

有答案或 rubric 时评分为 `official`；没有依据时只能产生带置信度的 `provisional` 诊断结果。学习单元优先使用显式 `learning_unit_id`；未指定时先精确匹配，再在 OCR 后用 BGE-M3 高置信归组，低置信则新建单元。

上传响应包含 `workflow_run_id`，Document 包含 `latest_workflow_run_id`。客户端应优先跟踪聚合工作流：

```http
GET /api/v1/workspaces/{workspace_id}/workflows
GET /api/v1/workspaces/{workspace_id}/workflows/{workflow_run_id}
GET /api/v1/workspaces/{workspace_id}/workflows/{workflow_run_id}/events
GET /api/v1/workspaces/{workspace_id}/workflows/{workflow_run_id}/events/stream
GET /api/v1/workspaces/{workspace_id}/documents/{document_id}/workflow
```

### Faithful Note Policies

用户默认值通过 `PATCH /api/v1/auth/preferences` 设置：

```json
{"note_content_edit_level":"conceptual","note_layout_edit_level":"minor","note_history_limit":3}
```

内容等级为 `verbatim/spelling/conceptual/rewrite`，排版等级为 `preserve/minor/reorder/reflow`，默认 `conceptual + minor`。上传 note 可用同名字段单次覆盖；也可手动触发：

```http
POST /api/v1/workspaces/{workspace_id}/learning-units/{unit_id}/notes/generate

{"content_edit_level":"spelling","layout_edit_level":"preserve","force_reprocess":false}
```

策略在 task 创建时固化。Scholar Notes 只输出带来源映射的 Note IR；后端校验每个 OCR block、代码缩进、公式、表格、箭头/圈选关系和纠错证据，再确定性渲染 HTML。非 `rewrite` 模式不得漏掉或重复原始 block；`conceptual` 修正必须有高置信来源。上传图片和 DocTr 矫正图只作为内部视觉参考，不会被裁剪、复制或嵌入最终笔记；低置信图示仅保留可验证的结构化描述和关系，不凭空补全细节。

`rewrite` 还会用原笔记标题、OCR 正文和已有知识点检索同一 LearningUnit 的课件、知识块、答案/rubric、作业题目及评分知识点。默认只选择相关度不低于 `0.75` 的前 12 项；学生答案和 provisional 评分只能提示“可能缺少哪个主题”，不能作为事实依据。每个自动补充 block 必须引用后端提供的权威证据，并在 HTML 中显示低干扰的“资料补充”标记。Note JSON 的 block 会包含 `origin="evidence_supplement"`、`source_refs` 和 `supplement_reason`，版本 metadata 则记录 `completion_count/completion_source_document_ids/completion_evidence_revision/completion_strategy`。知识库或答题 revision 在生成期间变化时，本次输出会被丢弃并重新调度。

这些补全只在用户选择 `content_edit_level=rewrite` 后发生。上传课件或作业本身仍只更新知识库和缺口建议，不会静默改写现有笔记。检索依赖 BGE-M3；服务不可用时任务按既有重试机制处理，不会悄悄保存一份不完整结果。相关部署阈值为 `NOTE_REWRITE_COMPLETION_SIMILARITY_THRESHOLD` 和 `NOTE_REWRITE_COMPLETION_MAX_EVIDENCE`。

`note_history_limit` 表示最新版本之外保留的历史版本数，范围 `0..100`。正文变化创建新版本；超限版本、专属对象和对应卡组异步清理，但不删除最新版本、原始资料或答题审计。

### Continuous Image Notes

连续多图先创建 NoteSet：

```http
POST /api/v1/workspaces/{workspace_id}/note-sets

{"title":"操作系统第 3 讲","expected_page_count":3,"subject":"computer science"}
```

每页 upload session 提交相同 `note_set_id` 和唯一、从 0 开始的 `page_index`，且 `document_kind` 必须为 `note`。上传全部页面后调用：

```http
GET  /api/v1/workspaces/{workspace_id}/note-sets/{note_set_id}
POST /api/v1/workspaces/{workspace_id}/note-sets/{note_set_id}/complete
```

每页独立 OCR 和重试，但组内资料固定属于同一 LearningUnit；全部知识库完成后按 `page_index` 合并且只生成一份笔记。没有 NoteSet 时继续使用高置信自动归组，低置信资料不会被强行拼接。

### Knowledge Gaps

课件、作业和考试只提出缺口，不直接修改笔记：

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

建议返回 `document_id/page_index/block_id/bbox/excerpt` 来源和 `section_id/insert_position/target_anchor` 位置，可跳转到 `rendered_html#target_anchor`。接受时必须仍基于最新笔记，否则返回 `409`。没有基础笔记时状态为 `no_base_note`，必须显式通过 `notes/from-gaps` 创建首版。来源删除或笔记变化后建议变为 `stale`。

### Note Results

```http
GET  /api/v1/workspaces/{workspace_id}/learning-units/{unit_id}/notes?include_download_url=true
GET  /api/v1/workspaces/{workspace_id}/learning-units/{unit_id}/notes/{note_version_id}/download-url?kind=rendered_html
GET  /api/v1/workspaces/{workspace_id}/learning-units/{unit_id}/notes/{note_version_id}/corrections
POST /api/v1/workspaces/{workspace_id}/learning-units/{unit_id}/notes/{latest_note_version_id}/revisions
GET  /api/v1/workspaces/{workspace_id}/learning-units/{unit_id}/flashcard-decks/latest
```

### 闪卡复习原因

最新和历史闪卡组详情中的每张卡除 `priority_score/priority_factors` 外，还返回结构化 `review_hint`。主提示会区分最近连续答对、刚出现失误、近期频繁错误、最近做对、来自笔记和历史错误；最多三个 badges 补充连续正确次数、近期错误次数和最近结果。后端只返回稳定的 `code/message_key/tone/params`，客户端负责本地化，未知 key 回退为“建议复习”。新卡组返回 `data_quality=complete`；旧卡组只根据当时保存的权重因子返回 `legacy`，不会用当前答题记录改写历史含义。

提示阈值可通过 `FLASHCARD_HINT_ERROR_WINDOW_DAYS`、`FLASHCARD_HINT_SUCCESS_WINDOW_DAYS`、`FLASHCARD_HINT_FRESH_ATTEMPT_DAYS`、`FLASHCARD_HINT_FREQUENT_ERROR_COUNT` 和 `FLASHCARD_HINT_IMPROVING_STREAK` 调整。评分产生新答题记录后会触发新卡组，排序和提示会同步更新。


笔记本来源标识处理与内容修改等级绑定：`verbatim` 按字面保留全部原稿，包括印刷的学校、公司或制造商文字；`spelling`、`conceptual`、`rewrite` 会排除页眉、页脚、封面中的学校、公司、生产商、出版社、Logo 和版权行。被排除的 OCR block 会记录在笔记 JSON 与版本 metadata 的 `excluded_source_blocks/excluded_notebook_identity_blocks` 中，且后端会拒绝遗漏排除、重复映射或在标题/摘要/正文中重新出现的来源标识。普通含“公司”“大学”等词的学习句子不会仅凭关键词删除。

默认 `STUDY_NOTE_DEBOUNCE_SECONDS=0`，note 的知识库更新完成后会立即排队生成电子笔记；正数配置只用于部署方主动启用防抖。客户端优先展示 `download_urls.rendered_html`，按后端主题渲染；手工修订创建新版本，错题高亮只更新最新版本。图片 note 的 OCR 是文字事实基线；Scholar Notes 的代码、公式、圈选、箭头和排版视觉参考只使用 DocTr 生成的 `deskewed_image`，绝不把原始上传图发送给文档 Skill。有效矫正 artifact 缺失或对象失效时，worker 会在 GPU lease 内从 SeaweedFS 原图自动补跑 DocTr；DocTr 暂时不可用时任务按既有策略重试，矫正图与原图同时缺失时永久失败。只有 provider 明确不支持图片时，Scholar Notes 才在同一任务中降级为 OCR-only。

这条 corrected-only 规则只适用于文档 Skill。普通 `/ai/chat` 图片附件仍使用用户原始图片，不会被 DocTr 矫正。`ai_visual_deskewed_reused`、`ai_visual_deskewed_regeneration_started`、`ai_visual_deskewed_regenerated` 和 `ai_visual_deskewed_original_missing` task events 可用于排查视觉准备过程；事件只包含 document/artifact ID，不包含 object key 或图片内容。

历史数据校正脚本默认 dry-run：

```bash
docker compose exec api python /opt/notepatch/scripts/reconcile_faithful_notes.py
docker compose exec api python /opt/notepatch/scripts/reconcile_faithful_notes.py --workspace-id <workspace-id> --apply
```

历史自动创建的碎片单元可先 dry-run，再按高置信结果创建现有异步 merge task：

```bash
docker compose exec api python /opt/notepatch/scripts/reconcile_learning_units.py
docker compose exec api python /opt/notepatch/scripts/reconcile_learning_units.py --workspace-id <workspace-id> --apply
```

历史合并阈值默认 `0.94`、领先差值 `0.08`，仅自动合并 `metadata.source=automatic_pipeline` 的单元；手工单元可作为目标但不会作为自动删除的来源。原始文档和评分审计不删除。

后台会为每个用户 OpenClaw runtime 写入内置 skill 说明：

```text
notepatch_file_reader
notepatch_question_extractor
notepatch_kb_builder
notepatch_scholar_notes
notepatch_note_supplement
notepatch_grading
notepatch_note_highlighter
notepatch_flashcards
```

每个 skill 安装在用户 `workspace/skills/`，因此启用 workspace-only 文件权限的 OpenClaw sandbox 可以读取，且不会越过 `/workspace` 边界。skill 使用固定 `input.json` 与输出文件、稳定 session key，并在输入中携带后端生成的严格 Pydantic JSON Schema。首次结构错误会在同一 session 请求校正；仍不合法则任务重试，绝不落库替代内容。Agent 仅可在用户 workspace 内读写；文件解析允许 sandbox 内的 exec/process，但浏览器、外部网络、elevated 和宿主执行始终禁用。

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

`ocr-worker` 使用 `paddleocr-cache:/models/paddlex` 持久化模型；`embedding-service` 使用 `bge-cache`。默认单张 16 GB GPU 部署中，PaddleOCR 使用 GPU 和 `auto_growth` 分配策略，DocTr 在每次请求后卸载模型并释放显存，BGE-M3 通过 `EMBEDDING_DEVICE=cpu` 运行。这样避免三个大型模型同时常驻导致 PP-StructureV3 OOM。GPU 操作仍通过 Redis lease 串行，lease 带 token、TTL 和自动续租；`OCR_WORKER_CONCURRENCY=1`，单卡部署不要横向扩容 GPU worker。显存更大的部署可显式设置 `EMBEDDING_DEVICE=cuda:0`，但应先完成并发 OCR 压力测试。

宿主 NVIDIA 驱动更新、重载或 GPU reset 后，长期运行的 GPU 容器可能保留失效的 NVML 注入，表现为 `Paddle CUDA runtime is unavailable: No CUDA device is visible` 或 `Failed to initialize NVML`。先确认没有 queued/running 的 GPU/AI task，再重新创建容器；单纯 `restart` 不足以刷新 NVIDIA runtime：

```bash
nvidia-smi
docker compose up -d --force-recreate --no-deps docserver embedding-service
docker compose --profile ocr up -d --force-recreate --no-deps ocr-worker
docker exec notepatch-server-ocr-worker-1 \
  python -c 'import paddle; print(paddle.device.cuda.device_count(), paddle.device.get_device())'
```

预期输出包含 `1 gpu:0`，且 `docker compose ps` 中三个 GPU 服务均为 healthy。模型权重和缓存位于持久 volume/bind mount，重建容器不会删除它们。

DOCX/PPTX 先由内网 LibreOffice converter 转为 `converted_pdf` artifact，再复用 PDF OCR；转换失败会明确写入 task/events。

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
## Upload validation and optional security scanning

File security scanning is disabled by default so completed uploads can be displayed and processed immediately:

1. tusd still accepts at most `UPLOAD_MAX_FILE_SIZE_MB` (200 MB by default), and object keys remain server-generated.
2. With `CLAMAV_ENABLED=false`, upload completion sets `scan_status=skipped` and does not create a `scan_document` task. Learning files enter the automatic pipeline immediately; chat attachments become `ready`.
3. Disabled scanning does not compute SHA-256 or inspect the actual MIME type with libmagic. The declared MIME allowlist and upload size limit still apply.
4. Optional ClamAV scanning can be restored with the `security` Compose profile. In that mode MIME spoofing, malware, scanner unavailability, or oversize input fails the document and removes the untrusted object.
5. DOCX/PPTX files are converted by the internal LibreOffice service to a `converted_pdf` artifact before OCR. Images, PDFs and converted files continue according to `document_kind`: note builds knowledge and a faithful HTML note; courseware/other build knowledge and note-gap suggestions; homework types extract, grade and detect gaps; answer/rubric stop after OCR until referenced.

A document without a valid `learning_unit_id` creates its own learning unit. Merge units asynchronously:

```http
POST /api/v1/workspaces/{workspace_id}/learning-units/{target_id}/merge
Content-Type: application/json

{"source_learning_unit_ids":["source-unit-id"]}
```

The response is a `202 TaskRead`. Poll the task or stream ordered events:

```http
GET /api/v1/workspaces/{workspace_id}/tasks/{task_id}/events
GET /api/v1/workspaces/{workspace_id}/tasks/{task_id}/events/stream
Authorization: Bearer <access-token>
Last-Event-ID: 12
```

SSE emits monotonic event IDs, heartbeat comments, and a final `done` event. Existing polling remains supported.

Study notes are validated safe HTML fragments. Clients should prefer the signed `download_urls.rendered_html` URL, which wraps the newest highlighted/plain fragment with the note version's immutable theme and a strict CSP. Legacy notes retain their original themes; newly generated notes use `notepatch-paper-v4`. Note IR text is a restricted Markdown fragment that the backend deterministically renders into paragraphs, lists, headings, tables, and code before applying the HTML sanitizer. Clients must not parse the stored fragment as Markdown a second time, and must use `rendering.theme_id` plus the complete `rendering.css_url`, including its `?v=` cache revision, instead of hard-coding a stylesheet.

Existing Note IR notes can be reformatted without calling AI or creating a new note version. Preview the operation with `docker compose exec -T api python /opt/notepatch/scripts/rerender_note_ir_markdown.py`; apply it with the same command plus `--apply`. Notes without `note_ir_object_key` are reported as legacy and are not modified.

Rich-text editors must persist font size with controlled classes such as <span class="np-font-size-24">Text</span>. Supported sizes are 12, 14, 17, 20, 24, 28, 32, and 40 px. Inline style attributes and unsupported font-size classes are intentionally removed by the backend sanitizer.

### Operations

Validate and migrate before replacing containers:

```bash
docker compose config --quiet
docker compose run --rm api alembic upgrade head
docker compose up -d --build api worker chat-worker ai-worker converter admin-web
docker compose --profile ocr up -d --build ocr-worker

# Optional: enable antivirus scanning explicitly.
CLAMAV_ENABLED=true docker compose --profile security up -d --build clamav api worker
```

Start backups and monitoring after setting non-default `RESTIC_PASSWORD` and `GRAFANA_ADMIN_PASSWORD`:

```bash
docker compose --profile ops up -d --build backup prometheus grafana
docker compose --profile ops run --rm backup sh /opt/notepatch/scripts/backup/backup_once.sh
docker compose --profile ops run --rm backup sh /opt/notepatch/scripts/backup/list.sh
docker compose --profile ops run --rm backup sh /opt/notepatch/scripts/backup/check.sh
```

Backups contain a PostgreSQL custom dump, a logical SeaweedFS S3 mirror, configuration manifest, and checksums under `${NOTEPATCH_DATA_ROOT}/backups`; Restic keeps 14 daily snapshots. Restore requires an explicit new target directory and never overwrites the running environment.

Host exposure is limited to API `8001`, admin `5173`, tusd `1080`, signed S3 downloads `8333`, and optional Grafana `3000`. PostgreSQL, Redis, SeaweedFS master/filer, DocTr, embedding, converter, optional ClamAV, and Prometheus remain internal.

## Random Public Gateway (FRP + IP TLS)

Public access uses one deployment-only random prefix. The real value belongs in the untracked `.env` and `/etc/notepatch/public-gateway.env`; never commit it.

```text
https://PUBLIC_IP/np-<32-lowercase-hex>/                 Admin Web
https://PUBLIC_IP/np-<32-lowercase-hex>/api/v1/          FastAPI
https://PUBLIC_IP/np-<32-lowercase-hex>/files/           tusd
https://PUBLIC_IP/np-<32-lowercase-hex>/health           health
https://PUBLIC_IP/notepatch/...                          signed SeaweedFS objects
```

All other HTTPS paths return `404`. HTTP serves only `/.well-known/acme-challenge/`; after TLS activation, other HTTP requests redirect to the random-prefix homepage. The random path only reduces scanner noise and does not replace JWT, workspace checks, rate limits, or S3 signatures.

This host terminates TLS in Nginx. A remote frps forwards TCP `80` and `443`; it must not terminate or rewrite HTTP. Bootstrap in this order:

```bash
# Local host: install the HTTP ACME site, Certbot and frpc. frpc remains stopped.
sudo scripts/public-gateway/configure_nginx.sh http
sudo scripts/public-gateway/install_certbot.sh
sudo scripts/public-gateway/install_frpc.sh

# Cloud host: install frps v0.71.0, copy infra/frp/frps.toml.example,
# and securely copy /etc/frp/client_token to /etc/frp/server_token.
# Open TCP 7000, 80 and 443, then start frps.

# Local host after frps is reachable:
sudo systemctl enable --now frpc
sudo scripts/public-gateway/issue_ip_certificate.sh staging
sudo scripts/public-gateway/issue_ip_certificate.sh production
```

The production certificate uses Certbot's `shortlived` profile and IP validation. `notepatch-cert-renew.timer` checks every eight hours. It skips certificates with more than three days remaining, force-renews certificates inside the three-day window, validates Nginx, and reloads it only after successful renewal. Failures are recorded in the systemd journal. Do not enable the TLS site before the certificate exists. Nginx preserves the original `/notepatch/...` URI and `Host` because both are part of S3 SigV4.

`notepatch-frp-direct-route.service` installs an IPv4 policy rule with priority `80` for the frps address. It forces both the long-lived frpc control socket and later work sockets to use the main routing table instead of v2rayN/sing-box TUN tables. The frpc service also clears proxy environment variables. Install or refresh it with `sudo scripts/public-gateway/install_frp_direct_route.sh`; verify with `ip route get 8.137.78.255`, which must show the physical LAN interface rather than `singbox_tun`.

For public activation, configure:

```bash
PUBLIC_PATH_PREFIX=/np-<32-lowercase-hex>
PUBLIC_API_BASE_URL=https://PUBLIC_IP${PUBLIC_PATH_PREFIX}
TUSD_BASE_URL=https://PUBLIC_IP${PUBLIC_PATH_PREFIX}/files/
SEAWEEDFS_PUBLIC_BASE_URL=https://PUBLIC_IP
VITE_API_BASE_URL=${PUBLIC_PATH_PREFIX}/api/v1
VITE_TUSD_BASE_URL=https://PUBLIC_IP${PUBLIC_PATH_PREFIX}/files/
```

LAN/VPN ports `8001`, `5173`, `1080`, and `8333` remain published for existing clients. Public clients must use the prefixed URLs.

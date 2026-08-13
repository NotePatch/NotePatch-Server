# NotePatch 全功能系统审计报告

- 日期：2026-08-13（Asia/Shanghai）
- 仓库：`/home/usr/notepatch-server`
- 测试数据：`/home/usr/Documents/收废品.zip`
- 隔离 workspace：`10934b0e-ff7d-46cc-9cbf-18a8fb44ebfe`
- 数据原则：真实 OCR、DocTr、LibreOffice、ClamAV、BGE-M3、pgvector、OpenClaw Gateway/Skills；单元测试使用显式 fake，不生成生产替代结果。

## 1. 总体验收

| 门禁 | 结果 | 数据 |
|---|---|---|
| OpenAPI | 通过 | 102 paths，117 operations；admin 56、AI 9、documents 8、learning 10 等 |
| Backend tests | 通过 | 160 passed；1 条 Starlette TestClient 弃用 warning |
| Python syntax | 通过 | `python3 -m compileall backend/src services scripts` |
| Compose | 通过 | 20 个含 profile 服务，配置校验通过 |
| Admin Web | 通过 | Node 20 + Vite production build，Nginx 镜像构建成功 |
| Migration | 通过 | 12 个 revision；临时 PostgreSQL 完成 head→base→head |
| Runtime | 通过 | API、default/chat/OCR worker、DocTr、converter、embedding、ClamAV、SeaweedFS、Redis、PostgreSQL 健康 |
| Observability | 通过 | Prometheus target up；Grafana 12.1.0 database ok；default/ocr/chat queue 均为 0 |
| Backup | 通过 | Restic 当日快照 66.082 MiB；`check --read-data-subset=5%` 无错误 |
| Secret scan | 通过 | Git diff 未包含 provider key、token 或测试密码 |

完整接口清单见 [OpenAPI operations](data/openapi-operations.tsv)，任务和阶段原始数据见 [tasks](data/tasks.tsv) 与 [events](data/events.tsv)。

最终重启核验：API `/health` 返回 `ok`，20 个 Compose 服务均处于 running/healthy，default、ocr、chat 三个队列均为 0，数据库无 queued/running task，worker 最近日志无 lease error、Traceback 或 critical。

## 2. 测试资料

原 ZIP 大小 1,042,139,937 bytes，共 12,523 entries、8,433 PDFs。为控制生产测试时间，从真实归档抽取手写英语图片、数学作业 PDF、批改作业 PDF、两页英语课件 PDF；另生成结构合法的 DOCX/PPTX 以覆盖转换链路。

| 输入 | 大小 | 声明/检测类型 | 扫描 | 最终状态 |
|---|---:|---|---|---|
| 手写笔记 PNG | 76,728 | image/png → image/png | clean | ready |
| 笔记 DOCX | 992 | DOCX → DOCX | clean | ready |
| 课件 PPTX | 4,488 | PPTX → PPTX | clean | ready |
| 数学作业 PDF | 59,899 | PDF → PDF | clean | ready |
| 答案 PDF | 59,899 | PDF → PDF | clean | ready |
| EICAR DOCX | 1,166 | DOCX | infected | failed，对象不进入 OCR |
| PNG 冒充 PDF | 76,728 | PDF → image/png | rejected | failed，对象删除 |
| 201 MiB 声明上传 | 210,763,776 | 超限 | 未创建 | HTTP 413 |

逐文档 ID、SHA-256 前缀和时间见 [documents.tsv](data/documents.tsv)。

## 3. 身份、Workspace 与会话

| 阶段 | 输入 | 输出/断言 |
|---|---|---|
| register | 独立测试邮箱与密码 | User、唯一 personal Workspace、owner membership、access/refresh token |
| login/me | 正确凭据/JWT | 用户资料、`ai_history_enabled`、`preferred_ai_model` |
| refresh 并发 | 同一 refresh token 两个并发请求 | 两个请求均 200；rotation grace 生效 |
| preferences | history false→true | 用户全局开关持久化 |
| presence | heartbeat/client_id→offline | Redis session TTL 写入并清理 |
| workspace recovery | 再次 POST workspace | 409；不会创建第二个 personal workspace |
| members | personal workspace invite | 410；组织成员能力关闭 |
| isolation | A/B workspace 与猜测资源 ID | workspace 403；跨 workspace resource 404 |

## 4. 上传、tusd、扫描与存储

实际阶段数据：

1. `upload-session` 只写 metadata，object key 固定在 `workspaces/{workspace}/documents/{document}/original/`，文件名经过 sanitize。
2. 客户端通过 tusd PATCH 断点上传；FastAPI 不承载大文件正文。
3. `post-finish` webhook 幂等创建 original artifact；重复事件不重复落库。
4. `scan_document` 计算 SHA-256、libmagic MIME、ClamAV 结果；声明/检测冲突和病毒命中 fail closed。
5. clean 文档进入 OCR queue；失败文档不进入下游。
6. 下载 URL 仅在 workspace/admin 权限通过后由 StorageService 签发。

本次 `scan_document`：6 succeeded、3 expected failed；成功平均运行 0.071 秒。SeaweedFS 中 original 6 份、202,135 bytes。历史 6 条缺少 file_size 的 `questions_json` 已通过 HeadObject 幂等回填，剩余缺失 0。

## 5. 转换、DocTr 与 OCR

| 文件类型 | 预处理 | OCR 输入 | 产物 |
|---|---|---|---|
| 图片 | DocTr rectify | deskewed PNG | OCR/布局/公式/表格六件套 |
| PDF | PyMuPDF render | 每页 PNG | 同上 |
| DOCX | LibreOffice → converted PDF | PDF pages | 同上 |
| PPTX | LibreOffice → converted PDF | PDF pages | 同上 |

真实引擎为 PP-OCRv5 + PP-StructureV3 + PP-FormulaNet_plus-M；图片 DocTr 成功后优先使用 deskewed artifact。12 次 `document_processing_pipeline` 全部 succeeded，平均 12.922 秒，最大 17.331 秒。

Artifact 汇总：converted PDF 3/54,505 bytes，deskewed image 5/9,956,510 bytes，OCR JSON 12/99,804 bytes，Markdown 12/12,643 bytes，Text 12/12,468 bytes，layout 12/63,422 bytes。完整明细见 [artifacts.tsv](data/artifacts.tsv)。

进程缓存验证：同一图片连续两次 force reprocess 均约 5 秒；第一次创建 16 个 Paddle 组件，第二次日志中 `Creating model` 为 0。force 模式创建新版本，不覆盖 original 或旧 OCR artifacts。

## 6. 任务与队列

| Queue | Task types | 结果 |
|---|---|---|
| default | scan、purge、merge 等编排 | 不被长 AI 调用阻塞 |
| ocr | document pipeline、ocr_document | 独立 GPU worker，进程级模型复用 |
| chat | chat 与全部 OpenClaw-backed learning skills | 独立于扫描/purge |

Redis worker lease 为 60 秒、每 20 秒续租；真实 70 秒 smoke 后 TTL 为 50，退出后 key 删除。容器退出遗留的 flashcard running task被自动写入 `orphan_requeued`，attempt 1→2 后 succeeded。活 lease 不回收，attempt 耗尽明确 failed。

成功任务平均/最大运行秒数：KB 55.301/92.677，OCR pipeline 12.922/17.331，切题 65.273/67.506，闪卡 45.524/69.642，笔记 84.991/107.643，批改 88.584/97.582，高亮 129.348/150.594，聊天 32.231/67.496。历史 expected/修复前失败仍保留用于审计，不代表当前回归失败。

## 7. 知识库

1. 课件/笔记 OCR 后调用 `notepatch_kb_builder`。
2. OpenClaw 输出经 Pydantic schema 校验；失败不落假数据。
3. BGE-M3 批量生成 1,024 维 embedding。
4. pgvector 以 workspace/learning unit 过滤 cosine top-k。
5. force reprocess 在新结果验证成功后替换该文档旧 chunks，不累加重复块。

最终数据：17 knowledge chunks，17/17 有 embedding，维度 min=max=1024；28 canonical knowledge points。语义搜索“quadratic equations and algebra”返回带 `score/document_id/metadata` 的结果。两次强制 KB 重建后目标文档仍为 9 chunks，而非 18。

## 8. HTML 学霸笔记

知识库与笔记是独立生命周期：KB 每文档更新，笔记在 revision 防抖后合并生成。Skill timeout 已从聊天 120 秒中拆出，学习 Skill 默认 300 秒；同 task 重试会接管迟到且 schema-valid 的输出。

最终有 4 个有效 NoteVersion。合并目标单元最新为 version 2，包含 11 个知识点、2 个来源文档、17 个错题引用。高亮 HTML 7,311 bytes，包含 11 个 `data-knowledge-point-id`，使用 `np-highlight--red`/`np-highlight--yellow`，无 `<script>`、事件属性或外部 style。

高亮严格原地更新最新 note：执行前后版本数均为 2，version_no 最大值仍为 2；只更新 `highlighted_html_object_key`、highlight map 与 source mistake IDs。rendered HTML 带 CSP、nosniff、no-store，并套用版本化 NotePatch paper theme。

## 9. 题目、作业、批改、错题与闪卡

- 真实 question extractor 生成 2 份 `questions_json`，现有 metadata 合计 6,928 bytes。
- Homework `10d09d86-52c7-485c-ac25-f48b4c2690cc` 有 1 个 answer-key reference，rubric PATCH 只更新显式字段。
- 无依据：provisional 61/100，confidence 0.48。
- 有答案/rubric：official 13/20，confidence 0.72；后续 official 12/20，confidence 0.55。
- 17 条 open mistake；20 次 knowledge-point attempt：3 correct、14 partial、3 incorrect。
- 合并后 Homework、KnowledgePoint、Attempt、Mistake metadata 全部迁到 target unit；不会回流已隐藏 source unit。
- 最新 flashcard deck：13 cards，attempt_revision=3，priority 1.000–5.995；优先级由错误压力、时间衰减、成功压力和连续答对确定。
- 同一知识点允许多张内容不同的卡；仅拒绝未知 point ID 或完全重复 front/back。

## 10. AI 模型、聊天与历史

Provider `/models` 返回 7 个模型；用户选择快照为 `openai/gpt-5.5`，Gateway body 仍为 `model=openclaw`，真实模型通过 `x-openclaw-model` 传递。切换只影响新任务。

会话 1 个、消息 6 条、3 条 assistant 全部 succeeded；每条 assistant 保存 model_id、citations 和 source status。首轮真实回答 548 字符、6 citations。历史竞态测试中，提交时关闭历史后立即重新开启，Task payload 固化 `ai_history_enabled=false`，最终精确返回 `NO_HISTORY`；证明执行不受后续偏好变化影响。

每 task 使用独立文档快照目录，聊天与 Skill 不再互删共享镜像。删除来源文档只移除 citation 并更新 source status，不清空历史正文。

## 11. 学习单元合并

合并 task 本体 0.142 秒完成编排，源单元隐藏并指向 target，原始文档保留。下游 force reprocess 重建 OCR→KB→笔记→闪卡/高亮。

本轮修复了三类一致性：

- 派生任务失败后管理员 retry 成功，可从 `merge_status=failed` 恢复并最终 completed；无关任务不能洗掉失败状态。
- 合并同步迁移 Homework metadata、KnowledgePoint、Attempt、Mistake metadata 和 attempt revision。
- 目标最新 note/highlight/flashcards 使用迁移后的 20 次 attempt，不再向 source unit 写新数据。

## 12. 删除、取消与管理后台

文档删除返回 202，立即隐藏并创建 purge task；取消上传 session、协作取消任务、清理对象/派生数据，保留脱敏 tombstone 与 task audit。tusd webhook 无法复活 deleted document。对话软删除取消活动 AI task但不受普通文档 purge 误伤。

管理员 live smoke 覆盖：用户创建/编辑/禁用/启用、临时密码强制修改、密码重置、异步物理删除；文档/task/learning/note/knowledge/homework/mistake/conversation/operations/audit read；task retry、note regenerate/highlight、用户 purge。物理删除 operation succeeded，目标用户不存在，审计保留脱敏记录。

管理后台 API 非登录 401、普通用户 403；管理员下载 URL 与全局查询可用。前端缓存失效和异步 operation 轮询已通过生产构建。

## 13. 故障注入

| 场景 | 预期/结果 |
|---|---|
| 超大声明文件 | 413，不创建上传 |
| MIME spoof | scan rejected，Document failed，对象删除 |
| EICAR DOCX | ClamAV infected，Document failed，不 OCR |
| 重复 tus finish | 幂等，无重复 original artifact |
| Gateway 启动慢 | ready poll 后 retry succeeded |
| Skill 超过 120 秒 | 使用 300 秒专用 timeout；真实笔记 84 秒成功 |
| Gateway 迟到输出 | 同 task 后续 attempt 复用 schema-valid 文件 |
| Worker 被重启 | Redis lease 消失后 orphan requeue，真实任务 attempt 2 succeeded |
| force OCR/KB | OCR 新版本；KB 替换旧 chunks，不重复累积 |
| 历史开关竞态 | 使用提交快照，返回 NO_HISTORY |
| 跨 workspace 猜 ID | 403/404，无数据泄露 |

## 14. 本次修复清单

1. 移除无 handler 的 `preprocess_image` task type，并强制 registry/task type 一致。
2. 修复用户与管理员闪卡入口的 learning unit/note 校验。
3. OpenClaw-backed 学习任务统一路由 chat queue。
4. OCR worker 复用进程级模型。
5. force KB 重建替换旧 chunks。
6. 允许同知识点多张不同闪卡，拒绝真正重复内容。
7. 聊天历史开关写入 task 快照。
8. Admin task events 返回 sequence_no。
9. Skill 独立 300 秒 timeout，重试复用迟到合法输出。
10. 合并失败重试可正确收口 completed。
11. 合并完整迁移 Homework/attempt/mistake/point 引用。
12. 新笔记有错题时自动同时调度高亮。
13. Redis task lease、续租和 orphan recovery。
14. JSON artifact 写入真实 file_size，并回填历史缺失记录。
15. README/前端文档同步真实 queue、timeout、Office 转换行为。

## 15. 剩余非阻断项

- 测试依赖有 1 条 Starlette TestClient 弃用 warning；不影响运行，后续可迁移 httpx2 测试客户端。
- 当前是 local build，`/health` 的 revision/build_time 为 `dev/unknown`；CI/发布环境应设置 `GIT_SHA` 与 `BUILD_TIME`。
- 服务均监听 IPv4 `0.0.0.0`。主机地址为 LAN `192.168.100.123` 与 Tailscale `100.116.101.110`；当前签名 SeaweedFS URL 使用 LAN host。若客户端只经其他 10.x VPN 地址访问，需要把 `SEAWEEDFS_PUBLIC_BASE_URL`、tusd 和 API public base 统一设置为该客户端实际可路由地址。
- 隔离 audit workspace 保留，便于管理员复核本报告；本次测试生成的临时 token 与凭据文件已删除。

## 附录

- [documents.tsv](data/documents.tsv)
- [artifacts.tsv](data/artifacts.tsv)
- [artifact-summary.tsv](data/artifact-summary.tsv)
- [tasks.tsv](data/tasks.tsv)
- [events.tsv](data/events.tsv)
- [learning.tsv](data/learning.tsv)
- [notes_flashcards.tsv](data/notes_flashcards.tsv)
- [grading.tsv](data/grading.tsv)
- [chat.tsv](data/chat.tsv)
- [openapi-operations.tsv](data/openapi-operations.tsv)

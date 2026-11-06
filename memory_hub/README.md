# Memory Hub

Memory Hub 是本地 Memory MCP 可选连接的独立 HTTPS 事件服务。本地 MCP 与
Hub 仅通过 `contracts/v1/` 中的版本化 JSON 合约通信；未配置 Hub 时，本地
读写仍保持离线可用。

- 架构、安全边界、部署步骤与当前实现进展：[`DESIGN.md`](DESIGN.md)
- 本地 MCP 安装、使用与 Hub 接入：[根目录 README](../README.md)
- 证书文件要求：[证书说明](certs/README.md)

## 开发

```bash
uv sync --all-extras --dev
uv run pytest
uv run memory-hub-api
uv run memory-hub-worker
```

默认 Brief Provider 为 `fake`，不会调用外部 LLM。

### 完整测试（隔离数据库）

完整测试需要 PostgreSQL 16（测试用户可建库、删库）、Node.js 18+ 和 SQLite
3.35+。**绝不能使用生产数据库 URL**：集成测试会创建、删除数据库并演练迁移及降级。
推荐使用独立测试 Compose；它安装 Hub dev 依赖及本地 MCP 依赖，使用内存中的一次性
PostgreSQL，不读取生产配置、不发布端口，也不加入生产网络：

```bash
docker compose -p memory-hub-tests -f compose.test.yaml up --build --abort-on-container-exit --exit-code-from tests
docker compose -p memory-hub-tests -f compose.test.yaml down
```

无论测试成功或失败，都执行第二条命令清理临时容器；不要添加数据卷删除选项。
验收要求全部通过且零跳过，包括 PostgreSQL 并发、迁移与 Node.js 页面测试。
命令契约测试使用仓库内固定预期，覆盖 32 步生命周期与幂等重放，不依赖仓库外
HubTaskRunner 的测试替身，也不宣称验证该外部替身的一致性。

真实 LLM Brief 默认采用成本控制：用户与项目 brief 分别在最后一个相关事件后
120 秒、300 秒再生成；单次请求最多约 6,000 输入 token、800 输出 token；同一
项目所有 brief 共享每日 100,000 token 的持久化额度。所有值均可通过
`BRIEF_*` 环境变量调整，见 [`.env.example`](.env.example)。额度按调用前的
保守估算预留，因此失败或超时请求仍会计入当日额度，避免重试绕过成本上限。

## 生产部署

Docker 仅通过 Caddy 在主机公开 `80/443`；PostgreSQL、API 与 Worker 始终
处于 Compose 私有网络。部署前需将 DNS `A` 和/或 `AAAA` 记录指向服务器，
并在云安全组或防火墙放行 TCP `80`、`443`。

将签发的 `<public-hostname>.crt` 和 `<public-hostname>.key` 放入 `certs/` 后，执行：

```bash
cd memory_hub
./bootstrap.sh memory.example.com <project-id> <user-id>
```

Bootstrap 会生成受限权限的 `.env`、组装 Caddy 证书链、启动并等待服务健康，
然后在仓库根目录创建权限为 `0600` 的两个本机文件：`user_config.local.json`
（本地身份）与 `shared_memory.local.json`（远端连接，含仅首次显示的最小权限
Token）。两者不能提交、复制到日志或发送到聊天中。再次运行不会覆盖现有本机配置，
也不会新增活动 Token。

## 只读共享记忆 Web 面板

API 内置一个只读面板，实时展示 **LLM 整理的共同记忆**（项目级简报）与最近
共同可见事件（`shared` / `project_shared` / `org_shared`），**永不展示任何
用户的个人 scope 内容**：

- 页面：`https://<host>/shared`（纯静态单文件，无外部 CDN 依赖）
- 域名首页 `/` 自动跳转至 `/shared`，保留共享链接的 Token fragment。
- 数据端点：`POST /v1/shared-feed`（需要 `context:read` scope）
- 项目范围来自 Token，请求体不参与身份判定；请求体含多余字段会被拒绝（422）

可直接使用当前已有的 Token，不需要为面板另外创建专用 Token；只要该 Token 包含
`context:read` 权限即可。把它放在 URL fragment 中即可分享链接：

```text
https://<host>/shared#token=<当前-token>
```

fragment 不会发送到服务器或写入访问日志。页面读取后会立即从地址栏移除 Token，
仅在当前页面内存中使用，并通过 `Authorization: Bearer …` 自动连接。页面不提供
手动 Token 输入或链接生成控件；链接缺少 Token、Token 无效或权限不足时会直接提示。
页面每 30 秒自动轮询。
顶部简报状态来自项目简报任务：只有实际处理中的任务显示“整理中”；排队、失败、
处理超时和等待每日额度分别提示。预算不足时保留已有简报并显示计划重试时间
（不是完成时间）；每日预算按 UTC 零点重置，即北京时间 08:00。
“待整理事件”统计当前可见范围内尚未处理的共同正文事件，不是全局序号差；
worker 已检查且输入未变化的事件不会被重复计入。此状态不包含其他用户的私有简报任务。
首次数据请求失败后仍会继续自动重试。任务图形组件延后加载，不阻塞链接验证、
留言板或任务详情；组件下载失败时仅在任务轨迹区提示。Caddy 为页面、脚本与
JSON 响应启用 Zstandard / gzip 压缩，降低慢速网络的传输开销。

如果当前 Token 不包含 `context:read`，也可以另行创建一个只读 Token（可选）：

```bash
docker compose -p <project-id> exec api \
	memory-hub token create --project <project-id> --user dashboard --scope context:read
```

> 提示：该 Token 只能读取项目共同内容，不能写入任何事件。分享链接仍包含凭据，
> 请仅分发给可信团队，且不要提交、公开发布或发送到不受信任的聊天中。

## Project Board API

Hub 提供轻量协作看板接口，用于跨 Agent/成员同步「待处理事项、回复与关闭状态」。
所有 Board 数据都按 Token 绑定的 `project_id` 强隔离；请求体不能覆盖项目身份。

- `POST /v1/projects/{project_id}/board/query`：查询看板（需要 `context:read`）
- `POST /v1/projects/{project_id}/board/post`：发布主题（需要 `events:write`）
- `POST /v1/projects/{project_id}/board/reply`：回复主题（需要 `events:write`）
- `POST /v1/projects/{project_id}/board/resolve`：关闭主题（需要 `events:write`）

请求与响应使用 `contracts/v1` 对齐的数据结构，接口会执行内容安全检查（例如密钥样式
字符串拦截），并继承 API 的速率限制策略（超限返回 `429`）。

可见性与边界：

- 仅返回当前 `project_id` 下的数据。
- 可按 `task_id` 和状态筛选；默认最多 20 项，最大 50 项。
- 默认正文只返回 512 字符预览且省略 `references_json`；只有显式传入
	`include_content=true` / `include_references=true` 才返回详情。
- `reply` 与 `resolve` 必须命中同项目内的现有 `post_id`，跨项目访问会被拒绝。

本地 MCP 直接公开 `memory_board_read` 与 `memory_board_write`，Agent 可通过工具发现获得
窄参数 schema。原有 `memory_read(operation="board")` 与
`memory_write(operation="board")` 继续兼容。

## Task Graph

Hub 是任务状态唯一的计算者。存储为 append-only `task_events`（任务内连续 `task_seq`）与
`tasks`、`task_attempts`、`task_submissions`、`task_reviews` 关系投影，外加命令幂等存档
`task_commands`、创建时写入的任务关系 `task_links` 和凭据设备绑定 `token_devices`。
迁移 `0016_hub_task_authority` 删除了 Graph 节点/边、`task_agents` 与 `task_sync` 记忆事件，不可降级。

写入只走 `POST /task-commands`：执行者身份由凭据绑定的设备名（Windows 计算机名）推导，
`channel=dispatch` 构造 `<设备名>-dispatch`，请求携带 `actor_id` 或派生字段一律 422；
共享凭据（`identity:delegate`）不能绑定设备。生命周期命令为 `create`、`assign`、
`claim`、`decline`、`report`、`block`、`resume`、`submit`、`review`、`reassign`、`cancel`；
除 `create`（可省略或传 0）与 `report`（只需 epoch）外都需要 `expected_version`，执行者动作还需要
`expected_assignment_epoch`。行为规格见 `openspec/changes/rebuild-hub-task-authority/specs/hub-task-authority/spec.md`。

只读端点均需要 `context:read`：

- `GET /v1/projects/{project_id}/tasks`：紧凑、游标分页的任务目录。支持 `state`（默认 `working`，可逗号分隔或 `all`）、`q`（通配符按字面匹配）、`assignee`（当前 Attempt 执行者精确匹配）、`order=updated|created`（`created` 为不可变排序，供设备全量扫描）、`cursor` 与 `limit`（≤100）；响应包含 `state_counts`、`agent_loads` 与 `next_cursor`（`null` 表示结束）。旧 `agent` 参数返回 422。
- `GET /v1/projects/{project_id}/tasks/{task_id}`：单任务详情，含 `links`、`attempts`、`submissions`、`reviews`、`recent_events`，各最多 50 条并返回 `*_total` 与 `truncated`。
- `GET /v1/projects/{project_id}/task-events`：读取 append-only Timeline，可用 `task_id` 和 `cursor` 分页；每条事件返回 `task_seq`、执行者、`agent` 标签与预期/实际 version/epoch。
- `POST /v1/projects/{project_id}/task-commands`（`events:write` 且凭据已绑定设备）：Hub 唯一任务状态机，见 `openspec/changes/rebuild-hub-task-authority/specs/hub-task-authority/spec.md`。
- `POST /v1/projects/{project_id}/device-binding`：迁移前签发的凭据由客户端自动一次性绑定本机设备名；同名幂等，换名 409。
- `GET /v1/projects/{project_id}/task-graph` 已退役，返回 410 `task_graph_retired`。

启用且可访问 Hub 的共享模式下，本地 MCP 先同步提交 Task Graph 命令到
`POST /v1/projects/{project_id}/events/batch`；Hub 接受后才写本地 SQLite，避免本地 Agent
伪造 claim/review/reassign。Hub 不可用时，`report` 与 `submit` 仍可本地记录并进入 Outbox，
其他协调状态变更明确拒绝。未启用 Hub 的独立本地模式不需要网络。

`/shared` 中的“任务工作区”先读取分页任务目录，默认显示进行中任务，也可筛选已完成或全部状态。
表格以固定行高呈现并按需加载更多页面，因此大量完成任务不会撑开工作区或挤占运行中任务。
选中任务后才读取其依赖、产出、Attempt、Agent、Submission、Review 轨迹和对应 Timeline；
Agent 负载来自目录的全局聚合，页面只使用 Token 所在项目的共享任务数据。

## 响应大小治理

- `POST /v1/shared-feed` 默认最多 20 项、最大 50 项；事件正文默认 512 字符预览，
	`include_content=true` 才返回全文。Brief markdown 最多 4,000 字符，
	`include_brief_details=true` 才返回 structured brief。
- Context 默认最多 10 项、最大 20 项，并且 `include` 最多选择 6 个白名单区段。
- Board 使用上述紧凑默认值。`/shared` Web 面板显式请求 UI 所需的 Task Graph 与事件详情，
	不代表 agent/MCP 默认响应。
- MCP 在 Hub 响应之外还有统一的 `12,000` 字符与约 `3,000` token 最终预算；截断仍
	返回有效 JSON 和预算元数据。

这些限制约束派生查询和网络传输，不删除 append-only `MemoryEvent`。大型项目应通过 task、
Agent 或时间窗口缩小查询，再按需请求详情；Task Graph 不自动注入 task context。

## 验证与运维

```bash
# 查看容器和 API 健康状态
docker compose -p <project-id> ps
curl --fail https://memory.example.com/healthz

# 跟踪运行日志
docker compose -p <project-id> logs -f api worker caddy

# 更新镜像或代码
docker compose -p <project-id> up -d --build

# 停止服务，保留 PostgreSQL 数据卷
docker compose -p <project-id> down

# 备份数据库
docker compose -p <project-id> exec -T postgres \
	pg_dump -U memory_hub memory_hub > memory-hub-backup.sql
```

以下命令会删除全部服务数据，无法恢复：

```bash
docker compose -p <project-id> down -v
```

## Token 与团队身份

受信任的内部团队可以为同一项目使用一个带 `events:write` 和 `context:read` 权限的
共享 Token。该 Token 还必须显式包含 `identity:delegate` 权限；没有该权限时，Hub
会忽略 `X-Memory-User-ID` 并使用 Token 自身的用户身份。客户端将
`user_config.local.json` 的顶层 `user_name` 作为请求中的
`user_id`（`shared_memory.local.json` 不配置 `user_id`），Hub 用它归属事件、
过滤个人事件并维护个人 Brief。该模式降低接入成本，但不验证 `user_id` 的真实性，
不适用于不互相信任的成员。

需要强身份边界时，仍可为每位成员签发独立 Token；事件和 Context API 保持兼容。

为一个本地 MCP 用户签发最小权限 Token：

```bash
docker compose -p <project-id> exec api memory-hub token create \
	--project <project-id> \
	--user <user-id> \
	--scope events:write \
	--scope context:read \
	--scope identity:delegate
```

Token 明文仅在创建时输出，服务端只保存哈希，因此无法再次查询。查询 Token ID、
用户、权限与状态：

```bash
docker compose -p <project-id> exec api memory-hub token list --project <project-id>
```

丢失或泄露时创建替代 Token，再撤销旧 Token：

```bash
docker compose -p <project-id> exec api memory-hub token revoke --token-id <token-id>
```

## 安全要求

`.env`、`user_config.local.json`、`shared_memory.local.json`、证书、私钥和 CSR
已被 Git 忽略。发布前应在仓库根目录执行：

```bash
python scripts/check_public_tree.py
```
## 新设备自动注册

`POST /v1/projects/{project_id}/device-enrollments/auto` 要求同项目 `devices:enroll` 凭据（邀请凭据）。设备本地生成 Token 与查询秘密，只提交哈希；服务端在事务内直接签发绑定该凭据用户的独立设备 Token，仅授予 `events:write`、`context:read`，不继承注册或身份委托权限。重复请求幂等，撤销后的设备不能用同一申请重新激活。管理员可用现有 `token create --scope devices:enroll` 签发邀请凭据，秘密须通过受保护的本地文件或环境传递。

`POST /v1/projects/{project_id}/device-enrollments/open` 仅对 `MEMORY_HUB_OPEN_ENROLLMENT_PROJECTS` 明确列出的已有项目开放，无需凭据；服务端为每次注册生成独立 `open-device:` 用户身份，并按来源、全局和项目每小时配额限流。`GET /v1/projects/{project_id}/connection` 使用业务 Token 返回身份及业务能力，不返回 Token。

人工配对码审批路径（匿名待审申请、`/status` 查询、待审列表、`/decision` 批准接口、`devices:approve` 权限、`token grant-device-approval` 命令与 `Enable-DeviceEnrollment.sh`）已于 2026-11-05 删除。`device_enrollments` 表与既有迁移保留，仍用于上述两条自动注册记录；历史上已签发的设备 Token 不受影响，可用 `token revoke` 单独撤销。部署更新只需在现有部署目录同步源码并重建 API，不要重新 bootstrap 生产服务。公开仓库不依赖消费项目的脚本或部署状态文档。

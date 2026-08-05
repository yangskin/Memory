# Memory MCP Server

给 AI agent 使用的项目记忆服务器。Markdown 是真源，SQLite 是索引，MCP 默认只暴露两个工具：`memory_read` 和 `memory_write`。

高级维护能力走 CLI：重建、诊断、备份、压缩、快照、谱系、治理、LLM enhance。

---

## 1. 项目说明

- **定位**：跨会话项目记忆。用于保存事实、决策、观察、任务节点、错误摘要、交接信息。
- **真源**：`memory-bank/**/*.md`。SQLite、compiled 文档、runtime digest、cache 都是可重建派生数据。
- **目录**
  - `memory-bank/`：项目记忆，建议入版本管理。
  - `.ai-context/`：当前任务热上下文，不入版本管理。
  - `.ai-memory/`：配置、索引、备份、审计、缓存，不入版本管理；`.ai-memory/config.json` 可入版本管理。
- **工具表面**：普通 agent 只使用 `memory_read` / `memory_write`。
- **多人模式**：始终开启。`activeContext` 按用户分文件，`teamContext` / `progress` / `techContext` / `systemPatterns` 只沉淀共享或发布记录。
- **维护策略**：guard 超限、总预算超限、索引过期、事件膨胀、冷数据 retention 都由 auto-maintenance 处理。
- **测试状态**：`tests/memory_server` 当前通过 `862 passed, 5 skipped`。

实现代码在 `servers/memory_server/`。

---

## 2. 安装方式

约定：

- `<MemoryRoot>`：本目录，即包含本 README 的 Memory MCP 目录。
- `<RepoRoot>`：目标项目根目录。

`<RepoRoot>` 解析顺序：`-RepoRoot` 参数 → `MEMORY_REPO_ROOT` → 向上查找 `.git` / `.svn` / `.hg` / `*.uproject` / `*.code-workspace` / `*.sln` → 兜底到 `<MemoryRoot>/../..`。

### 2.1 一键 bootstrap（推荐）

```powershell
powershell -ExecutionPolicy Bypass -File <MemoryRoot>/scripts/bootstrap.ps1
```

自动完成：

- 创建 venv。
- 安装依赖。
- 设置稳定 user id。
- 写入 `<MemoryRoot>/user_config.local.json` 的 `user_name`。
- 合并 `<RepoRoot>/.vscode/mcp.json` 的 `project-memory-mcp` 配置。
- 执行一次 health 检查。

自动检测项目根失败时显式传入：

```powershell
powershell -ExecutionPolicy Bypass -File <MemoryRoot>/scripts/bootstrap.ps1 -RepoRoot <RepoRoot>
```

### 2.1.1 仅部署 venv + 依赖（`deploy.bat` / `deploy.ps1`）

只安装 Python 环境，不改 VS Code 配置：

```bat
cd <MemoryRoot>
deploy.bat
deploy.bat -ForceRecreate
deploy.bat -PythonExe "C:\Py311\python.exe"
deploy.bat -InstallDevDeps
deploy.bat -RegisterVSCode
deploy.bat -SkipInstall
deploy.bat -NoVerify
```

PowerShell 版本参数一致：

```powershell
powershell -ExecutionPolicy Bypass -File <MemoryRoot>/deploy.ps1
```

Python 版本要求与 `vendor/` wheel 标签一致，当前是 Windows CPython 3.11。运行时锁定在仍受维护的 MCP SDK v1 线（`mcp>=1.29,<2`）；MCP 2.x 是破坏性重写，迁移前不得由在线安装静默升级。`vendor/SHA256SUMS` 记录完整离线 wheel 集的校验值，刷新方式见 `vendor/README.md`。解释器查找顺序：

1. `-PythonExe`
2. UE 自带 Python
3. `py -3.11`
4. PATH 中的 `python` / `python3`

### 2.2 手动注册到非 VS Code 客户端

Codex 示例：`%USERPROFILE%\.codex\config.toml`

```toml
[mcp_servers.project-memory-mcp]
command = '<RepoRoot>/<MemoryRelToRepo>/.venv/Scripts/python.exe'
args = ['-m', 'servers.memory_server', '--root', '<RepoRoot>']
env = { PYTHONPATH = '<RepoRoot>/<MemoryRelToRepo>', PYTHONUTF8 = '1' }
```

修改 MCP 配置后重启客户端或新开会话。

### 2.3 验证

```powershell
# 启动 MCP server
powershell -ExecutionPolicy Bypass -File <MemoryRoot>/scripts/run_memory_server.ps1

# 跑全部测试
powershell -ExecutionPolicy Bypass -File <MemoryRoot>/scripts/run_memory_all_tests.ps1

# 检查当前记忆状态
$env:PYTHONPATH = '<MemoryRoot>'
<MemoryRoot>/.venv/Scripts/python.exe -m servers.memory_server.cli --root <RepoRoot> health --pretty
```

### 2.3.1 可选连接 Memory Hub

本地 MCP 默认完全离线运行，不配置 Hub 时不会发起任何网络请求，原有
`memory_read` / `memory_write` 行为不变。远端服务器连接使用独立配置文件，和本地
身份分离：

1. 本地身份：复制 `<MemoryRoot>/user_config.example.json` 为同目录的
   `user_config.local.json`，填写 `user_name`。
2. 远端服务器：复制 `<MemoryRoot>/shared_memory.example.json` 为同目录的
   `shared_memory.local.json`，并填写：

```json
{
  "enabled": true,
  "server_url": "https://memory.example.com",
  "project_id": "your-project-id",
  "token": "mem_v1.<token-id>.<secret>"
}
```

Hub `user_id` **不需要**（也不应）在 `shared_memory.local.json` 中配置：客户端始终
复用 `user_config.local.json` 中的顶层 `user_name` 作为 Hub `user_id`。

`user_config.local.json` 与 `shared_memory.local.json` 均已被 Git 忽略，Token 不得
写入 `.ai-memory/config.json`、MCP 配置或版本控制。重启 MCP 客户端后生效。环境变量
`MEMORY_HUB_TOKEN` 可在 CI 或临时调试时覆盖文件中的 Token。同步在后台进行：本地写入
不会等待网络；缺少 URL、项目 ID 或 Token 时自动保持禁用；认证失败时停止重试，直到
Token 更换。旧版 `user_config.local.json["shared_memory"]` 仍会被读取，用于平滑迁移。

团队可共用一个仅限该项目的 Hub Token；客户端始终将 `user_config.local.json` 的顶层
`user_name` 作为 Hub `user_id`，用于隔离个人事件和个人 Brief。该模式适用于受信任的
内部团队，服务端不验证该用户 ID 的真实性；未来需要更强身份保证时，可改为为每位成员
签发独立 Token，无需迁移既有事件格式。

Hub 管理员的服务器部署、运维、备份和 Token 签发方式见
[`memory_hub/README.md`](memory_hub/README.md)；中文的架构、部署与当前进展说明见
[`memory_hub/DESIGN.md`](memory_hub/DESIGN.md)。

### 2.4 Agent 规则配置（团队接入必做）

目标仓库的 agent 规则文件需要固定 Memory MCP 使用方式，例如 `AGENTS.md`、`.github/copilot-instructions.md`、`.cursorrules`。规则内容以 [3.2 推荐工作流](#32-推荐工作流) 的可复制提示词为准，不要在多个文档维护不同版本。

### 2.5 Git/SVN ignore（团队接入必做）

```gitignore
# === Memory MCP ===
.ai-memory/*
!.ai-memory/config.json
.ai-context/
memory-bank/compiled/
memory-bank/.tmp*
.vscode/settings.json
!.vscode/mcp.json
MCP/Memory/user_config.local.json
```

SVN 团队不要对 `teamContext.md` / `progress.md` / `techContext.md` / `systemPatterns.md` / `projectbrief.md` 加锁。服务端对共享文档使用 append-only 或 generated rebuild 策略。

Memory MCP **源码仓库自身**不得提交 `.ai-memory/`、`.ai-context/`、本机配置、运行事件、索引、缓存或任何消费项目的名称、路径、资产和测试数据。发布前及 CI 中运行：

```powershell
python scripts/check_public_tree.py
```

---

## 3. 使用方式

### 3.1 MCP 工具表面

| 工具 | 用途 |
|---|---|
| `memory_read` | 读取任务上下文与任务简报、读取文件、搜索、检索上下文、获取重要记忆、获取最新记忆、读取 runtime digest |
| `memory_write` | 写入 raw record、observation、checkpoint |

`memory_read(operation="task_context")` 是会话入口，返回 `context_token`、`task_id`、`task_run_id`、`active_context`、`current_task` 与 `task_brief`。`current_task` 来自 `.ai-context/current-task/{user}/{context_token}.md`，不是全局单文件。`memory_write` 只写结构化记忆，不做文件维护。guard、health、backup、compact、rebuild、snapshot、lineage、conflict、LLM enhance 等管理能力走 CLI。

读侧 MCP 响应默认保持紧凑：`task_context` 会去掉派生文档生成头和文件系统 `meta`；`retrieve_context` / `important_memories` / `latest_memories` / `search_records` 只返回继续任务所需的记忆正文、摘要和最小元数据。预算报告、召回 pipeline、prefilter stats、候选丢弃原因、完整 provenance 等诊断字段仅在 `include_diagnostics=true` 时返回。向量 / embedding 类 payload 即使在诊断模式也不会透出。

`latest_memories` 用于“当前项目中当前用户最新记忆”场景：显式 `user` 优先；否则使用 `context_token` 绑定的用户；再否则使用当前配置用户。返回按 `occurred_at` / `valid_from` / `updated_at` / `created_at` 倒序排列的结构化记录。默认可见范围包含当前用户的 `personal` / `session` / `user_private` 记录，以及项目共享记录；若只想看本人私有记录，传 `include_scopes=["personal","session","user_private"]`。如果记录的 `author` 是 agent 名但带有 `task_id`，服务端会从 task context 反查该 task 是否属于当前用户。无法可靠归属的孤立 agent-authored private 记录不会默认归入当前用户。

`task_brief` 是“当前意图 + 权威信息地图”，不是第二份项目真源。`task_context` 默认自动附带，也可用同一个 `context_token` 单独调用 `memory_read(operation="task_brief")` 读取同一冻结快照。v3.9 固定提供 Rules、宿主可选的 procedure 元数据指针、Source / Runtime、Validation、任务相关经验、连续性、冲突/缺口和下一步取证入口；规则正文、Skill 正文、源码正文、Active Context 正文和历史记忆完整正文均不注入。Memory MCP 不扫描仓库 Skill；只有宿主显式传入的 metadata-only catalog 才可作为非记忆 procedure 索引展示。记忆只以带 record id 的有界摘要进入简报，并继续标记为历史证据而非当前真源。`compact|standard|deep` 默认分别限制为约 12k/20k/32k 字符和 4k/6k/10k token；显式 `max_chars` / `max_tokens` 可放宽硬上限，但不会扩大槽位数量。deep 默认槽位为稳定经验 6、情景经验 6、相关近期任务 6、额外记忆线索 4、源码指针 14、验证入口 6；同一验证或经验记录不会跨 Validation、经验与记忆线索重复渲染。`include_task_brief=false` 可用于只需要身份握手的低延迟调用。

生成采用双通路：

- `.ai-memory/config.json` 启用 `llm_defaults.capabilities.generate_task_brief` 且 provider 配置可用时，LLM 生成意图摘要，并把确定性层已经筛选的稳定/情景经验合并成最多 5 条带 record id 的可追溯摘要；路径、规则、符号、验证状态与来源仍只能由确定性层发现和校验。提示词使用固定指令前缀与 `<current_task>` / `<authority_index>` / `<historical_memory>` 数据围栏，历史摘要按不可信证据处理，开放问题会回流 `missing_context`。Memory MCP 不发现或生成 Skill，也不把历史经验写成自我指令；其目标是提炼真正利于当前任务的决策、根因、验证结果、约束和未解决问题。
- capability 关闭、provider 缺失、超时、超预算、网络失败、结构协议非法或引用不存在的 record id 时，自动回退到确定性线路。确定性线路校验活动文件、源代码符号、规则入口、仓库 Skill 元数据与历史线索，隔离个人范围，并排除重复、乱码、疑似密钥、越界路径与冲突记录。
- LLM 失败只体现在 `task_brief.generation`，不能改变 `task_context.ok`、`current_task` 或 `active_context`。调用方可用 `brief_use_llm=false` 强制确定性生成。
- 同一 `context_token + task_id + 参数` 的简报持久化到 `.ai-memory/temp/task-brief-cache.sqlite`，因此 MCP 随时退出、重启后仍读取同一快照；`brief_refresh=true` 才显式重建。缓存只保存可再生派生视图，不影响基础记忆读写。
- 调用方可用 `brief_skill_catalog=[{name,description,path}]` 显式提供宿主已经发现的 procedure 元数据；简报只返回入口，不读取或自行发现 Skill 正文，这些入口也不计入记忆证据。`checkpoint(task_done)` 会把 `completed_at` 持久化进本机 task registry，历史任务仍只作为连续性指针，不能升级为当前事实。

### 3.2 推荐工作流

1. **开始前取上下文**：调用 `memory_read(operation="task_context")`，保存 `context_token` 并读取随附 `task_brief`。
2. **执行中按需读取**：需要项目背景、历史决策、验证结果时，用同一个 `context_token` 调 `retrieve_context`、`important_memories`、`latest_memories` 或 `search_records`。
3. **结束后写结果**：用 `memory_write(operation="record")` 写一条结构化总结；任务节点再写 `checkpoint`。
4. **管理动作走 CLI**：不要通过普通 MCP 写文件或维护派生文档。

检索默认使用 `ranking_version="v2"`：先区分业务领域记录与 Memory MCP/Task Brief 自评等元记录，再按强查询词的绝对命中数与覆盖率划分 relevance band，并在同一角色与 band 内参考相关性和 importance。领域 Task Brief 只用任务目标检索经验，不拼接活跃文件路径；先排除 band 0/1，再只保留距离本次最佳 query-match 不超过 0.10 的自适应窗口。仅当完全没有强证据时，最多降级装配 8 条 band 1 记录并显式标记 `weak_relevance_fallback_used`，不会为了填满大上下文而混入只命中模块名或三四个泛词的旧记忆。业务查询优先业务事实，记忆系统查询只使用记忆系统自身的工程经验。预算打包前合并 `auto_team_settlement` 跨 scope 镜像、精确重复与显式 supersede 链；代表记录继承组内最佳查询相关性，并用 `collapsed_best_record_id` / `collapsed_record_ids` 保留追溯。`facet_mode="hard"` 保持旧 API 的严格过滤语义；Task Brief 等内部推断使用 `facet_mode="boost"`，facet 缺失不会误删精确查询命中。发生 v2 内部异常时自动回退 v1；调用方也可显式指定 `ranking_version="v1"`。

### 3.2.A LLM 辅助 metadata 对齐（opt-in，§15.2-B）

为了在 agent 习惯性传入业务领域 tag（如 `sample_domain` / `sample_prefab`）时仍能把记忆落地，`memory_write(operation="record")` 与 `memory_read(operation="task_context")` 各暴露一个 **opt-in** 参数，默认关闭，启用且 LLM 已配置时生效，LLM 不可用时降级为原有行为且永远不静默改写。

- `memory_write(operation="record", llm_normalize_tags=True, ...)`：仅当请求 `tags` 含有不在受控词表中的值时触发。服务端会调用 `classify_record` 拿到合规 tag 建议，将 `requested ∩ allowed` 与 LLM 建议合并写入；被拒绝的业务词拼成 `tag1.tag2` 形态写到 `system_area`（仅当调用方未显式提供 `system_area` 时）。写入成功后返回字段：
  - `metadata_suggestion`：`{status: "ok"|"llm_unavailable"|"llm_failed"|"skipped", applied, requested_tags, accepted_tags, rejected_tags, final_tags, suggested_tags, suggested_record_kind, suggested_scope, suggested_system_area, confidence, rationale, model, message}`。
  - `warnings`：当且仅当真正发生归一化（`status == "ok"` 且存在 `rejected_tags`）时追加一条 `{code: "metadata_normalized_by_llm", from_tags, to_tags, rejected_tags, system_area, rationale}`。
  - LLM 不可用 / 调用失败时不改写 args，返回原 `invalid_input`，并附带 `metadata_suggestion` 帮调用方诊断。
- `memory_read(operation="task_context", llm_suggest_metadata=True, user_goal=..., active_files=[...])`：在原返回结构上额外追加 `suggested_metadata`（与上面同构），便于 agent 在动手前预先对齐 `record_kind` 与 tag。

设计要点：保持两工具 MCP 表面不变；不引入 `memory_enhance`；LLM 永远只是“建议器”，最终 tag 仍由服务端 schema 校验保证 ⊆ `tag_schema.allowed_tags`。

无 LLM 或未启用 `llm_normalize_tags` 时，调用方必须自行只传受控 tag；未知业务词不会被静默改写，会按原 schema 校验返回 `invalid_input`。当前项目可用 tag 由 `.ai-memory/config.json` 的 `tag_schema.allowed_tags` 配置，内置默认值由 `servers/memory_server/memory_config.py` 的 `DEFAULT_ALLOWED_TAGS` 提供。默认完整词表为：

当 `memory_write(operation="record")` 因未知 tag 被拒绝时，`invalid_input` 响应会附带 `invalid_field="tags"`、`rejected_tags`、`allowed_tags`、`tag_schema_version` 与 `hint`，便于 agent 直接重试，不必再翻 README 或配置文件。

```text
archive_candidate
asset_pipeline
build
handoff_ready
high_value
material
mcp
needs_validation
skill_possible
texture
ui
validation
workflow
```

业务领域、资产名、模块名、玩法名等非词表信息不要塞进 `tags`；优先写入 `system_area`、`asset_paths`、`module_names` 或正文。例如 `sample_domain` / `sample_prefab` 这类业务词应进入 `system_area="sample_domain.sample_prefab"` 或正文说明，`tags` 只保留 `asset_pipeline`、`material`、`workflow` 等受控分类。

给 agent 的保守规则：**不会选 tag 时直接省略 `tags`，不要发明 tag**。`tags` 只是受控分类，不是关键词检索字段；业务关键词放正文或 `system_area`。常用合法组合：

| 场景 | `record_kind` | 推荐 `tags` |
|---|---|---|
| 普通实现交接 / 任务完成总结 | `handoff` | `["handoff_ready", "high_value"]` |
| 架构 / 技术决策 | `decision` | `["high_value"]` |
| 测试 / 验证结果 | `validation_result` | `["validation"]` |
| 可复用流程 | `procedure` | `["workflow", "high_value"]` |
| 构建 / 工具链事实 | `note` 或 `decision` | `["build"]` |
| MCP / Memory 系统自身变更 | `handoff` / `decision` / `procedure` | `["mcp", "high_value"]` |
| 资产 / 材质 / UI 相关事实 | `note` | 从 `["asset_pipeline", "material", "texture", "ui"]` 中只选匹配项 |
| 仍需验证的问题 / 事故根因 | `incident` | `["needs_validation"]` |

### 3.2.B 失效 `context_token` 的正文抢救

`memory_write(operation="record"|"observation")` 如果收到失效 `context_token` 但正文非空，会先尝试从同一 user / workspace / agent 的任务上下文中按 `system_area`、正文关键词、active files、任务目标、最近活跃时间推断任务线：

- 高置信匹配（当前阈值 0.85）：自动重绑到推断出的 `context_token`，返回 `context_recovery.mode="rebound"` 与 `warnings[].code="context_token_invalid_rebound"`。
- 置信不足或无候选：正文仍写入 `task_id="recovered_invalid_context"` 的 raw record/observation，返回 `context_recovery.mode="orphan"` 与 `warnings[].code="context_token_invalid_recovered"`；原 token 会写入 `source_refs` 便于审计。
- 空正文或 `checkpoint`：继续返回 `invalid_context_token`，因为没有可抢救内容。

恢复写入的任务归属是推断结果，不应当作强身份事实；调用方应检查 `context_recovery`，必要时重新调用 `memory_read(operation="task_context")` 获取当前 token 后补写更精确记录。

`checkpoint` 的主语义是阶段触发，不是正文存储。若调用方误把 `content_markdown` / `content` 放进 `checkpoint`，服务端会先把正文保存为一条 structured record，再返回 warning 提醒下次应先 `record` 再 `checkpoint`；这样重要记忆不会因为误用而丢失。

可复制到 agent 规则的提示词：

```markdown
Before any development task, call `memory_read(operation="task_context", user_goal=<current request>, agent_id=<agent name>, active_files=<relevant files>)`, keep the returned `context_token`, and reuse it for every task-scoped memory read or write; during the task, read memory only when project background, prior decisions, root causes, or validation results are needed; before finishing, write one structured summary with `memory_write(operation="record", context_token=...)` covering outcome, changed files, validation, and remaining risk, then send `memory_write(operation="checkpoint", task_phase="task_done", context_token=...)` without a body; choose `record_kind` by meaning: `decision` for architecture or technical decisions, `handoff` or `note` for implementation handoff, `validation_result` for test or verification results, `incident` for bug/root-cause notes, and `procedure` for reusable workflow; prefer the default personal scope unless the caller explicitly needs a shared raw record, because high-signal personal decisions/handoffs/procedures can be auto-settled into derived `project_shared` summaries; tags are optional, so omit `tags` when unsure instead of inventing labels; when tags are useful, use only `.ai-memory/config.json` `tag_schema.allowed_tags` (default full set: `archive_candidate`, `asset_pipeline`, `build`, `handoff_ready`, `high_value`, `material`, `mcp`, `needs_validation`, `skill_possible`, `texture`, `ui`, `validation`, `workflow`); common safe choices are implementation handoff `record_kind="handoff", tags=["handoff_ready", "high_value"]`, decision `record_kind="decision", tags=["high_value"]`, validation `record_kind="validation_result", tags=["validation"]`, workflow/procedure `record_kind="procedure", tags=["workflow", "high_value"]`, build/tooling `tags=["build"]`, and MCP/Memory work `tags=["mcp", "high_value"]`; put business-domain words, asset names, module names, and feature names in `system_area`, typed metadata fields, or the record body instead of `tags`; if an LLM is configured and the tool schema exposes it, `llm_normalize_tags=True` may be used as an opt-in safety net for accidental non-vocabulary tags, but do not depend on it for normal writes; never store secrets, credentials, tokens, or private user data; never edit `activeContext/{user}.md`, `teamContext.md`, `progress.md`, `techContext.md`, or `systemPatterns.md` directly; use the CLI for administrative work.
```

### 3.2.C Project Board 推荐必要提示词

从 v0.5.12 起，MCP facade 新增 `board` operation（单一入口，按 `action` 分流 `post/query/reply/resolve`），用于多人协作留言，不替代正式 Memory 事实。

可复制到 agent 规则的提示词：

```markdown
At task start, use unresolved board items injected by `memory_read(operation="task_context")` as advisory coordination context. Board availability, remote delivery, and replies must never gate local work: if the service is unavailable or nobody replies, continue with the safest local path and record assumptions. Create a board post when a blocker, open question, handoff, or cross-agent risk would help others align; do not post routine progress noise. Query unresolved items when available to avoid duplicates, then use `memory_write(operation="board", action="post", post_type=<note|question|request|warning|handoff|proposal>, content_markdown=<message>, task_id=<task>)`. Reply on an existing thread when useful, and resolve it after the outcome is locally observed or validated; never wait for a reply or remote confirmation solely to advance task state. Board identity and project membership come from the configured Hub token; do not put identity data, API keys, private keys, bearer tokens, or database connection strings in board content. Board messages are non-authoritative, best-effort discussion items and must not be treated as verified facts.
```

最小调用示例：

```json
{
  "operation": "board",
  "action": "post",
  "post_type": "question",
  "content_markdown": "请确认网络接口修改影响",
  "task_id": "network"
}
```

```json
{
  "operation": "board",
  "action": "query",
  "filter": "unresolved",
  "task_id": "network",
  "max_items": 20
}
```

```json
{
  "operation": "board",
  "action": "reply",
  "thread_id": "<thread-id>",
  "reply_to": "<post-id>",
  "content_markdown": "回复内容"
}
```

```json
{
  "operation": "board",
  "action": "resolve",
  "post_id": "<post-id>"
}
```

### 3.3 派生文档与自动维护

### 3.3.1 Raw Record Packing

默认按日期 pack 写 raw record。即使旧项目的 `.ai-memory/config.json` 没有 `record_packing` 段，结构化写入也会按目标目录追加到日期 pack 文件。没有任务信号时，personal 记录仍写入用户每日 pack，例如 `memory-bank/people/alice/packs/20260512-001.md`；带 `task_id` 或 `branch` 时，personal 记录按任务/分支分桶，例如 `memory-bank/people/alice/packs/task-123/20260512-001.md`。shared 记录按 `author + task_id/branch` 分桶，例如 `memory-bank/shared/packs/alice/task-123/20260512-001.md`。

这个策略用于平衡多人/多 agent 冲突和碎片数量：不同任务不会争抢同一个用户每日 pack；同一任务内的多个写入仍合并到同一个日期 pack，避免按 agent run 或单条 record 生成大量碎片文件。每条记录仍保留独立 Front Matter 和 `id`，读取、`search_records`、key-doc rebuild、lineage/governance 会把 pack 内条目展开为逻辑记录。

配置示例：

```json
{
  "record_packing": {
    "max_record_chars": 2000,
    "max_pack_chars": 64000,
    "archive_after_days": 90,
    "archive_pack_max_chars": 1048576,
    "max_active_pack_files": 500,
    "max_single_record_files": 2000,
    "max_archive_pack_files": 2000
  }
}
```

`max_record_chars` 是诊断/调参参考值，不再决定是否打包。写入只受 `max_pack_chars` 限制；当前 pack 超过 `max_pack_chars` 时滚动到 `YYYYMMDD-002.md`。单条记录本身超过 `max_pack_chars` 会被拒绝并返回诊断错误，避免重新生成无限增长的独立 record 文件。

长期维护：

```powershell
# 预览：把历史单条小记录合并到日期 pack
python -m servers.memory_server.cli pack-existing-records

# 执行迁移
python -m servers.memory_server.cli pack-existing-records --apply

# 预览：把超过 archive_after_days 的日期 pack 合并到 1 MiB 归档 pack
python -m servers.memory_server.cli compact-record-packs

# 执行归档合并
python -m servers.memory_server.cli compact-record-packs --apply

# 查看 / 清空异步关键文档重建队列
python -m servers.memory_server.cli key-doc-jobs
python -m servers.memory_server.cli key-doc-jobs --drain --max-jobs 5
```

归档 pack 写在 `memory-bank/archive/record-packs/YYYYMM-001.md`，仍属于 `memory-bank` 真源，`search_records`、runtime digest、key-doc rebuild 仍能读取；只是物理文件数量从“每天/每用户/每目录”继续合并到“每月若干个 1 MiB 文件”。

关键文档是派生视图：

- `memory-bank/activeContext/{user}.md`
- `memory-bank/teamContext.md`
- `memory-bank/progress.md`
- `memory-bank/techContext.md`
- `memory-bank/systemPatterns.md`

它们由 raw record、snapshot、observation 重建。人工编辑会在 rebuild 前归档到 `memory-bank/archive/manual-edits/`。

`activeContext` 是 user-scoped 暖上下文：

- 写 `memory-bank/activeContext.md` 会重定向到 `memory-bank/activeContext/{user}.md`。
- 超过 guard 阈值时自动归档完整原文。
- live 文件会被压缩到预算内。
- 归档保留在 `memory-bank/archive/activeContext/{user}/`。
- `rebuild-key-docs --target activeContext --user <user>` 只重建该用户的 activeContext，不覆盖顶层 `activeContext.md`。

`teamContext` / `progress` / `techContext` / `systemPatterns` 是团队共享沉淀：

- 只从 `scope=shared|project_shared|org_shared` 或 published 记录生成。
- `personal` / `session` / `user_private` 默认不会进入团队文档。
- 高价值个人记录（如 `decision` / `handoff` / `procedure` / `incident` / `validation_result`，或带 `high_value` / `mcp` / `workflow` 等团队标签）会自动生成一条派生 `project_shared` 摘要，再进入团队文档。
- `session` / `user_private` / candidate / distilled / 含明显密钥信号的记录不会自动提升。
- 需要共享完整 raw 时，写入时显式使用共享 scope，或通过 validate/publish 流程提升。

自动沉淀默认开启：

- 成功结构化写入达到阈值后重建关键文档。
- checkpoint 命中 `phase_triggers` 时可立即重建。
- `auto_team_settlement` 会先判断是否需要派生团队摘要；LLM 可用时参与判断和摘要，失败时回落 deterministic。
- LLM 可用时也可参与是否重建 key documents 的 gate；失败时回落 deterministic。
- 自动重建路径默认 `async=true`：MCP 写入只把 rebuild job 持久化到 `.ai-memory/key_document_rebuild_jobs.json`，立即返回；key documents 是最终一致派生视图。
- 异步 job 按 user/renderer/guard 策略合并 pending targets；drain 时使用单 worker lock，避免旧慢任务覆盖新任务。
- job 带 source watermark；若 rebuild 期间有新 raw 写入，完成时标记 `stale_at_publish=true` 并自动补排一个最新 job。
- 自动重建路径默认 `guard_prefer_llm=false`：若派生文档超出 guard 预算，MCP 自动路径使用 deterministic 压缩；显式 CLI rebuild 仍可使用 LLM guard 压缩。

### 3.4 多人协作

多人安全模式始终开启。

用户解析优先级：

1. `MEMORY_MCP_USER`
2. `<MemoryRoot>/user_config.local.json["user_name"]`
3. `.vscode/settings.json["memory-mcp.userName"]`（旧配置兼容）
4. `USERNAME` / `USER`
5. `unknown`

推荐把稳定 user id 放在 Memory 项目根目录的本地配置中，和 LLM 本地配置并列：

```powershell
Copy-Item <MemoryRoot>/user_config.example.json <MemoryRoot>/user_config.local.json
```

```json
{
  "user_name": "alice"
}
```

未配置稳定 user id 时，结构化读写会返回 `user_not_configured`，除非 `.ai-memory/config.json` 设置：

```json
{
  "mcp": {
    "allow_unknown_user": true
  }
}
```

并发策略：

- 每个写目标有跨进程文件锁。
- 写入使用同目录临时文件 + atomic replace。
- `if_match` 支持 SHA-256 乐观锁。
- shared key documents 使用 append-only 或 generated rebuild。
- personal / user_private 记录按 author 隔离。
- task hot context 按 `context_token` 分文件，避免多个 agent 共享全局 `.ai-context/current-task.md`。

### 3.5 LLM 接入（可选）

LLM 不在主路径上。没有 LLM 时，写入、FTS 检索、deterministic rebuild、guard、auto-maintenance 都正常工作。

配置：

```powershell
Copy-Item <MemoryRoot>/llm_config.example.json <MemoryRoot>/llm_config.local.json
```

也可使用环境变量：

- `MEMORY_LLM_API_KEY`
- `MEMORY_LLM_BASE_URL`
- `MEMORY_LLM_MODEL`

LLM 接入点：

- `memory_write(operation="record", distill=true)`
- `memory_write(operation="record")` 的 `auto_team_settlement` gate/summary
- `memory_read(operation="retrieve_context", summarize=true)`
- `memory_read(..., rewrite_query=true)`
- CLI `rebuild-key-docs --renderer auto|llm`
- CLI `weekly-snapshot-rebuild --narrative`
- CLI `monthly-snapshot-rebuild --narrative`
- CLI `enhance`
- guard 超限压缩中的 `guard_compaction`（CLI / 显式 rebuild 默认可用；MCP 自动重建默认不用 LLM）

LLM 输出只进入 distilled / generated / summary 层，不覆盖 raw 真源。

---

## 4. 项目设计思想

1. **真源可读**：记忆是 Markdown 文件，不绑定数据库。
2. **派生可重建**：索引、digest、关键文档、快照都能从 raw 重新生成。
3. **写入可审计**：写入路径统一做校验、锁、预算、备份、原子替换、事件记录。
4. **上下文有预算**：guard 控制热上下文、关键文档和总预算；超限自动压缩。
5. **多人默认安全**：user-scoped activeContext、teamContext 共享沉淀、shared append-only、author isolation 是默认行为。
6. **LLM 是增强层**：LLM 可摘要、压缩、重写查询、生成快照说明；不能改 raw 真源。
7. **普通工具少**：agent 只需要 `memory_read` / `memory_write`；管理动作走 CLI。
8. **失败可降级**：LLM、embedding、索引、cache 失败时回落 deterministic 或 FTS，不阻断主链路。

LLM 能力边界：

| 非 LLM | LLM | Hybrid |
|---|---|---|
| raw 写入 | 摘要 | guard 压缩 |
| Front Matter / Schema | query rewrite | key-doc rebuild |
| lock / backup / event | snapshot narrative | conflict explanation / team settlement gate |
| FTS / scoring / lineage / budget | classify / extract / merge | retrieve summary |

---

## 5. 开发状态与计划

当前已具备：

- 两工具 MCP 表面：`memory_read` / `memory_write`。
- CLI 管理面。
- Markdown raw 真源。
- SQLite FTS。
- CJK bigram / trigram 检索。
- metadata facet 预筛。
- budget-first retrieval。
- key documents rebuild。
- auto-maintenance。
- guard 单文件和总预算治理。
- LLM capability runner。
- query rewrite / summarize recall / snapshot narrative。
- local ONNX embedding provider。
- deterministic hash embedding fallback。
- multi-user always-on。
- lock lease metadata。
- retention archive。
- Windows legacy stdout UTF-8 fallback。

当前维护重点：

1. 维护 P1 基座的 crash-recovery / concurrency / fault-injection 回归集。
2. 用真实任务历史观察 `project_reflection` 的误发布率和候选保留率，再调整阈值。
3. 为 baseline 更新增加确认策略或 LLM 建议策略。
4. 在真实模型下载后运行 `scripts/eval_recall.py` 建立 local-onnx 召回基线。
5. 达到规模阈值后再启动 RAG Phase 3：GPU EP / 量化 / HNSW。

### P1 系统基座（常驻进程可随时终止）

- `ReloadableMemoryConfig` 在每个 MCP 请求和 worker tick 检查配置签名；新配置损坏时继续使用 last-known-good，并在 diagnostics 暴露错误。
- key-document 与 project-reflection 均使用持久 JSON 队列：`pending → running(lease) → done | pending(retry) | dead`。进程被杀后，下一次启动回收过期 lease；队列保留 `.bak` 恢复副本和 dead letter。
- 后台 worker 是 daemon，启动有宽限期，所有步骤都有顶层异常隔离。LLM、索引、编码审计或队列故障不会改变 `memory_read` / `memory_write` 的主结果。
- compact apply 使用 `prepared → committed | conflict` 恢复日志、源 SHA compare-and-swap、原文件备份和原子替换。下次启动会重放源未变化的 prepared 事务。
- SQLite FTS 保存完整语料文件签名；搜索前校验 source manifest。索引缺失时普通 retrieval 继续 Markdown fallback，索引损坏或过期时可重建。
- 所有新记录拒绝 NUL、U+FFFD 和非法 UTF-8。编码修复必须显式指定 codec、默认 dry-run，并先做原始字节级备份。

项目级反思只在 `checkpoint(task_done|test_failed)` 后写入队列。worker 从同一 `task_id` 的结构化记录收集证据（默认最多 256 条 / 1,000,000 字符，利用大上下文但不强制填满），依次执行 extractor 和 adversarial critic；确定性门禁再次校验证据 ID、类型、置信度、秘密信号和重复项。提案动作协议为 `CREATE / UPDATE / MERGE / SUPERSEDE / REJECT`：更新类动作只允许指向尚未被替代的 `project_shared + background_reflection + replaceable=true + authoritative=false` 记录；落盘始终新增记录并写 `supersedes`，绝不原地改写或删除旧记录。只有高置信且具有 `validation_result` 证据，或至少两个不同任务重复支持的提案，才能发布；`REJECT` 不写记录，其余未过门禁的候选只保留在 durable job result，等待 Curator。

运维命令：

```powershell
python -m servers.memory_server.cli --root <repo> key-doc-jobs
python -m servers.memory_server.cli --root <repo> reflection-jobs
python -m servers.memory_server.cli --root <repo> reflection-backfill --limit 100
python -m servers.memory_server.cli --root <repo> reflection-curate
python -m servers.memory_server.cli --root <repo> encoding-audit
python -m servers.memory_server.cli --root <repo> recover-transactions
python -m servers.memory_server.cli --root <repo> worker-once
```

参考实现选择：当前 P1 不引入外部运行时依赖。借鉴 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 的后台复盘与写入审批、[Hermes Self-Evolution](https://github.com/NousResearch/hermes-agent-self-evolution) 的“trace → candidate → eval → constraints”闭环、[LangMem](https://github.com/langchain-ai/langmem) 的 background manager，以及 [Graphiti](https://github.com/getzep/graphiti) 的 episode provenance/temporal validity。现有 Markdown 真源、任务上下文、LLM runner 和 MCP 生命周期已经覆盖 P1 所需边界；直接接 LangMem 会引入 LangGraph store，Graphiti 会引入图数据库和 embedding 运维，均留到规模/时态关系需求触发后评估。

规模阈值：

- `chunks >= 100000`
- 全量向量重建 `>= 10min`
- 检索负载 `>= 20 QPS`

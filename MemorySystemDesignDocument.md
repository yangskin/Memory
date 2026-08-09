# MCP 记忆系统设计文档

> 状态：v0.12 接口收敛。P0/P1/P2、P3 结构升级、P3 hardening、P4 LLM pipeline、P4-B LLM enhance 内部/CLI 能力、P4-C 关键文档可重建、P5 RAG Phase 1–2c、P4 LLM 软增强余项（v0.10.0）、v0.10.1 团队接入、v0.11.0 RAG/LLM 首批、v0.11.1 两个保留项（`PRESETS` sha256 + `llm_smoke.py`）均已落地；普通 agent 默认 MCP 表面包含通用 `memory_read` / `memory_write` 与专用 `memory_board_read` / `memory_board_write`。多人沉淀模型已拆分为 per-user `activeContext/{user}.md` 与 shared `teamContext.md`。
>
> 日期：2026-05-11
>
> 配套文档：实现细节与版本流水见 [README.md](./README.md) 与 [DEVLOG.md](./DEVLOG.md)；原始设计稿备份见 `MemorySystemDesignDocument.md.bak`。

## 0. 当前基线（一句话总结）

仓库已具备：5 个默认 MCP tool（通用 `memory_read` / `memory_write`，专用 `memory_board_read` / `memory_board_write` / `memory_task_sync`）+ CLI 维护/同步/诊断/重建/谱系/LLM enhance 入口 + 三层目录（`memory-bank/` / `.ai-context/` / `.ai-memory/`）+ 路径安全 + 原子写入 + 跨进程文件锁 + SQLite FTS（含 CJK bigram/trigram）+ schema v2 + 时间快照 + lineage + importance scoring + budget-first retrieval + dao/fa/shu 视图 + LLM map-reduce pipeline + 6 项 read-only LLM 增强能力 + verified embedding presets + gated 真 LLM smoke。普通 agent 的心智模型固定为：读取上下文/检索用 `memory_read`，写入记忆/任务 checkpoint 用 `memory_write`，跨 Agent 留言使用专用 Board 工具，协作任务生命周期使用 `memory_task_sync`。详细历史见 DEVLOG。

### 0.1 Graph Agent Task System（当前增量）

`memory_task_sync` 独立于既有 Memory 知识图：本地 SQLite 保存 append-only Task Event、
Task/Agent/Attempt/Submission/Review Projection 与 Graph Bundle；Hub 使用同构的规范化事件表和
投影。每次命令以 `command_id + expected_version + expected_assignment_epoch` 防止重复与旧执行者
迟到提交。共享 Hub 启用时，协调命令先由 Hub 同步裁决再落本地；Hub 离线时只允许 `report` /
`submit` 留作待同步记录。Memory 的普通本地写入继续完全不等待远端。

## 1. 文档目标

- 用户/AI 只写 raw，关键文档由系统自动重建（无感原则）
- 个人记忆沉淀 + 协作交接 + 输出"小而准"重要记忆供 LLM 复用
- 全文检索与历史追溯，与 Git 工作流兼容
- 通过 MCP 暴露统一接口；无 LLM 时基础链路完整可用

## 2. 核心设计原则

### 2.0 无感原则（最高优先）

| 层 | 谁写 | 是否可改 | 例子 |
|---|---|---|---|
| **raw 真源层** | 人类 / agent / 工具 | 写一次冻结（`immutable=True`） | observation / decision / note / incident / claim / rule |
| **派生关键文档层** | 系统自动重建 | 整文件可重建可丢弃 | `activeContext/{user}.md` / `teamContext.md` / `progress.md` / `techContext.md` / `systemPatterns.md` / runtime digest |

三档重建降级：`LLM` → `本地小模型 / embedding 模板` → `deterministic`；任一失败自动跌落，全部失败时从 `compiled/snapshots/<doc>-<timestamp>.md` 回退；raw 始终不受损。

人工只在两种场景介入：以 `human` 身份新增 raw（修正/补充）；手动微调关键文档（下次 rebuild 前会自动归档到 `archive/manual-edits/`）。

### 2.0.1 Agent 自动沉淀（默认能力）

Memory MCP 的默认产品定位是 agent-first：用户不应记住何时手动运行 `rebuild_key_documents`，也不应要求每个 agent 永远正确判断团队 scope。因此新配置默认启用轻量自动沉淀：先保留个人 raw，再按保守规则/LLM gate 生成可替换的团队派生摘要，最后刷新派生视图。LLM 不直接改写 raw，也不发布正式系统共识。

触发策略：

1. **写入计数触发**：每 `key_documents.auto_rebuild.after_successful_writes` 次成功结构化写入后触发，默认 5 次；默认统计 `record` / `observation` / legacy `memory_write_record`，不统计普通 file 写入。
2. **任务节点触发**：agent 可用 `memory_write(operation="checkpoint", task_phase=...)` 上报阶段；`plan_confirmed` / `test_passed` / `stable_pattern_found` / `task_done` 等阶段可不等计数直接触发。
3. **团队派生提升**：`key_documents.auto_team_settlement.enabled=true` 时，写入后对 `decision` / `handoff` / `procedure` / `incident` / `validation_result` 等高价值个人记录做保守判断；命中后新增一条 `scope=project_shared`、`derived_from_record_ids=[raw_id]` 的派生摘要，原个人 raw 不变。
4. **LLM gate（可选）**：`llm_gate="when_available"` 时，团队提升和 key-doc rebuild 都先尝试 `auto_memory_gate`。团队提升阶段 LLM 只回答“是否可进入团队上下文、原因、团队摘要”；重建阶段 LLM 只回答“是否值得沉淀、目标关键文档、阶段/层次建议、原因”。失败或未配置时 deterministic fallback 继续执行，不能阻断写入。
5. **层次选择**：deterministic 路由按 `record_kind` / `task_phase` 选择沉淀层次：例如 `decision` → `activeContext+teamContext+progress`，`incident` → `activeContext+teamContext+techContext`，`procedure` / `stable_pattern_found` → `techContext+systemPatterns`，`task_done` → 全量关键文档。
6. **共享过滤**：`teamContext` / `progress` / `techContext` / `systemPatterns` 只从 `shared|project_shared|org_shared` 或已发布记录生成。`personal` / `session` / `user_private` raw 默认不直接进入团队文档；需要团队可见时，要么由 `auto_team_settlement` 生成派生共享摘要，要么显式写 `scope=project_shared|shared|org_shared` 或走发布流程。

默认配置保持低成本：`renderer="deterministic"`，无 LLM/API/token 成本；LLM 只做 gate/摘要增强，不默认参与关键文档正文生成。自动重建路径默认 `async=true`，`checkpoint` / 写入阈值触发时只持久化 rebuild job 并立即返回；key documents 是最终一致派生视图。job 通过 user/renderer/guard 策略合并 pending targets，drain 时用单 worker lock 串行发布；若 rebuild 期间 source watermark 变化，完成时标记 `stale_at_publish=true` 并自动补排最新 job。自动路径默认 `guard_prefer_llm=false`，派生文档超出 guard 预算时使用 deterministic 压缩；显式 CLI rebuild 仍可使用 LLM guard 压缩。旧项目可用 `key_documents.auto_team_settlement.enabled=false`、`key_documents.auto_rebuild.enabled=false` 或 `key_documents.auto_rebuild.async=false`、`key_documents.mode="manual|disabled"` 关闭。

### 2.0.2 多 Agent 任务上下文绑定（v0.12 已落地）

单个 MCP server 实例可能被 Codex、Copilot、Cursor agent、CI 子进程等多个 agent 交错调用。此时服务端**禁止**维护进程级全局 `current_task_id`：任何全局当前任务都会在 A agent 与 B agent 的连续调用间串线。

任务上下文采用显式握手机制，且不依赖 LLM：

1. **开始/解析任务**：agent 调 `memory_read(operation="task_context")`，传入可得线索：`agent_id`、`client_session_id`、`user`、`workspace_id` / MCP roots、`branch`、`active_files`、`user_goal`、`external_ref`、可选显式 `task_id`。
2. **确定性解析**：MCP 只用规则解析：显式 `task_id` > `client_session_id` 绑定 > `external_ref` 精确匹配 > workspace/branch/goal/active_files 指纹 > 高阈值文本与文件重合度匹配 > 新建 provisional task。LLM 不参与主路径。
3. **返回句柄**：MCP 返回 `task_id`、`task_run_id`、`context_token`、`confidence`、`matched_by`。`task_id` 表示真实任务；`task_run_id` 表示某个 agent 会话；`context_token` 是后续调用必须携带的上下文句柄。
4. **每次调用注入**：`memory_read` / `memory_write` 带 `context_token` 时，服务端从 token 注入 `user` / `author` / `task_id` / `branch`，并在结果中返回 `task_context`。
5. **无 token 保守**：没有 `context_token` 的调用不读取“当前任务”全局变量，只按显式参数或原有默认行为执行；新 agent 工作流必须先 `memory_read(operation="task_context")`。

存储边界：

- `.ai-memory/task-contexts.json` 保存 token、session binding、task registry，是本机可重建运行态索引，不进 Git。
- `.ai-context/current-task/{user}/{context_token}.md` 保存当前任务热摘要，按 token 分文件，不再使用全局 `.ai-context/current-task.md` 表示“当前任务”。
- 结构化记忆继续写入 `memory-bank/people/{user}/` / `shared/`，并通过 `task_id` 与 `branch` 参与检索过滤。
- 后续若需要团队级任务清单，可再把稳定 task manifest 派生到 `memory-bank/tasks/{task_id}/`；当前阶段先保证同一 MCP 实例内多 agent 不串任务。

验收门：

| 项 | 验收标准 |
|---|---|
| A. 无全局状态 | 任意两个 agent 交错调用，A 的后续读写只由 A 的 `context_token` 决定，不受 B 的 `begin_task` 影响 |
| B. 同任务合流 | 不同 agent 传入相同 `external_ref` 或高置信相同 goal/fingerprint 时共享同一 `task_id`，但拥有不同 `task_run_id` |
| C. 不同任务隔离 | 同一用户、同一文件、不同 goal 的两个 agent 必须得到不同 `task_id`，检索只返回各自任务记录 |
| D. 会话绑定 | 同一 `agent_id + client_session_id + workspace_id + user` 重复 `begin_task` 应返回原 context，不创建新 run |
| E. 错误安全 | 无效 `context_token` 返回结构化错误，不回退到其它 agent 的上下文 |
| F. LLM 非依赖 | 关闭 LLM、无网络、无 sampling 能力时任务解析完整可用 |

### 2.0.2.A 意图与权威信息地图任务简报（v0.14 已落地）

`task_context` 除了建立任务身份，还默认装配 `task_brief`。简报的职责是让 agent 明确“当前要做什么，以及去哪里查权威事实”，而不是复制项目背景或把历史记忆编译成第二真源。v3.9 固定结构为当前意图、Rules、宿主可选的 procedure 元数据指针、Source / Runtime、Validation、任务相关经验、连续性、冲突/缺口、下一步取证和质量边界。

生成线路：

1. **确定性权威地图**：发现并校验活动文件、源码符号、测试入口和项目规则；历史记忆以相关性检索为主线，`latest` 仅在检索故障时降级。经验检索只使用任务目标，不把活跃文件路径、模块目录或扩展名拼进主题查询；长查询相关度同时考虑绝对命中数与覆盖率，band 2/3 候选还要进入“距最佳 query-match ≤ 0.10”的自适应窗口。领域事实与 Memory MCP/Task Brief 自评等元记录采用意图感知角色隔离，稳定/情景摘要、近期任务、源码指针和验证入口各自有阈值与槽位。完全没有强证据时才最多降级 8 条 band 1 并暴露诊断，避免只命中模块名或少量泛词的旧记录填满大上下文。Memory MCP 不扫描仓库 Skill；宿主可显式传入 metadata-only procedure catalog，但它不属于记忆证据。连续性同时保留“上一全局完成任务”和“上一相关任务”。Active Context、规则、Skill、源码和历史记忆完整正文均不进入简报。声明权威按类型区分：指令看用户/AGENTS/Skill，当前实现看源码/配置/资产，验证看本次测试/构建/日志，历史看 checkpoint/事件/提交。
2. **LLM 意图与经验增强**：仅在 `generate_task_brief` capability 启用且 provider 可用时，让 LLM 生成意图摘要、未来完成条件、当前焦点、风险、假设和待确认问题，并把确定性层已筛选的经验合并为最多 5 条带 record id 的摘要。DONE 不得复述历史“已完成”声明，必须表达本任务未来可验收的结果；不能把 Automation 入口推断为 Rewind 或其它工具链。路径、规则、Skill、符号、验证结论和 provenance 不能由 LLM 声明；输入用固定指令前缀和 XML 风格数据围栏隔离当前目标、权威指针、连续性与不可信历史摘要，输出采用 `task-brief-v3.9` 固定行协议并做引用白名单、字段类型、字符上限与空输出校验。LLM 不可用或输出无效时，直接渲染同一批确定性有界摘要；开放问题以“待核验”进入 `missing_context`，而不是被隐藏。
3. **失败隔离**：disabled/unavailable/timeout/budget/parse/citation failure 全部回退确定性简报。即使装配器本身出现未预期异常，`task_context` 仍返回身份、`active_context` 和 `current_task`，只把 `task_brief` 标成结构化错误。
4. **冻结快照**：同一 `context_token + task_id + 请求参数` 的派生简报持久化到 SQLite，MCP 非正常退出或重启后仍复用同一快照；只有 `brief_refresh=true` 显式重建。缓存故障只影响简报缓存命中，不影响基础读写。
5. **渐进披露**：简报只给信息地图和缺口；agent 根据当前动作再读取具体规则或源码。`brief_skill_catalog` 可显式传入宿主已发现的 procedure 元数据，但服务端不自行发现或读取宿主 Skill；该索引不属于记忆。
6. **预算**：`compact|standard|deep` 使用不同候选数量、独立区段槽位和输出上限；deep 默认 256k token、显式请求最高 500k token，以适配 512k 上下文模型。deep 默认最多装配 stable=12、episodic=12、recent=10、leads=12、sources=24、validation=12。`max_chars` / `max_tokens` 是 ceiling，不是目标大小，不为接近预算而填充低价值上下文。

任务简报是只读、可丢弃、可重建的派生视图，不写新记忆、不覆盖 raw，也不把 LLM 文本、历史记忆或 Active Context 提升为当前系统指令。Memory MCP 的目标是总结真正利于当前任务开发的经验；不生成 Skill，不自动演化 Skill/Prompt/Code，也不把项目经验改写成对 agent 的祈使指令。

### 2.0.3 user-scoped activeContext 自动归档与压缩（v0.12 已落地）

`memory-bank/activeContext/{user}.md` 是当前用户的暖上下文，不是长期事实真源。超过 guard 阈值后只返回 warning 会导致下一轮 agent 继续读入过大的上下文，与“无感维护”目标冲突。因此 user-scoped activeContext 需要写后自动治理：

1. **触发点**：`memory_write` 写入 `memory-bank/activeContext.md` 经 user-scoped 重定向后，如果 live 文件超过该 guard target 的 `max_chars` 或 `max_tokens`。
2. **先归档**：将完整 live 文件写入 `memory-bank/archive/activeContext/{user}/activeContext-YYYYMMDDTHHMMSS-<suffix>.md`。文件头包含来源路径、触发原因、归档前字符/Token 数与阈值。归档文件入 Git，可审计。
3. **再压缩 live**：用 deterministic `warm_context` 规则生成 compact 版本，替换 live 文件；压缩结果必须仍是普通 Markdown，保留当前焦点、阻塞、近期决策和本周优先项。
4. **安全边界**：归档与 live 替换发生在原写入锁内；归档路径使用时间戳 + 随机后缀，避免并发冲突；失败时不吞掉主写入结果，返回 `active_context_auto_compaction.error` 供调用方处理。
5. **配置**：默认开启；可通过 `key_documents.active_context_auto_archive.enabled=false` 关闭。默认 `archive_dir="memory-bank/archive/activeContext"`，默认 `policy="warm_context"`，默认触发阈值沿用 guard target。
6. **不替代 raw**：activeContext 自动压缩只治理 live 派生视图。事实记忆仍应通过写入记忆工具写入 raw 记录，再由 `rebuild_key_documents(activeContext, user=...)` 派生当前用户视图。
7. **归档可召回**：`memory-bank/archive/activeContext/{user}/*.md` 会被 deterministic corpus 投影为低优先级历史 note，供 `rebuild_key_documents(activeContext)` 使用；它们不是 raw 真源，不参与治理晋升，只作为恢复上下文的历史证据。

验收门：

| 项 | 验收标准 |
|---|---|
| A. 超限自动处理 | 写入后超过 activeContext guard 阈值时自动归档完整原文并压缩 live 文件 |
| B. 不丢历史 | 归档文件包含写入后的完整超限内容，能从 archive 找回 |
| C. live 回到预算内 | compact 后 live 文件小于原文，并优先低于 `max_chars` |
| D. 多用户隔离 | `alice` 超限只归档/压缩 `activeContext/alice.md`，不触碰 `bob.md` |
| E. 可关闭 | 配置关闭时保留原 warning 行为，不归档不压缩 |
| F. 并发安全 | 轮转在 per-target file lock 内完成，归档名不会冲突 |
| G. 归档参与重建 | activeContext 关键文档重建能读取 `archive/activeContext/{user}` 中的历史 compact 投影 |

### 2.0.4 团队沉淀与个人上下文拆分（v0.12 已落地）

多人使用时，`activeContext` 不能同时表示“当前用户工作焦点”和“团队共享当前状态”。v0.12 将两者拆开：

1. **个人工作视图**：`activeContext` target 写入 `memory-bank/activeContext/{user}.md`，严格只选择当前用户 authored records 与该用户 activeContext archive。
2. **团队工作视图**：新增 `teamContext` target，写入 `memory-bank/teamContext.md`，只选择共享/发布记录，承载跨人焦点、共享决策与协调事项。
3. **系统沉淀过滤**：`progress` / `techContext` / `systemPatterns` 与 `teamContext` 一样，只从共享/发布记录生成；个人 scratch、session 观察、user_private distilled summary 不进入团队文档。
4. **自动团队提升**：个人记录默认停留在 `people/{user}`；当写入是高价值团队事实时，系统可额外写一条派生 `project_shared` 摘要。该摘要带 `provenance=auto_team_settlement`、`derived_from_record_ids`、`author`，可审计、可重建、可替换。
5. **硬跳过规则**：`session` / `user_private` / `archive` / candidate / distilled / 含明显密钥信号的记录不会自动提升；需要共享完整 raw 时仍应显式写共享 scope 或走 publish。
6. **兼容入口**：历史 `memory-bank/activeContext.md` 读写仍通过 user-scoped policy 重定向到 `activeContext/{user}.md`，但系统重建不再覆盖顶层 `activeContext.md`。

### 2.0.5 双工具 MCP 表面（v0.12 已落地）

普通 agent 默认只需要两个动作：读和写。过多 facade 会让 LLM 把“当前任务上下文”“检索”“文件维护”“写入记忆”“诊断/重建”混为一类，最终把普通记忆写成文件修改，或在任务开始时漏掉上下文绑定。

v0.12 MCP 表面固定为：

| 工具 | 责任 | 允许 operation |
|---|---|---|
| `memory_read` | 任务上下文、任务简报、文件读取、搜索、runtime digest、上下文召回、重要/最新记忆 | `task_context` / `task_brief` / `get_task_context` / `get` / `search` / `search_records` / `runtime_digest` / `retrieve_context` / `important_memories` / `latest_memories` |
| `memory_write` | 写入结构化 raw 记忆与任务 checkpoint | `record` / `observation` / `checkpoint` |

`memory_read(operation="task_context")` 是唯一推荐会话开始入口。它创建/解析任务身份，返回 `context_token`，并同时返回 `active_context`、`current_task` 与“意图 + 权威信息地图”`task_brief`。后续所有任务读写携带同一个 `context_token`。

`memory_write` 的边界：

| 项 | 设计 |
|---|---|
| 唯一职责 | 写入结构化 raw record、observation 或 checkpoint |
| 禁止字段 | MCP schema 不暴露 `path` / `mode` / `backup` / file write |
| 必填字段 | record/observation 需要 `content_markdown` 或 `content` |
| 默认字段 | `record_kind=note`、`scope=personal`、`status=raw` |
| 上下文 | 接受 `context_token`，服务端注入 `user` / `author` / `task_id` / `branch` |
| 落盘路径 | `scope=personal` 默认写 `memory-bank/people/{user}/mem_*.md` |
| 共享记忆 | 仅显式 `scope=project_shared|shared|org_shared` 时写 `memory-bank/shared/` |
| 失败模式 | user 未配置、token 无效、schema 非法、写入冲突、磁盘错误均结构化返回，不回退到文件写入 |

以下能力不再作为 MCP 工具暴露，统一迁到 CLI 或内部 API：

| 能力 | CLI / 内部入口 |
|---|---|
| file write / backup / compact / guard / health / index / migrate | `write-file` / `backup` / `compact` / `guard` / `health` / `rebuild-index` / `migrate` |
| compile / runtime digest rebuild / snapshots | `compile` / `runtime-digest` / `snapshot-rebuild` / `weekly-snapshot-rebuild` / `monthly-snapshot-rebuild` |
| key document rebuild | `rebuild-key-docs` |
| diagnose / lineage / conflicts / snapshot compare / artifact link | `config-diagnose` / `trace-lineage` / `list-conflicts` / `compare-snapshots` / `link-artifact` |
| LLM enhance | `enhance` 或 `memory_llm_enhance` 内部 API |

已移除 admin MCP 配置开关；旧 admin schemas 可以留在代码中供迁移参考，真实操作面是 CLI。

验收门：

| 项 | 验收标准 |
|---|---|
| A. 默认工具安全 | 默认 MCP `list_tools` 只出现通用记忆工具与专用 Board 工具，管理能力仍为 CLI-only |
| B. 单写入入口 | 调 `memory_write(content_markdown=...)` 必须写出 `memory-bank/people/{user}/mem_*.md` 或显式 shared record |
| C. 无路径逃逸 | MCP `memory_write` schema 不接受 `path` / `mode`；`operation=file` 返回 `admin_cli_required` |
| D. 多 agent 稳定 | 交错 agent 携带各自 `context_token` 写入时，author/task_id 不串线 |
| E. 指令一致 | README、Copilot instructions、AGENTS 不再引用 `memory_context.begin_task` 或直接写 `activeContext.md` |
| F. CLI 覆盖 | 被移出 MCP 的 admin/sync/diagnose/rebuild/lineage/LLM enhance 能力均有 CLI 或内部入口 |

### 2.1 真源不可变（Raw-Immutable）

- `raw`：`immutable=True` + `authoritative=True`，任何 LLM 不得修改或删除
- `distilled`：`immutable=False` + `derived_from=[raw_id, ...]`，可被任意 LLM 自动重写
- 实现层硬守卫：`assert_raw_writable()` 命中 raw 立即抛 `RawImmutableError`

### 2.2 其它原则（要点）

- **分层**：个人记忆 / 系统记忆 / 历史记忆 / 本地临时记忆 / 编译记忆
- **编译**：从结构化记录筛选 → 路由聚合 → 生成视图；以确定性为主，LLM 仅做软增强
- **预算优先**：先确定 `max_tokens`/`max_chars`/`max_items`，再在预算内选最重要内容；同一条记忆正文不在多 section 重复展开
- **自动化边界**：插件负责"动态记忆供给"，不在插件内维护项目规则真源
- **降级**：无 LLM / 本地小模型 / 云端 LLM 三档运行；LLM 不可用不能阻断基础读写、检索、编译

## 3. 系统边界

| 负责 | 不负责 |
|---|---|
| 记忆写入、结构化记录管理、标签分类、编译运行时视图、检索索引、重要记忆评分/保留/压缩、Git 兼容存储、MCP 暴露 | 替代项目文档/issue/代码仓库；维护项目规则真源；让 LLM 自动决定正式系统共识；让 LLM 直接发布正式 skill |

## 4. 记忆分层

| 层 | 用途 | 特点 |
|---|---|---|
| 个人记忆 | 个人事项、阶段结论、踩坑、交接 | 每人独立、进 Git、允许逐步沉淀 |
| 系统记忆 | 已验证规则、跨人共识 | 数量少、内容稳定、走 candidate→validated→published（兼容路径） |
| 历史记忆 | 归档结论、候选历史、事件日志 | 不默认注入，可检索可重编 |
| 本地临时 | 当前会话缓存、调试信息 | 不进 Git、生命周期短 |
| 编译记忆 | runtime digest / handoff / system digest / **关键文档**（`activeContext/{user}.md`、`teamContext.md` 等） | 三档渲染器生成、可重建可丢弃；头部带 `<!-- generated_by=memory-mcp ... -->` |

## 5. 目录结构

```text
memory-bank/
  activeContext/{user}.md        # 用户暖上下文派生视图
  teamContext.md                 # 团队共享当前上下文派生视图
  progress.md                    # 团队共享进度派生视图
  techContext.md                 # 团队技术上下文派生视图
  systemPatterns.md              # 团队模式/规则派生视图
  shared/                       # 已发布系统记忆
  people/{user}/                # 个人记忆
  candidates/                   # 系统/skill 候选池
  archive/                      # 降级与归档
  compiled/
    runtime/                    # system-digest / people/{user}-digest / task / branch
    publish/                    # *-candidates.jsonl
    snapshots/                  # 关键文档历史快照（rebuild fallback）

.ai-context/current-task/{user}/{context_token}.md
                                # 当前任务热上下文（不进 Git）

.ai-memory/                     # 不进 Git
  config.json                   # 配置（完全可选）
  search.db                     # SQLite FTS 派生索引
  events.jsonl                  # 事件日志
  compile-cache/  temp/  backups/  locks/
  ue_facets.json                # P1-2: UE facet 自动推断词典
  baseline.json                 # P2-1: 规模性能基线
  last_maintenance.json         # P0-3: auto-maintenance 时间戳
```

## 6. 存储格式

- **传输**：JSON（MCP 调用）
- **正文**：Markdown
- **落盘**：Markdown + YAML Front Matter
- **派生索引**：SQLite FTS5（CJK bigram/trigram 兜底，无新增依赖）
- **事件流**：JSONL

记录统一外壳字段（节选）：`schema_version` / `id` / `record_kind` / `scope` / `status` / `author` / `created_at` / `updated_at` / `tags` / `confidence` / `source_refs` / `task_id` / `branch` / `validated_by` / `last_used_at`。完整 schema v2 字段（occurred_at / valid_from-to / memory_tier / cognitive_level / derived_from_* / supersedes / conflicts_with / importance_score / facet 字段）见代码 `memory_records.py` 与 README §3.4。

记录类型：`note` / `event` / `claim_candidate` / `rule_candidate` / `handoff` / `skill_candidate` / `validation_result` / `system_rule` / `archive_record` / `observation` / `artifact_ref` / `incident` / `decision` / `procedure` / `snapshot_daily|weekly|monthly`。

标签为受控词表，只做路由不做真相判定。

## 7. 写入模型

- **硬元数据**（系统/调用方决定）：`author` / `created_at` / `source` / `task_id` / `branch` / `workspace` / `event_id` / 默认 `status` / 文件路径
- **软元数据**（LLM 可在 schema 内填）：`record_kind` / `tags` / `confidence` / `scope_hint` / `skill_possible` / `needs_validation`

写入边界（**raw 永远不能改、distilled 可以随便改**）：

- LLM **不能**：改/删 raw、把 distilled 提升为 raw、产出无 `derived_from` 的 distilled
- LLM **可以（无需人工确认）**：写新 raw（立即冻结）、写新 distilled、`supersede` 旧 distilled、自动归类/打标/生成 abstract/snapshot narrative

历史 `published` / `validated` / `dao` 走兼容路径，仍保留人工/规则限制（见 §10），不再是默认链路。

## 8. 编译模型

编译 = 从结构化记录筛选 → 按规则分组聚合 → 生成视图（不是 LLM 重写总结）。

主要编译目标（均已实现）：

| target | 用途 | 输出 |
|---|---|---|
| `runtime_digest` | 当前 agent 运行时上下文 | `compiled/runtime/` |
| `task_handoff` | 跨会话/交接 | `compiled/runtime/task/{task_id}-handoff.md` |
| `system_digest` | 团队共享 digest | `compiled/runtime/system-digest.md` |
| `publish_queue` | 治理审核入口 | `compiled/publish/publish-queue.md` |
| `daily/weekly/monthly_snapshot` | 时间快照 | `compiled/snapshots/...` |
| `rollback_context` / `review_queue` | 回退/回顾 | `compiled/...` |
| `dao_digest` / `fa_digest` / `shu_digest` | 按 cognitive_level 分层视图 | `compiled/...` |

编译原则：标签只做路由不做真相；优先读结构再读正文；产物可重建（不是唯一真源）；支持增量；默认面向运行时上下文压缩，正文优先抽 `Decision` / `Expected Behavior` / `Acceptance Checks` / `Next Step(s)` 等关键段；`body_mode="full"` 仅审计/调试时使用。

## 9. 检索模型

- **真源**：Markdown + JSONL + Front Matter；**派生索引**：SQLite FTS（CJK bigram/trigram）
- **流程**：解析 Front Matter → 元数据/tag 过滤 → FTS 全文 → 重排
- **预算策略**：入口必须接受 `max_tokens` / `max_chars` / `max_items`；按预算逐条装配（不是简单 `top_k`）；返回必须含 `budget_report` / `dropped_candidates` / `evidence_refs`
- **预筛优化**（v0.5.11）：索引健康时先用 SQLite metadata/facet 缩小候选集，再回 Markdown 真源做确定性排序；索引异常时无损回退全量扫描
- **回退**：标签缺失时仍可按 `scope` / `author` / 时间范围 / 关键词 / 全文检索兜底

## 10. 验证与发布治理（兼容层，已 attic）

> ⚠️ `candidate -> validated -> published` 是历史兼容流程，不是默认路径。新流程默认走 §2.1 的 raw + distilled。
> 仅历史数据迁移、跨团队共识发布等场景才走此链路。详细冻结范围、允许/禁止的改动见 §15.4。

链路：`raw -> candidate -> validated -> published -> degraded/archive`。

实现要点：

- `memory_validate_candidate` / `memory_publish_candidate` / `memory_archive_record`：临时文件 → `os.replace` → 删旧路径，全程原子。默认不暴露为 MCP；CLI 依然保留。
- `memory_delete_record` 只允许删 archived 记录，墓碑写入 `.ai-memory/tombstones.jsonl`。
- 多人安全策略始终开启：`activeContext.md` 自动按 `{user}.md` 分流；`teamContext.md` / `progress.md` / `techContext.md` / `systemPatterns.md` / `projectbrief.md` 默认 append-only 或 generated rebuild。旧配置无 `write_policy` 时通过 `multi_user.user_scoped_paths` / `shared_paths_policy` 兜底。单人使用只是只有一个有效 user 的退化情况，不再提供 `multi_user.enabled` 关闭开关；旧配置残留该字段时忽略。
- 维护工具：`memory_health_check` / `memory_migrate_records` / `memory_update_index`。
- 安全加固（v0.4.1 起，v0.5.4-0.5.6 加强）：`O_CREAT|O_EXCL|O_WRONLY` + fsync + `os.replace` + `FILE_SHARE_DELETE` 重试 + 长路径 `\\?\` 规范化 + `DiskFullError` 结构化错误 + `events.jsonl` 滚动归档；详见 DEVLOG。

## 11. MCP 接口

### 11.1 MCP 工具表面

当前默认 MCP 只暴露 2 个 agent 工具：

| Tool | operation | 用途 |
|---|---|---|
| `memory_read` | `task_context` / `task_brief` / `get_task_context` / `get` / `search` / `search_records` / `runtime_digest` / `retrieve_context` / `important_memories` / `latest_memories` | 读取当前任务上下文与简报、文件读取、搜索、读 digest、召回上下文、重要/最新记忆输出 |
| `memory_write` | `record` / `observation` / `checkpoint` | 写入结构化 raw 记忆与任务节点；可选 `distill=true` 触发蒸馏 |

CLI 负责所有高级/同步/维护能力：

| 能力 | CLI |
|---|---|
| 配置/健康/guard | `config-diagnose` / `health` / `guard` |
| 文件维护/索引/迁移 | `write-file` / `backup` / `compact` / `rebuild-index` / `migrate` |
| 编译/快照/关键文档 | `compile` / `runtime-digest` / `snapshot-rebuild` / `weekly-snapshot-rebuild` / `monthly-snapshot-rebuild` / `rebuild-key-docs` |
| 谱系/冲突/artifact | `trace-lineage` / `list-conflicts` / `compare-snapshots` / `link-artifact` |
| LLM enhance | `enhance` |

不存在可配置的 admin MCP 工具表面。新能力若不是普通 agent 每轮必需，默认进入 CLI 或内部 API。

### 11.2 LLM 接入（OpenAI 兼容协议）

实现位置：`memory_llm.py` + `memory_llm_pipeline.py` + `memory_llm_enhance.py` + `memory_llm_policy.py`。

- 默认 profile：DeepSeek（`https://api.deepseek.com`，`deepseek-chat`），任何兼容服务（OpenAI / Moonshot / vLLM / Ollama 网关）切换 `base_url`+`model` 即可
- 配置优先级：显式 `LLMConfig` > 环境变量（`MEMORY_LLM_API_KEY` / `BASE_URL` / `MODEL` / `TIMEOUT`，回落 `DEEPSEEK_API_KEY` / `OPENAI_API_KEY`）> `MCP/Memory/llm_config.local.json`（**不入 Git**）
- 仅依赖 stdlib（`urllib`），可注入 transport 便于 mock
- LLM 失败抛 `LLMRequestError`，调用方退化为无 LLM 路径
- pipeline 提供 `compute_distill_cache_key` / `chunk_raw_records` / `map_reduce_distill` / `summarize_records_for_recall`，处理"同输入不重复花钱"+"过长输入分块汇总"
- 主路径接入：`memory_write(distill=true)` raw 落盘后蒸馏；`memory_read(operation="retrieve_context", summarize=true)` 召回结果概括；`task_brief` 在 capability 启用时生成证据受限执行摘要。所有线路均有确定性降级，且简报 LLM 故障不改变基础任务上下文结果。

⚠️ 安全：`llm_config.local.json` 与 `*.api_key` / `*.apikey` 已在 `MCP/Memory/.gitignore`，禁止把 API key 提交版本库。

## 12. LLM / 非-LLM 职责划分

判断标准：(1) 非 LLM 已经做得足够好的事不引入 LLM；(2) 离开 LLM 做不好的事由 LLM 做并落 distilled 层。

| 能力 | Owner | 备注 |
|---|---|---|
| raw 写入 / 哈希 / 冻结 | **非 LLM** | `memory_writer` + `make_raw_record` |
| Front Matter 解析 / Schema 校验 | **非 LLM** | 规则即可 |
| 事件日志 / Lock / Backup / Compactor | **非 LLM** | deterministic |
| 关键词 / FTS 检索 | **非 LLM** | SQLite FTS 已是 BM25 基线 |
| 评分 / 排序 / 衰减 | **非 LLM** | `memory_scoring` |
| Token 估算 | **非 LLM** | `token_estimator`（CJK-aware） |
| 编译模板 digest（无 LLM 兜底） | **非 LLM** | "稳但朴素"路径，永远可用 |
| 自然语言摘要 / 重写 / 蒸馏 | **LLM** | 必须 `derived_from` 指回 raw |
| 意图与权威信息地图任务简报 | **LLM 意图提议 → 非 LLM 地图/校验/降级** | 确定性层校验规则、Skill、源码、验证、来源、预算与 fallback；LLM 不声明当前事实 |
| 跨 raw 主题聚类 / 命名 | **LLM** | 落 distilled，可重做 |
| 冲突识别（语义层） | **LLM 提议 → 非 LLM 校验** | LLM 输出候选；governance 流程裁决 |
| 自然语言查询解析 | **LLM 提议 → 非 LLM 检索** | LLM 翻译为 facets，FTS 执行 |
| 团队沉淀提升 | **LLM 提议 → 非 LLM 校验/写入** | LLM 可判断个人记录是否适合团队上下文并生成摘要；确定性层执行硬跳过、schema、`derived_from` 与写入 |
| 谱系 / `derived_from` 维护 | **非 LLM** | 审计基础 |
| 治理（candidate→...→published） | **非 LLM** | 仅历史兼容 |
| 成本/预算控制 | **非 LLM** | LLM 不能自己决定要不要再花一次钱 |

不变量：

- 任意环节失败/关闭 LLM，系统不退化为 0：raw 仍写入、FTS 仍检索、模板 compile 仍出 digest
- LLM 输出从不直接覆盖 raw 或非 LLM 索引，只产出 distilled
- distilled 可清空可重建，不损坏 raw 真源

代码层单一事实源：`memory_llm_policy.LLM_CAPABILITY_MATRIX` + `should_use_llm(capability)`。新加 LLM 必须先在矩阵登记并加单测。

## 13. Git 策略

- **消费项目可进 Git**：`memory-bank/shared/` / `memory-bank/people/{user}/` / `memory-bank/candidates/` / 经审查的 `.ai-memory/config.json`
- **消费项目不进 Git**：`.ai-context/` / `.ai-memory/` 中除配置外的运行状态（含 search.db / events.jsonl / backups / temp / compile-cache / locks / *.api_key / llm_config.local.json）
- **Memory MCP 源码仓库不进 Git**：整个 `.ai-memory/`、`.ai-context/`、本机配置以及任何消费项目的名称、路径、资产、记忆和测试数据；由 `scripts/check_public_tree.py` 与 CI 阻断回归。

## 14. 权限与冲突策略

- 个人增量提交为主，不鼓励多人共写同一系统正文文件
- 系统记忆走候选汇总与发布；shared 层尽量由发布流程写入
- 共享内容采用追加、分段、生成式汇总；编译产物可重建，不作主编辑层

## 15. 后续计划与收口状态

> 已完成里程碑的实现细节迁移至 [README §5](./README.md#5-开发历史与计划)；版本流水见 [DEVLOG.md](./DEVLOG.md)。本节保留 v0.11.x 收口状态与仍需观察/未触发的后续方向。

### 15.1 【最高优先级】RAG 召回质量解锁（v0.11.x）

> **收口状态（v0.11.1）**：A / B / C / D 四项均 ✅。B 在 v0.11.1 补齐 verified presets；真模型召回基线转为后续观察项。详见 DEVLOG。

**问题诊断**：当前 RAG 通路骨架完整、安全约束扎实（§15.3 完成项 v0.7.5–v0.9.0），但召回**质量**未达生产线。三处具体阻塞：

1. **`LocalOnnxProvider` 的分词器是 byte-level hash 桩件**（[`memory_embeddings.py`](./servers/memory_server/memory_embeddings.py)）：bge-small / MiniLM 这类模型必须配套 WordPiece / BPE / SentencePiece，桩件分词在 ONNX 模型上推出来的向量与"真正训练出来的语义空间"严重失配，导致 cos 分布偏窄、跨条目区分度低；**这是当前设计-实现 gap 最大的一处**。
2. **`scripts/download_embedding_model.py` 的 `PRESETS` sha256 曾为 `<fill-me-in>`**：v0.11.1 已补齐两组 verified preset，下载模型、tokenizer 与必要 sidecar 均强制 sha256 校验。
3. **召回质量没有可回归的评测**：当前只有功能测试（"接入点不抛"），没有 recall@k / MRR / 跨 provider 对比脚本；改 chunk 大小 / 重排权重 / provider 时无法量化好坏。

**验收门**（缺一项不算解锁）：

| 项 | 验收标准 | 状态 |
|---|---|---|
| A. 真实分词器 | `LocalOnnxProvider` 引入 `tokenizers` 或 `sentencepiece`（按模型族二选一），与 `model_path` 同目录读取 `tokenizer.json` / `*.model`；缺失则 provider 走 `unavailable` 而非桩件输出 | ✅ v0.11.0 |
| B. `PRESETS` 哈希 | `bge-small-zh-v1.5` 与 `paraphrase-multilingual-MiniLM-L12-v2` 至少一组 `model_url + tokenizer_url + sha256` 全部填实；脚本 `--list` 必须显示已校验 | ✅ v0.11.1 |
| C. 召回评测脚本 | 新增 `scripts/eval_recall.py`：读 `tests/data/recall_set.jsonl`（人工标注的 query→record_id 列表）→ 跑 `vector_search` / `_rank_records` → 输出 `recall@5/10` + `MRR` + `provider_id` + `model_hash`；CI smoke 跑 deterministic-hash provider 设下限 | ✅ v0.11.0 |
| D. 失败可观测 | `_vector_supplement` 异常路径必须写 `events.jsonl`（type=`vector_supplement_skipped`）而非纯 try/except 吞掉；health 暴露 `vector_skip_count_24h` | ✅ v0.11.0 |

**非目标**：不引入远程 API；不引入 GPU 依赖；不改 §15.3 已完成的索引格式与重建协议。

### 15.2 【最高优先级】LLM 调用统一（v0.11.x）

> **收口状态（v0.11.1）**：A / B / C / D 四项均 ✅。C 在 v0.11.1 补齐 `scripts/llm_smoke.py`。详见 DEVLOG。

**问题诊断**：v0.10.0 已落地 `memory_llm_runner.run_llm_capability` + 七状态包络，但仓库内仍有**双轨调用**：

1. `_run_distill_for_write` / `_run_recall_summarize` / `key_documents` 的 LLM renderer 仍在用旧的 try/except + 临时降级，错误状态无法对齐七状态包络。
2. 缺少 `tests/memory_server/test_dispatch.py` 对 `rewrite_query` / `narrative` 这两条 v0.10.0 入口的 facade 端到端测试（当前只有单元测试）。
3. 没有"真 LLM smoke" 脚本（gated）：所有 LLM 测试都跑 `FakeProvider`，配置改变 / SDK 升级时无法自检。

**验收门**：

| 项 | 验收标准 | 状态 |
|---|---|---|
| A. 三处入口收敛 | `_run_distill_for_write`、`_run_recall_summarize`、`memory_key_documents.render_llm_document` 全部改走 `run_llm_capability(capability=…)`；所有失败必须返回七状态之一 | ✅ v0.11.0 |
| B. facade dispatch 测 | `test_dispatch.py` 增 `rewrite_query` / `narrative` 两个 facade 端到端用例，覆盖 `enabled=False` / `unavailable` / `timeout` / `ok` 四档 | ✅ v0.11.0 |
| C. 真 LLM smoke | `scripts/llm_smoke.py`（gated by `MEMORY_LLM_SMOKE=1` + 真 key），跑 `distill_for_write` + `query_rewrite` + `snapshot_narrative` 三个最小 case，输出每个 capability 的 status / latency / token_used；CI 不跑，开发者手动跑 | ✅ v0.11.1 |
| D. 配置可视化 | CLI `config-diagnose` 输出 `llm_capabilities` 段，列每个 capability 的 `enabled / provider / timeout_ms / fallback`，按文件 / 环境 / 默认归类来源 | ✅ v0.11.0（5 能力 × 4 字段，每字段 `{value, source}`） |

**非目标**：不替换 deterministic 路径；不引入 LLM 强依赖；`enabled` 默认仍为 `False`。

### 15.3 已完成里程碑速查表

> 详细实现（P0/P1/P2 清单、RAG 各 phase、provider 抽象、索引协议、LLM runner 七状态等）已迁移至 [README §5](./README.md#5-开发历史与计划)。

| 版本 | 主题 | 完成 |
|---|---|---|
| v0.6.0 | OOTB 稳健性（user 强校验 / shared overwrite 拒绝 / auto-maintenance / bootstrap.ps1 / UE facet 自动盘点 / shared compactor / scale-baseline / health 自愈） | OK |
| v0.7.0 | P4-C 关键文档可重建（KEY_DOCUMENTS manifest + facade rebuild_key_documents + CLI + 三档 renderer + backup 强保留） | OK |
| v0.7.5 / v0.8.0 / v0.9.0 | RAG Phase 1 / 2a / 2b / 2c（provider 抽象 + DeterministicHash + 分块编排 + retrieval `_vector_supplement` + key_documents embedding renderer + LocalOnnxProvider CPU EP only + sha256 强校验脚本） | OK |
| v0.10.0 | P4 LLM 软增强收尾（`memory_query_rewrite` + `memory_snapshot_narrative` + `memory_llm_runner` 七状态包络 + `llm_defaults` capability 粒度） | OK |
| v0.10.1 | 团队接入扫尾（`MEMORY_MCP_USER` 最高优先级 user 来源 + README §3.3 失败模式表 + UE Facet §17 文档收口 + 并发/多进程子集 64 用例全绿） | OK |
| v0.11.0 | RAG 召回质量解锁 + LLM 调用统一首批（§15.1-A/C/D + §15.2-A/B/D；621 passed + 3 skipped） | OK |
| v0.11.1 | v0.11.x 保留项收口（§15.1-B verified `PRESETS` + §15.2-C `llm_smoke.py`；624 passed + 3 skipped） | OK |

### 15.4 已降级方向（attic，保留兼容入口，不再扩张）

- **多人联合项目治理**（candidate → validated → published → archive）：现有 validate / publish / archive 链路保留用于历史数据迁移；治理动作迁至 CLI / scripts / 管理 skill；不再把「插件内自动审查与规则晋升」当作主产品方向。
  - 现状：
    - `memory_governance.py` / `memory_maintenance.memory_delete_record` 实现保留。
    - MCP 面：不注册；不存在可开启治理 MCP 工具的配置。
    - CLI 面：`cli.py validate` / `publish` / `archive` / `delete` 保留，供历史数据迁移使用。
    - `memory_governance.py` 文件头带 `DEPRECATED — ATTIC-ONLY` 横幅。
  - **attic 期允许的改动**：保持厄子写入契约的 bug fix；测试维护；CLI 可读性修复。
  - **attic 期不允许的改动**：新增 pipeline 阶段；新 caller hook；新 candidate 子状态；把任何默认路径重新接回这条链路。
  - **解冻条件**：出现多人多仓库治理需求，且 raw + distilled 路径证明不足以表达跨团队共识。

### 15.5 已冻结方向（保留实现，不再扩张）

- **整个 vector / RAG 通路**（`memory_embeddings.py` / `memory_vector_search.py` / `memory_vector_index.py` / `memory_vector_corpus.py` + `_vector_supplement` + key_documents `embedding` renderer）：自 v0.11.x slim-down 起冻结。
  - 现状：默认 `embeddings.enabled=False`，主路径 0 字节、0 调用开销；五个模块文件头与两个消费入口（`memory_retrieval._vector_supplement` / `memory_key_documents._render_embedding_renderer`）均带 `EXPERIMENTAL — FROZEN` 横幅。
  - 不启动条件（任一命中即可解冻）：
    - `chunks ≥ 100k`（当前约 1.5–3 万）
    - 全量重建 `≥ 10 min`（当前 sub-minute）
    - 稳态 `QPS ≥ 20`（当前 < 1）
    - 完成"真模型召回基线"观察项（设计文档 §16）并证明 deterministic + FTS 召回不足
  - **冻结期允许的改动**：保持现有契约的 bug fix；测试维护；`embeddings.enabled=True` opt-in 路径上的兼容性修复。
  - **冻结期不允许的改动**：新 provider；新 chunk 策略；新 renderer；新 tuning knob；HNSW / 量化 / GPU EP（属于已计划但未触发的 RAG Phase 3）。
- **RAG Phase 3**（GPU EP / 量化 / HNSW）：上述阈值任一命中后再考虑。

> 说明：vector tier 既"已落地"又"未达阈值"，因此从原 §15.5「不启动」升级为「已冻结」；治理链路（原 §15.4）维持「已降级，保留兼容」。两者的差别在于：治理链路有历史数据迁移路径需要保留入口；vector tier 是默认 0 消费、可随时无感重新启用。

## 16. 最终结论

- **形式**：MCP（接口）+ JSON（传输）+ Markdown+Front Matter（存储）+ SQLite FTS（索引）+ 可选向量索引（`.ai-memory/vector_index/`）。
- **写入**：raw 冻结由代码生成，distilled 可由任意 LLM 重写，谱系由 `derived_from` 绑死。
- **编译**：确定性为主，LLM/RAG 仅软增强；产物可重建可丢弃。
- **兜底**：无 LLM、无 onnxruntime、无模型时基础写入/检索/快照/评分/编译/关键文档 deterministic 渲染均完整可用。
- **下一阶段观察项**：下载真实 local-onnx 模型后跑 `scripts/eval_recall.py` 建立召回基线；RAG Phase 3 继续按 §15.5 阈值触发。详细历史见 [DEVLOG.md](./DEVLOG.md) 与 [README.md](./README.md)。
## 17. UE Facet 数据模型（已实现，v0.10.1 文档收口）

UE 专属记忆建模分两层：**项目级自动盘点** + **记录级 facet 字段**。两层都是确定性的、可关闭的；非 UE 仓库自动跳过，无任何强制依赖。

### 17.1 项目级自动盘点（`memory_ue_facets.py`）

启动期 / `health` 触发，扫 `repo_root` 写 `.ai-memory/ue_facets.json`：

| 来源 | 抽取字段 |
|---|---|
| `*.uproject` | `project`、`engine_association`、`modules[]` |
| `Source/**/*.Build.cs` | `dependencies[]`（解析 `(Public\|Private)?DependencyModuleNames.Add(Range)?` 数组字面量） |
| `Plugins/**/*.uplugin` | `plugins[]`（`FriendlyName` → `Name` → 文件名 fallback） |

`is_ue_project=true` 当根目录有任意 `*.uproject`；否则全字段为空、不写文件。`known_components(facets)` 暴露已知名集合给 record-write warning 链路（`memory_record_ue_warnings`：`components` 字段含未知名时回 `ue_unknown_components` warning，不阻塞）。

### 17.2 记录级 facet 字段（`memory_records` / `memory_lineage` / `memory_artifact_paths`）

`memory_write(operation="record")`、`memory_read(operation="retrieve_context|important_memories")` 与 CLI 多个命令接受同一组 facet：

| 字段 | 命名空间 | 归一化 | 索引/检索 |
|---|---|---|---|
| `asset_paths` | UE `/Game/...` 或物理 `Content/X.uasset` | `normalize_asset_paths` 双向解析 + 附 `git_sha` | FTS + `link_artifact` |
| `blueprint_paths` | 同 `asset_paths`（约定 `/Game/...`） | 同上 | FTS + lineage |
| `map_names` | UE Map / Level / World Partition 名 | string 列表去重 | FTS facet |
| `module_names` | C++ 模块名（与 `Build.cs` 抽取的 `dependencies` 同源） | string 列表去重 | FTS facet + warning |
| `plugin_names` | 插件名（与 `*.uplugin` 同源） | string 列表去重 | FTS facet + warning |
| `class_names` | UCLASS / 普通 C++ class | string 列表去重 | FTS facet |

`memory_read.retrieve_context / important_memories` 在 MCP schema 上原生暴露上述六个字段；CLI `compile / retrieve-context / important-memories / link-artifact` 也复用这些 facet。公开接口默认 `facet_mode=hard`，保持严格过滤兼容；内部推断可用 `facet_mode=boost`，只加分、不误删精确命中。v2 路径为 `metadata/FTS recall → relevance band → importance tie-break → canonical/lineage collapse → budget packing`，显式 `ranking_version=v1` 可回滚，v2 异常也会自动回退。

### 17.3 显式不做的扩张

按 §1 "无 UE 编辑器依赖" 边界，以下能力**不主动引入**，仅在 record 已显式声明 facet 时被动消费：

- Map / Level / World Partition 引用关系自动抽取（需要 UE Editor 解析 `.umap`，违反 "无 UE 编辑器依赖"）
- Blueprint 节点级依赖图（同上）
- Asset Registry 扫表自动入库（同上 + 体量与索引压力）
- 模块 → 资产系统区映射的人工本体（推荐由 record 层在 `module_names` + `asset_paths` 同记录共现表达，由检索打分自然聚合）

如未来需要任一项，应作为独立 UE Editor MCP plugin 提供 facet 喂入端，本插件继续只持有 facet 字段与索引，保持 runtime 纯本地、无引擎依赖。

## 18. P1 可靠异步反思基座

### 18.1 故障域与恢复契约

MCP 的 stdio 线程不是后台任务的可靠宿主，任何时刻都可能退出。因此请求路径只负责把意图原子地放入 durable queue，不等待 LLM，也不把后台失败提升为主操作失败。

```text
checkpoint / write
  -> atomic enqueue
  -> MCP immediately returns

daemon worker (startup grace)
  -> reclaim expired lease
  -> claim + heartbeat
  -> execute isolated step
  -> commit result | exponential retry | dead letter

unexpected process exit
  -> running job and prepared transaction stay on disk
  -> next MCP startup reclaims/replays them
```

队列 live 文件损坏时先读 `.bak`；live 和 backup 都损坏时返回 `queue_corrupt`，但 `memory_read` / `memory_write` 基本功能继续工作。运行中的 job 不从 durable queue 删除，只有带匹配 lease token 的完成操作才能提交结果。配置变化不会重写旧证据；job 记录 enqueue/claim 时的 config hash，实际执行使用当前 last-known-good 配置。

### 18.2 项目全局反思，而不是 Skill 生成

输入是 task-scoped structured records，不是完整聊天记录。secret-bearing、snapshot、archive 和 `background_reflection` 自身产物在送入 LLM 前排除。两阶段提示词实现在 `memory_reflection.py`：

1. Extractor 只提出 `decision / procedure / system_rule / incident / validation_result`，每项必须引用精确 record ID，并从 `CREATE / UPDATE / MERGE / SUPERSEDE / REJECT` 选择动作。
2. Adversarial critic 对照证据缩窄或删除无依据、短期、重复、越权、含秘密的提案。
3. 确定性 validator 不信任两次 LLM 输出，再验证 JSON schema、record kind、citation subset、validation 类型、置信度、长度、秘密规则和目标记录安全性。更新类动作只能指向活动的、可替换的、非权威 background reflection。
4. 强门禁才自动发布：`confidence >= publish_min_confidence`，且存在真实 `validation_result`，或相同 fingerprint 获得至少两个 distinct task 支持。
5. 发布记录始终为 `scope=project_shared`、`status=distilled`、`provenance=background_reflection`、`authoritative=false`、`replaceable=true`，并用 `derived_from_record_ids` 绑定 raw 证据。UPDATE/MERGE/SUPERSEDE 只追加新记录和 `supersedes` 边，不原地改写或删除；REJECT 不落盘。

该结构借鉴 Hermes 的 closed learning loop，但不让模型直接改项目规则/Skills。Hermes 的 write approval 对本系统的等价物是“候选只留在 durable job result + deterministic publish gate + Curator”；Hermes Self-Evolution 的完整 tests/constraints/PR gate 对应本系统的 validation evidence、跨任务重复支持和全量回归门禁。

### 18.3 外部库结论

| 项目 | 值得借鉴 | P1 是否直接接入 |
|---|---|---|
| [Hermes Agent](https://github.com/NousResearch/hermes-agent) | session 后复盘、memory/skill 分层、写入审批、周期 Curator | 否；只借鉴产品闭环与门禁 |
| [Hermes Self-Evolution](https://github.com/NousResearch/hermes-agent-self-evolution) | execution trace 驱动反思、候选评测、全测/尺寸/语义约束 | 否；本目标不生成 Skill/Prompt/Code |
| [LangMem](https://github.com/langchain-ai/langmem) | background extraction/consolidation API | 否；现有 runner + Markdown store 已覆盖，避免绑定 LangGraph |
| [Graphiti](https://github.com/getzep/graphiti) | episode provenance、事实 validity window、增量冲突失效 | 否；当前只保留 Task Graph，若未来出现独立的时态知识关系需求再单独评估 |
| [Letta](https://github.com/letta-ai/letta) | memory blocks 与主 agent/睡眠 agent 分离 | 否；会扩大为另一套 agent runtime |
| [Mem0](https://github.com/mem0ai/mem0) | 可插拔 memory layer 与历史 API | 否；当前项目级 typed records/lineage 更贴合现有数据 |

P2 的合理触发条件：跨任务同义提案因纯 fingerprint 合并明显漏召回；需要回答“某事实在某时刻是否有效”；记录规模让 Markdown + FTS 的重建/查询超过既定阈值。触发前继续保持无新依赖、raw 可读、derived 可重建。


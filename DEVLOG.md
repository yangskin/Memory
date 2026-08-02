# DEVLOG - MCP Memory

## 2026-05-10 (two-tool MCP surface)

> **状态：普通 agent 默认 MCP 表面收敛为两个工具。** `memory_read` 负责任务上下文、读取、搜索、召回；`memory_write` 只负责结构化 raw record / observation / checkpoint。`memory_context` / `memory_enhance` / legacy/admin MCP 工具不再出现在 `list_tools`；高级/同步/维护能力走 CLI。随后已移除 admin MCP 扩展开关配置项；全量测试：690 passed。

### 范围

- `server_tools._build_tools` 固定返回 `memory_read` / `memory_write`。
- `memory_read` 新增 `task_context` / `get_task_context` / `retrieve_context` / `important_memories` 等读侧 operation，覆盖原任务握手和召回用法。
- `memory_write` schema 收窄为 `record` / `observation` / `checkpoint`；`operation=file` 和 `link_artifact` 返回 `admin_cli_required`。
- `_dispatch_tool` 对非 `memory_read` / `memory_write` 的 MCP 工具名返回 `unknown_tool`，避免隐藏工具被直接 call_tool 调用。
- 配置层删除 admin MCP 扩展开关字段；默认配置、dataclass、加载逻辑、诊断输出、测试与文档均不再暴露该选项。
- CLI 新增/补齐 `config-diagnose`、`write-file`、`retrieve-context`、`important-memories`、`trace-lineage`、`list-conflicts`、`compare-snapshots`、`link-artifact`、`enhance`。
- README、设计文档、Copilot rules 和相关测试同步到双工具模型。

---

## 2026-05-10 (always-on multi-user cleanup)

> **状态：移除普通运行时的单人模式开关。** `multi_user.enabled` 不再出现在默认配置或运行时分支；旧配置残留该字段时只在 `config_diagnose` 中报告为 ignored，不能关闭 user-scoped / append-only 多人安全策略。

### 范围

- `memory_config.MultiUserConfig` 删除 `enabled` 字段；`multi_user` 只承载 `user_scoped_paths` 与 `shared_paths_policy`。
- `memory_reader` / `memory_writer` / `memory_guard` / `memory_paths` 删除 `config.multi_user.enabled` 条件，路径分流与共享写策略始终按多人安全策略执行。
- `config_diagnose` 输出 `multi_user.mode=always_on`；旧 `multi_user.enabled` 字段输出为 `multi_user.enabled_ignored`。
- README 与设计文档同步：单人使用只是多人模式下单个有效 user 的退化情况，不再作为产品模式或配置开关；README 新增 Agent 规则配置片段，说明如何把 Memory MCP 硬规则写进 `AGENTS.md` / `.github/copilot-instructions.md`。

---

## v0.11.1 — v0.11.x 收口：预置模型校验 + 真 LLM smoke

> **状态：624 passed + 3 skipped。** 收口设计文档 §15.1-B / §15.2-C 两个 v0.11.x 保留项；普通 CI 不触发真实 LLM 或模型下载。

### 交付清单

| 来源 | 交付 | 验证 |
|---|---|---|
| §15.1-B | `scripts/download_embedding_model.py` 的 `PRESETS` 去除 `<fill-me-in>`：内置 `bge-small-zh-v1.5` 与 `paraphrase-multilingual-MiniLM-L12-v2` 两组 verified preset，均固定 `model + tokenizer + sha256`；bge 的 ONNX external data sidecar 同步固定 sha256 并下载到同目录；新增 `--list` 输出 verified 清单；显式下载路径支持 `--tokenizer-url / --tokenizer-sha256`。 | `test_local_onnx_provider.py`：`test_cli_lists_verified_presets` + `test_cli_downloads_explicit_model_and_tokenizer`；手动 `python scripts/download_embedding_model.py --list` 确认两组 preset 均显示 verified。 |
| §15.2-C | 新增 `scripts/llm_smoke.py`：默认 gated skip；仅当 `MEMORY_LLM_SMOKE=1` 且存在真实 LLM key 时，通过 `run_llm_capability(force_enabled=True)` 依次跑 `distill_summary`、`query_rewrite`、`snapshot_narrative` 三个最小 case；逐行输出 JSON：`capability / status / latency_ms / token_used / fallback_used`。 | `test_llm_smoke.py`：确认无 env 时返回 skip、不发网络请求；手动无 env 运行确认 skip。 |

### 文件级改动

- `scripts/download_embedding_model.py`：preset schema 从单文件扩展为 `model/tokenizer/extra_files`；新增 sha256 校验 helper、alias 解析、`--list`、多 artefact 事件记录与安装目录布局。
- `scripts/llm_smoke.py`：新增手动 smoke 脚本，复用现有 LLM client / runner / pipeline，不引入新依赖。
- `tests/memory_server/test_local_onnx_provider.py` + `test_llm_smoke.py`：新增 3 个用例，删除旧 placeholder preset 断言。
- `README.md` + `MemorySystemDesignDocument.md`：同步 v0.11.1 状态。

### 后续观察

- 真模型下载后可手动跑 `scripts/eval_recall.py` 建立 `local-onnx` 召回基线；不作为 v0.11.x 阻塞项。

---

## v0.11.0 — RAG 召回质量解锁 + LLM 调用统一（首批）

> **状态：621 passed + 3 skipped（v0.10.2 是 610 passed + 1 skipped；新增 11 用例 + 2 个 onnxruntime gated skip）。** 设计文档 §15.1 / §15.2 的最高优先级条目首批落地：6 项交付。

### 交付清单

| 来源 | 交付 | 验证 |
|---|---|---|
| §15.1-A | `LocalOnnxProvider` 引入真实 tokenizer：构造函数新增 `tokenizer="auto"` 参数，按模型同目录探测 `tokenizer.json`（HuggingFace `tokenizers`）→ `spiece.model` / `tokenizer.model`（`sentencepiece`）；缺失即抛 `ProviderUnavailableError`，`get_provider("auto", ...)` 透明降级到 `deterministic-hash`；`model_hash` 把 tokenizer 文件 sha256 折进去（换 tokenizer 自动失效缓存）。原字节级哈希桩件已删除。 | `test_local_onnx_provider.py`：新增 `test_local_onnx_provider_missing_tokenizer_raises_unavailable` + `test_get_provider_auto_falls_back_when_tokenizer_missing` + 现有 `test_local_onnx_provider_loads_real_model` 改用 `tokenizer=callable` 注入。 |
| §15.1-D | `_vector_supplement` 异常 / `result.ok=False` 双路径写 `events.jsonl::vector_supplement_skipped`（带 `reason` + `query_preview`）；`memory_health_check` 新增 `vector_skip_count_24h` 字段，由新工具函数 `count_recent_events(config, event_type, window_seconds=86400)` 实现。 | `test_vector_integration.py`：新增 `test_vector_supplement_writes_event_when_search_raises` + `test_health_check_surfaces_vector_skip_count_24h`。 |
| §15.1-C | `scripts/eval_recall.py`：`recall@5 / recall@10 / MRR + provider_id + model_hash` 输出，`tests/data/recall_set.jsonl` 提供 5 条种子用例。 | `test_eval_recall.py`：smoke + CLI 入口两条；deterministic 提供方下限 `recall@10 ≥ 0.4`。 |
| §15.2-A | 三处 LLM 入口统一改走 `run_llm_capability`：`server_dispatch._run_distill_for_write`（`distill_summary`）+ `server_dispatch._run_recall_summarize`（`summarize_recall`，新增 `config` 形参）+ `memory_key_documents._rebuild_one`（`rebuild_key_document`）。`run_llm_capability` 新增 `force_enabled: bool = False` 关键字以保留三处「显式 opt-in」语义；旧路径返回的状态字符串映射到原失败码（`disabled→llm_disabled` / `unavailable→llm_unavailable` / `timeout→distill_timeout` / …）。`_build_llm_client()` / `_maybe_build_llm_client()` 仍保留供 monkeypatch 测试与 snapshot 路径复用。 | 79/79 影响测试全绿（`test_dispatch` + `test_key_documents` + `test_cli_rebuild_key_docs` + `test_query_rewrite` + `test_llm_runner` + `test_llm_pipeline_dispatch` + `test_llm_enhance_dispatch`）。 |
| §15.2-B | `test_dispatch.py` 新增 4 个 `rewrite_query` facade 端到端用例（`disabled` / `unavailable` / `timeout` / `ok`）+ 1 个 `narrative` disabled 用例；通过 `monkeypatch.setattr(_runner, "_default_client_factory", ...)` + `monkeypatch.setattr(memory_query_rewrite, "rewrite_query", ...)` 控制四档分支。修复 `_run_query_rewrite._invoke` 缺失的 `profile` 第二实参（runner 契约）。 | `test_dispatch.py`：18 用例全绿。 |
| §15.2-D | `config_diagnose` 输出新增 `llm_capabilities` 段：5 个能力（`distill_summary` / `summarize_recall` / `rebuild_key_document` / `query_rewrite` / `snapshot_narrative`），每项 4 字段（`enabled` / `timeout_ms` / `max_tokens` / `fallback`）+ `description`，每字段带 `{value, source}`，`source ∈ {default, file}`，识别 `llm_defaults.timeout_ms` ↔ `llm_defaults.timeout` 别名。 | `test_config_diagnose.py`：新增 2 个用例覆盖 default 与 file overrides 两种来源。 |

### 文件级改动

- `servers/memory_server/memory_embeddings.py`：`LocalOnnxProvider.__init__` 重写；新增 `_resolve_tokenizer` static / `tokenizer_path` / `tokenizer_kind` properties；`_tokenise_batch` 删除字节级哈希桩件，改走 `self._tokenize_fn` + 截断 + 对齐 padding；`get_provider` 透传 `tokenizer` kw。
- `servers/memory_server/memory_retrieval.py`：导入 `append_event`；`_vector_supplement` 异常 / 非 ok 两路径写 `vector_supplement_skipped` 事件。
- `servers/memory_server/memory_events.py`：新增 `count_recent_events(config, event_type, *, window_seconds)`，遍历 `events.jsonl` + 轮转副本。
- `servers/memory_server/memory_maintenance.py`：`memory_health_check` 写 `extras["vector_skip_count_24h"]`。
- `servers/memory_server/memory_llm_runner.py`：`run_llm_capability` 增 `force_enabled` kw。
- `servers/memory_server/server_dispatch.py`：`_run_distill_for_write` / `_run_recall_summarize` 改走 runner；`_run_query_rewrite._invoke` 接受 `profile`。
- `servers/memory_server/memory_key_documents.py`：`_rebuild_one` 改走 runner，删除 `llm_client` 形参；`rebuild_key_documents` 用 `_maybe_build_llm_client()` 探针保留显式 `renderer="llm"` fast-fail。
- `servers/memory_server/memory_diagnose.py`：新增 `_diagnose_llm_capabilities` + 顶层 `llm_capabilities` 字段。
- `scripts/eval_recall.py` + `tests/data/recall_set.jsonl`：新增。
- 测试：`test_local_onnx_provider.py` + `test_vector_integration.py` + `test_config_diagnose.py` + `test_dispatch.py` 共增 11 个用例 + `test_eval_recall.py` 新文件。

### v0.11.x 待续状态

- §15.1-B / §15.2-C 已在 v0.11.1 收口。
- 实测 `local-onnx` 召回基线可作为后续观察项：下载真实模型后跑 `scripts/eval_recall.py`。

---

## v0.10.2 — 设计文档与 README 重组（仅文档）

> **状态：无代码改动；610 passed + 1 skipped 与 v0.10.1 一致。** 设计文档收敛到「未完成且影响下一步走向」的工作；已完成里程碑细节迁移至 README §5。

### 改动

- **`MemorySystemDesignDocument.md`**：原 432 行 → 330 行。
  - §15 整段重写：删除 §15.1（v0.6.0 P0/P1/P2 详细清单）、§15.2（P4-C 详表）、§15.3（v0.10.0 三处入口列表）、§15.4.1–§15.4.6（RAG 硬约束 / GPU 阈值 / Provider 抽象 / 索引格式 / 接入点 / Phase 推进表）、§15.5.1（v0.10.1 表格）、§15.6（后续版本占位 8 行表）。
  - §15 新结构：§15.1 **【最高优先级】RAG 召回质量解锁（v0.11.x）** + §15.2 **【最高优先级】LLM 调用统一（v0.11.x）** + §15.3 已完成里程碑速查表（5 行表）+ §15.4 已降级方向 + §15.5 不启动。
  - §16 精简到 5 条结论 + 下一阶段优先级显式指向 §15.1 / §15.2。
  - §17 UE Facet 数据模型保留（设计内容非状态）。
- **`README.md`**：§5 在原「已落地（最近）」+「计划中」之间插入「已完成里程碑详情（设计文档原 §15.1–§15.4 细节归档）」段，搬入 v0.6.0 / v0.7.0 / v0.7.5–v0.9.0 / v0.10.0 / v0.10.1 的全部实现要点（包括 RAG 硬约束、Provider 抽象、索引格式、配置示例、LLM 七状态包络等）。
- **未改代码、未改测试**：测试输出与 v0.10.1 完全一致。

### v0.11.x 待办（已写入设计文档 §15.1 / §15.2）

| 主题 | 关键验收 |
|---|---|
| RAG 召回质量解锁 | 真实 tokenizer（`tokenizers` / `sentencepiece`）+ `PRESETS` sha256 填实 + `scripts/eval_recall.py`（recall@k / MRR）+ `events.jsonl` 写 `vector_supplement_skipped` |
| LLM 调用统一 | 三处入口（`_run_distill_for_write` / `_run_recall_summarize` / `key_documents.render_llm_document`）改走 `run_llm_capability` + `test_dispatch.py` 增 `rewrite_query` / `narrative` 端到端 + `scripts/llm_smoke.py` gated + `config_diagnose` 新增 `llm_capabilities` 段 |

---

## v0.10.1 — 团队接入扫尾 + 文档同步

> **状态：`MEMORY_MCP_USER` 环境变量成为最高优先级 user 来源；conftest autouse 隔离；README/设计文档同步至当前实现；测试 607 → 610 passed (+3)，1 skipped 不变。** 不引入新功能，只关闭 P0-1 在 CI / 子进程 / 测试场景下的最后一个易踩坑。

### 改动

- **`memory_events.get_current_user`**：新增第 1 优先级 `MEMORY_MCP_USER` 环境变量分支；空白字符串视为未设置、继续向下回落。原 `.vscode/settings.json` → `USERNAME`/`USER` → `unknown` 顺序保留为 2/3/4 档。
- **`tests/memory_server/conftest.py`**：新增 autouse fixture `_clear_memory_mcp_user_env`，在每个测试前 `monkeypatch.delenv("MEMORY_MCP_USER", raising=False)`，避免开发者 shell 中已 export 的值污染依赖 `USERNAME` 的旧测试。
- **`tests/memory_server/test_user_validation.py`**：新增 3 个测试覆盖 (1) env 优先级压过 vscode + USERNAME；(2) env 单独即可解锁未配置 repo 的 `validate_effective_user`；(3) 空白字符串 env 不掩盖下层有效来源。
- **README**：测试数 506 → 610 + 1 skipped；§3.3 多人协作重写：列出 4 档用户名解析优先级 + 4 行常见失败模式表格 + 64 用例并发子集全绿；§5「计划中」把 embedding/RAG 从计划项移到「已落地」段（v0.7.5 / v0.8.0 / v0.9.0）+ 追加 v0.10.0 / v0.10.1 收尾说明。
- **MemorySystemDesignDocument.md**：头部状态行更新到 v0.10.1；§15.5.1 新增「v0.10.1 团队接入扫尾」小节，列出三个修复入口；§15.6 表格补 v0.10.1 行；§17 新增「UE Facet 数据模型」小节，分项目级（`memory_ue_facets.py`）+ 记录级（六个 facet 字段）+ 显式不做的扩张三段，对齐 §1 "无 UE 编辑器依赖" 边界。
- **`/memories/repo/memory_mcp_roadmap_status.md`**：测试规模 607 → 610，新增 v0.10.1 条目到「已完成」段。

### 验证

- `pytest tests/memory_server/test_user_validation.py -q`：35 passed in 0.12s（含新增 3）。
- 并发/多用户子集复跑：`test_concurrent_writes` + `test_multi_agent_stress` + `test_multi_user` + `test_user_validation` + `test_shared_overwrite_rejected` = **64 passed in 15.46s**，全绿。
- `pytest tests/ -q` 全量回归（见末段验证）：见下行实际运行记录。

### 影响范围 / 兼容性

- `get_current_user()` 行为零回归：未 export `MEMORY_MCP_USER` 时与 v0.10.0 完全一致。
- 测试侧 autouse fixture 仅 `delenv`，对未触碰该变量的测试透明。
- 文档侧 0 代码修改；UE facet 小节描述的全部 6 个字段与 `memory_ue_facets.py` 在 v0.6.0 P1-2 起就已存在，本次只是把已有事实正式纳入设计文档。

---

## v0.10.0 — P4 LLM 软增强余项收口

> **状态：§15.3 P4 全部余项落地；测试 571 → 607 passed (+36)，1 skipped 不变。** v0.10.0 默认所有 capability `enabled=False`，老调用路径零行为变化；用户可在 `.ai-memory/config.json` 的 `llm_defaults` 块按 capability 开启。

### 新增模块

- `memory_query_rewrite.py`：`rewrite_query(client, query, *, max_variants=3, context_hint=None, cache=None) -> QueryRewriteResult`。SHA-256 缓存（`qrw::` 前缀），markdown fence 兼容，去重 echo，`HARD_MAX_VARIANTS=8` 硬封顶；`MAX_VARIANT_CHARS=200`。
- `memory_snapshot_narrative.py`：`generate_snapshot_narrative(client, records, *, target, label, cache=None) -> SnapshotNarrativeResult` + `inject_narrative(snapshot_md, section)`。后者 idempotent — 重复注入只替换 `## Narrative (LLM)` 块，不堆叠；空 section 直接返回原文。
- 已在 `memory_llm_runner.run_llm_capability` (v0.10.0 早期) 之上消费：dispatch 走 `_run_query_rewrite` / `_maybe_generate_snapshot_narrative` 两个 helper，七状态包绕（`ok` / `disabled` / `unavailable` / `timeout` / `budget_exceeded` / `failed` / `invalid_capability`）+ fallback 保留原始 status 供诊断。

### 接入点

- `memory_retrieval._rank_records(extra_queries=...)`：每条记录 `_query_match_score(primary)` 与各 variant 取 max；`memory_get_important_memories` / `memory_retrieve_context` 增加 `query_variants: list[str] | None = None`。
- `server_dispatch`：`retrieve_context` / `important_memories` 检测 `args["rewrite_query"]` → 走 `_run_query_rewrite` → 把 LLMOutcome 落到 `result["query_rewrite"]`；`compile` op 透传 `narrative=bool(args.get("narrative", False))`。
- `memory_compile_views.compile_snapshot_target(narrative: bool=False)`：仅 `weekly_snapshot` / `monthly_snapshot` 触发；`inject_narrative` 在 deterministic 模板生成后注入；LLMOutcome 落到 `result["narrative"]`。
- `server_tools.memory_context` schema：新增 `rewrite_query` (bool)、`rewrite_max_variants` (int 1–8 默认 3)、`rewrite_context_hint` (string)、`narrative` (bool)。
- CLI：新增 `weekly-snapshot-rebuild` / `monthly-snapshot-rebuild`（`--user` `--task-id` `--branch` `--as-of` `--narrative`）。

### 配置

- `memory_config.MemoryConfig.llm_defaults: dict | None`。`DEFAULT_CONFIG_CONTENT` 新增带注释示例：

```jsonc
"llm_defaults": {
  "enabled": false,
  "timeout": 30.0,
  "max_tokens": 1024,
  "capabilities": {
    // "query_rewrite": { "enabled": true, "max_tokens": 256 },
    // "snapshot_narrative": { "enabled": true, "timeout": 60 }
  }
}
```

`_parse_llm_defaults` 仅保留 `enabled` / `timeout` / `max_tokens` 三个 knob，块缺失返回 `None` 由 runner 用内置默认；不会因为缺字段抛错。

### 测试

- `test_llm_runner.py`（新，15 测试）：profile 解析三档（built-in / global / cap-override）、policy 拒绝未知 capability、七状态全覆盖、fallback `ok=True` 但 `status` 保留原始失败、`to_dict_round_trip`。
- `test_query_rewrite.py`（新，10 测试）：happy path / markdown fence / max_variants / hard cap / 去 echo / empty query / 非 JSON 优雅降级 / 空数组 / cache hit / cache 按 query 隔离。
- `test_snapshot_narrative.py`（新，7 测试）：narrative 生成、空 records 短路、cache 命中、按 (target,label) 隔离、`inject_narrative` 位置在 `# Title` 后且在 `## Window` 前、idempotent 替换、空 section noop。
- `test_llm_policy.py` parametrize 扩容：`summarize_recall` / `snapshot_narrative` 进 LLM-native；`rebuild_key_document` / `query_rewrite` 进 hybrid。

### 验证

- `pytest tests/ -q`：**607 passed, 1 skipped in 28.7s**（基线 571 + 1）。
- 老调用链路（无 `llm_defaults` 块、未传 `rewrite_query` / `narrative`）行为零变化。

---

## 2026-04-27 (路径解耦：插件可部署在任意相对路径)

> **状态：插件位置不再硬编码 `<RepoRoot>/MCP/Memory/`，可部署在 `Tools/Memory/`、`vendor/memory-mcp/` 等任意相对路径；同时显式支持 MCP server + CLI 两种使用模式。** 测试维持 506 passed。

### 改动概览

- 新增 `scripts/_Resolve-MemoryRoots.ps1` 共享 helper：以 `-MemoryRoot` 为输入，按 `-RepoRoot` 参数 → `$env:MEMORY_REPO_ROOT` → 标记文件向上查找（`.git` / `.svn` / `.hg` / `pyproject.toml` / `*.uproject` / `*.code-workspace` / `*.sln`）→ 旧布局兜底 + warning 的顺序解析；返回 `MemoryRoot` / `RepoRoot` / `MemoryRelToRepo`（posix 相对路径，可能为 `null`）。
- 5 份测试（`conftest.py`、`test_concurrent_writes.py`、`test_multi_agent_stress.py`、`test_robustness_followup.py`、`test_write_robustness.py`）：`PROJECT_ROOT = parents[4]` + `MEMORY_ROOT = PROJECT_ROOT / "MCP" / "Memory"` → `MEMORY_ROOT = parents[2]`，去除外部目录深度假设。
- 9 份 PS 脚本接入 helper：
  - `setup_mcp.ps1`：新增 `-RepoRoot` / `-AbsolutePath`，`mcp.json` 路径占位改为 `${workspaceFolder}/<MemoryRelToRepo>/...`，`<MemoryRelToRepo>` 由 helper 动态计算（不再写死 `MCP/Memory`）；`-WorkspaceRoot` 保留为 `-RepoRoot` 的旧别名。
  - `deploy.ps1`：新增 `-RepoRoot`，转发给 `setup_mcp.ps1`。
  - `scripts/bootstrap.ps1`、`scripts/run_memory_server.ps1`、`scripts/deploy_memory_mcp.ps1`：新增 `-RepoRoot` 参数 + 调用 helper。
  - `scripts/Resolve-MemoryTestPython.ps1`：签名改为可选 `-MemoryRoot` / `-RepoRoot`，不再假设插件位于 `<RepoRoot>/MCP/Memory`。
  - 6 份 `run_memory_*_tests.ps1`：去除 `parents[3]` 风格的 repo root 推导，`memoryRoot` 直接从 `$PSScriptRoot/..` 取。
- README §2 重写「安装方式」：增加 `<MemoryRoot>` 概念、`<RepoRoot>` 解析顺序说明、CLI 模式示例（`python -m servers.memory_server.cli ... guard|health|backup|...`）。

### 验证

- `pytest tests/ -q`：506 passed in ~18s（多次重跑稳定）。
- `Resolve-MemoryRoots` 6 种场景实测全通过：
  1. 默认 `MCP/Memory/` 自检 ✓
  2. 显式 `-RepoRoot` ✓
  3. `$env:MEMORY_REPO_ROOT` ✓
  4. 模拟 `Tools/Memory/`（带 `.git`）→ `MemoryRelToRepo=Tools/Memory` ✓
  5. 模拟 `vendor/memory-mcp/`（带 `pyproject.toml`，深 2 层）→ `MemoryRelToRepo=vendor/memory-mcp` ✓
  6. 无标记目录 → 警告 + 兜底 ✓
- `setup_mcp.ps1` 端到端在临时 `Tools/Memory/` 布局下生成的 `mcp.json` 正确写出 `${workspaceFolder}/Tools/Memory/...` 占位（不再硬编码 `MCP/Memory`）。
- `run_memory_guard_tests.ps1` 等脚本独立运行通过。
- README §2 重写：287 → 124 行，删冗余的 `bootstrap` vs `setup_mcp` 重复段、合并 LLM 接入说明，保留 `<MemoryRoot>`/`<RepoRoot>` 占位约定。

### 影响范围 / 兼容性

- `setup_mcp.ps1 -WorkspaceRoot <path>` 旧用法仍可用（自动映射成 `-RepoRoot`）。
- 旧的 `MCP/Memory/` 默认布局零行为变化；新增 `Tools/Memory/` 等部署位置时不再需要改源码，只要把整棵插件目录拷过去即可。
- Python runtime 早就 100% 依赖 `MemoryConfig.repo_root`（来自 `--root` CLI 参数），本次未做 runtime 改动。

---

## 2026-04-27 (v0.7.1-P4C-slice2：LLM 档 + 三档降级 + key_documents 配置)

> **状态：LLM 档落地（hybrid：LLM 提议 + deterministic 兜底）；`key_documents.mode` + `renderers.prefer_order` 配置生效；`embedding` 档仍保留为 `not_implemented`（按用户要求暂不做 RAG）。** 测试 497 → 506（+9）。

### 配置

- `memory_config.DEFAULT_CONFIG_CONTENT` 新增 `key_documents` 段：
  - `mode ∈ {auto(默认), manual, disabled}`：`manual` / `disabled` 下 `rebuild_key_documents` 直接返回 `error="key_documents_manual_mode"`，磁盘零侧效，保 v0.6 行为兼容。
  - `renderers.prefer_order`：默认 `["llm", "deterministic"]`，`auto` 模式按此顺序逐档尝试，第一档成功即停。
- `MemoryConfig` 新增 `key_documents_mode: str` 与 `key_documents_prefer_order: tuple[str, ...]` 字段，`load_config` 经 `_parse_key_documents_mode / _parse_key_documents_prefer_order` 解析；非法 mode/renderer 自动回落默认值并强制保留 `deterministic` 兜底。

### memory_key_documents 升级

- 新函数 `render_llm_document(config, *, doc_key, user, llm_client, generated_at)`：
  - 通过 `make_raw_record` 把 `CompilableRecord` 包装为 raw record dict（`provenance=raw_capture` / `immutable=True`），喂给 `map_reduce_distill`。
  - **schema 不变**：header（`generated_by=memory-mcp renderer=llm …`）、`# Title`、`> _role_` 三段由 deterministic 代码控制；LLM 只填正文，不可写 H1，不可加任何前后缀。
  - 系统提示强制约束：grounded-only / no fabrication / 同语言。空语料返回与 deterministic 一致的「No raw records」骨架。
- `_rebuild_one(config, *, doc_key, user, request_id, tier, llm_client)`：新增 `tier` 与 `llm_client` 参数；任一档抛出异常都返回 `render_failed` 让编排器尝试下一档。
- `rebuild_key_documents(...)` 改为编排器：
  - 默认 `renderer="auto"`：按 `config.key_documents_prefer_order` 走（`llm` 不可用自动跳过；deterministic 兜底永远在末位）。
  - `renderer="llm"`：强制 LLM；client 不可用时 per-doc 报 `llm_unavailable`（不静默降级）。
  - `renderer="deterministic"`：强制 deterministic 单档。
  - `renderer="embedding"`：返回 `not_implemented`（占位，待后续 RAG 阶段）。
- 新辅助 `_maybe_build_llm_client()`：lazy 构建 LLMClient，构建失败返回 `(None, llm_unavailable_err)`，主路径不抛。

### 测试新增（+9）

- `test_rebuild_returns_manual_mode_when_disabled` / `…disabled_value`：mode=manual/disabled → `key_documents_manual_mode`，文件未创建。
- `test_rebuild_auto_falls_back_to_deterministic_when_llm_unavailable`：auto 模式 LLM 不可用 → renderer=deterministic 成功。
- `test_rebuild_llm_renderer_without_client_returns_llm_unavailable`：renderer=llm 且 client 不可用 → per-doc `llm_unavailable`。
- `test_rebuild_embedding_renderer_returns_not_implemented`。
- `test_render_llm_document_uses_map_reduce_distill`：stub 客户端 + monkeypatch `map_reduce_distill`，验证 raw 包装 + 输出含 LLM 正文 + header `renderer=llm`。
- `test_rebuild_auto_uses_llm_when_client_available`：auto 模式有 client 时优先 LLM 档。
- `test_rebuild_auto_falls_back_when_llm_render_raises`：LLM 渲染中途抛错 → auto 回落 deterministic 成功。
- `test_config_parses_key_documents_section` / `…defaults_when_key_documents_absent`。
- 模块级 autouse fixture `_disable_llm_by_default` 强制屏蔽真 LLM，避免误触 DeepSeek 计费。

### 影响 / 不变量

- 已实现层 `LLM_CAPABILITY_MATRIX["rebuild_key_document"] = "hybrid"`：LLM 提议 + deterministic 包装持久化，与矩阵约定一致。
- raw 仍 `immutable=True`：LLM 渲染只读 raw，不写 raw；只覆盖派生关键文档。
- 锁 / backup(tag=pre_rebuild) / atomic write / append_event(`key_document_rebuilt`) 全程复用 P4C-slice1 通道；新增 `renderer_used` 字段进入审计事件。
- 手写检测 + 归档语义不变；编排器各 tier 共用同一锁/归档/写入路径，不允许中间状态泄漏。

### 仍待落地

- `embedding` 档（RAG 检索 + 模板拼装）：按用户指示推迟到 P5 之后。
- `compiled/snapshots/<doc>-<timestamp>.md` 显式回滚：当前依赖 `backups/` 的 `pre_rebuild` 批次 + `_atomic_write_text` 的 tmp+replace 已构成"原内容永不丢"的等价保证；如需独立 snapshots 子目录与时间戳，留待真实回滚事故触发。

---

## 2026-04-27 (v0.7.1-P4C-slice1：关键文档可重建 — deterministic 档落地)

> **状态：deterministic 档完整落地；LLM / embedding 档保留契约接口，返回 `not_implemented`。** 测试由 481 增至 497（+16）。

### 新增

- `servers/memory_server/memory_key_documents.py`（新模块）：
  - `KEY_DOCUMENTS` / `KEY_DOCUMENT_KEYS`：四份关键文档的 spec 表（rel_path / title / role / include_kinds / preferred_tags / max_items）。
  - `build_generated_header` / `is_generated` / `parse_generated_meta`：派生层头注释的生成与识别。
  - `select_records_for`：按 `record_kind` 过滤、按偏好标签 + 时间倒序排序，限制 `max_items`。
  - `render_deterministic_document`：完全无 LLM 即可成文，schema 固定（标题、角色 blockquote、每条记录 `## 标题 + meta + body`）。
  - `_archive_manual_edit`：rebuild 前若现存文件不含 `generated_by` 头则归档到 `memory-bank/archive/manual-edits/<doc>-<timestamp>.md`，再写入派生层。
  - `rebuild_key_documents(config, *, targets=None, user=None, renderer="deterministic")`：编排器。
- `tests/memory_server/test_key_documents.py`：16 个 TDD 测试覆盖 header 契约、KEY_DOCUMENTS 完整性、deterministic 渲染、空语料兜底、归档语义、幂等不重复归档、未知 target 报错、`renderer="llm"` 暂报 `not_implemented`、通过 `memory_context` dispatch 能调用。

### 修改

- `servers/memory_server/server_dispatch.py`：`_dispatch_memory_context` 新增 `operation="rebuild_key_documents"` 分支；error message 同步列出新操作。
- `servers/memory_server/server_tools.py`：`memory_context.inputSchema` 的 `operation` 枚举追加 `rebuild_key_documents`，新增 `targets[]`（enum 限定四个文档）与 `renderer` 字段。
- `servers/memory_server/memory_llm_policy.py`：`LLM_CAPABILITY_MATRIX` 登记 `rebuild_key_document: "hybrid"`（避免后续接入 LLM 时被 `UnknownCapability` 卡住）。

### 安全语义复用

- 复用 `file_lock` 保证跨进程互斥；`backup_files(tag="pre_rebuild")` 留下回滚点；`_atomic_write_text` 保证 fsync + os.replace；`append_event(event_type="key_document_rebuilt")` 进 audit 流。
- 显式**绕过** `memory_writer.memory_write` 的 `user_scoped` 重定向与 `append_only` 拒绝 — 因为关键文档作为派生视图必须按 `rel_path` 整体覆盖；这一豁免封装在 `memory_key_documents._rebuild_one`，不开放给外部调用方。

### 仍未落地（留待 P4-C-slice2/3）

- LLM renderer（`renderer="llm"`）：调用 `map_reduce_distill` 提议正文，deterministic 校验头部并持久化。
- embedding-template renderer（`renderer="embedding"`）：用本地向量 + 模板，无外部 LLM 时仍优于 deterministic。
- `key_documents.mode ∈ {auto, manual, disabled}` 配置开关：当前默认行为等效 `auto`，但配置节尚未加入 `MemoryConfig`。
- 失败时回滚到 `compiled/snapshots/<doc>-<timestamp>.md` 的链路：当前依赖 `pre_rebuild` 备份，专门快照层未实现。
- `memory_users.py` 对 `activeContext.md` 的 user_scoped 重定向 与 `memory_shared_compactor.py` 的周归档：和派生视图语义存在冲突，需在后续 slice 协调。

---

## 2026-04-27 (v0.7.1 doctrinal pivot：无感原则 + 关键文档可重建)

> **状态：文档级方向收敛，未改代码、未改测试。**README §0、设计文档 §0.3/§1/§2.0/§4.5 同步声明：用户 / AI 只写 raw；`activeContext.md` / `progress.md` / `techContext.md` / `systemPatterns.md` 重新定位为**派生关键文档**，由系统按「LLM → 本地 embedding 模板 → deterministic」三档自动重建。这是「raw 不可改 + distilled 可重建」契约（v0.6.1 §2.1.A）的产品级延伸，把"无感"从"LLM 提炼无需人工确认"推到"关键文档无需人工编辑"。

### 设计契约（必须在 P4-C 实现时严格遵守）

- **raw 永远 `immutable=True`**：任何关键文档损坏不污染真源；删派生文档 → rebuild → 恢复。
- **关键文档头部强制注释**：`<!-- generated_by=memory-mcp renderer=llm|embedding_template|deterministic source_record_ids=[…] generated_at=… config_hash=… -->`。git diff / audit 工具必须能识别派生层。
- **三档 renderer schema 一致**：同段落顺序、同 section 标题；只允许内容详尽度差异，不允许结构差异，便于降级时不破坏外部消费方。
- **deterministic 必须可独立成文**：完全无 LLM 时按 record_kind / scope / recency / importance 排序后用模板拼装，**无臆造、无遗漏字段**。
- **手写检测 + 归档**：rebuild 前若文件无 `generated_by` 头注释 → 自动归档到 `archive/manual-edits/<doc>-<timestamp>.md` 再重建，避免静默覆盖人工编辑。
- **失败回滚**：任意档失败先尝试下一档；全部失败时回滚到 `compiled/snapshots/<doc>-<timestamp>.md`。
- **过渡期开关**：`.ai-memory/config.json` `key_documents.mode ∈ {auto(默认), manual(v0.6 行为)}`。

### 文档变更

- `README.md`：新增 `§0 产品定位（最高原则：无感）`；`§3.2 推荐工作流` 改写为「只写 raw → 关键文档自动重建」；新增 `§4.5.1 关键文档 = 派生视图`；`§5.3` 路线新增 `P4-C 关键文档可重建`，调整为 `v0.7.x` 路线。
- `MemorySystemDesignDocument.md`：状态行更新为 v0.7.1；`§0.3` 主线收敛声明 raw + 自动重建；`§1` 文档目标置顶「无感原则」并补「无 LLM 兜底重建」；`§2.0 自动化原则` 升级为 `§2.0 无感原则`，原文保留为 `§2.0.A`；`§4.5 编译记忆` 显式纳入四份关键文档。
- `DEVLOG.md`：本条目。

### 代码影响（本条目不改代码）

- 现有 `memory_context` / `memory_compiler` / `memory_compile_render` / `memory_llm_pipeline` 已具备多数构件；P4-C 待落地：
  - `memory_context` 新增 `operation="rebuild_key_documents"` 与 `targets[]` 参数。
  - `memory_compile_render` 新增 4 个 deterministic 模板（active/progress/tech/pattern），与 LLM renderer 共享 schema。
  - `memory_compile_writer` 复用现有 backup → tmp → fsync → replace 路径写关键文档；新增手写检测 + manual-edits 归档。
  - `memory_config` 新增 `key_documents.mode`、`key_documents.renderers` 段。

### 不变量保留

- raw immutable / authoritative / event-logged：不动。
- `LLM_CAPABILITY_MATRIX` 单一事实源：P4-C 新增 `rebuild_key_document` 能力（`hybrid` — LLM 提议，deterministic 兜底，必须登记）。
- 共享文件 append-only / 多人写入隔离 / If-Match 乐观锁：不动。关键文档列入「整文件 replace 但带 generated_by 头注释 + manual-edits 归档」的特例策略。
- 测试 481 不动；P4-C 落地时按 TDD 加新用例。

### 验证（仅文档）

```powershell
git diff --stat MCP/Memory/README.md MCP/Memory/MemorySystemDesignDocument.md MCP/Memory/DEVLOG.md
```

---

## 2026-04-27 (v0.7.0-P4-B 口径收敛与 distilled 持久化修正)

> **状态：修复 LLM/Memory 文档与实现口径不一致。`memory_write(distill=true)` 现在持久化为 `record_kind="distilled_summary"` + `status="distilled"` 的 replaceable 派生记录，而不是 raw `observation`；README / 设计文档统一为 4 facade、P4/P4-B 已完成主体、distill 为同步执行。LLM policy 矩阵补齐 6 个 `memory_enhance` 能力。测试 475 → 481 (+6 policy 口径钉死)。**

### 修复

- **distilled 落盘语义对齐**：`server_dispatch._run_distill_for_write` 的第二条记录改为 `distilled_summary` / `status=distilled` / `scope=user_private`，并写入 `provenance=llm`、`replaceable=true`、`authoritative=false`、`model`、`distilled_at`、`derived_from_record_ids=[raw_id]`。
- **结构化记录 schema 扩展**：`memory_records.py` 增加 `distilled_summary` record kind 与 `distilled` status，允许 v2 Front Matter 保存 distilled 派生层元数据。
- **LLM policy 单一事实源补齐**：`LLM_CAPABILITY_MATRIX` 登记 `classify_record` / `extract_candidates` / `merge_candidates` / `generate_skill_candidate` / `explain_conflict` / `generate_handoff`，避免 README/设计文档写着已登记但代码未登记。
- **文档口径统一**：README 与设计文档统一默认 4 facade、v0.7.0-P4/P4-B 当前状态、同步 distill 行为、测试数 481、P5 向量召回仍为后续能力。

### 验证

```powershell
MCP\Memory\.venv\Scripts\python.exe -m pytest MCP\Memory\tests\memory_server\test_llm_policy.py MCP\Memory\tests\memory_server\test_llm_pipeline_dispatch.py MCP\Memory\tests\memory_server\test_llm_pipeline.py MCP\Memory\tests\memory_server\test_llm.py -q
# 115 passed

powershell -ExecutionPolicy Bypass -File MCP/Memory/scripts/run_memory_all_tests.ps1
# 481 passed in 17.55s
```

---

## 2026-04-26 (v0.7.0-P4-B LLM 增强接口 + memory_enhance facade)

> **状态：v0.7.0 路线 P4-B 完成。新增单一 facade `memory_enhance`，按 `operation` 路由 6 个 opt-in LLM 增强能力。read-only：返回结构化建议不写盘；上层决定是否以 `status="candidate"` 调 `memory_write_record`。测试 442 → 475 (+33)。同日完成 DeepSeek 真接口冒烟（`'OK'`，12 tokens，¥0.000013）确认 OpenAI-compatible wire format 可联通。**

### 新增

- **`memory_llm_enhance.py`**（NEW，~430 行）：6 个 LLM 增强能力函数。
  - `_parse_json_response(text, *, expected_top=dict)`：容忍 ```` ```json ``` ```` 围栏 + 杂散文本，提取首未尾成对 `{}`/`[]`，解析失败抛 `LLMEnhanceError`。
  - `_llm_json_call(client, *, system, user, ..., op, expected_top=dict) -> (parsed, meta)`：统一调用，捕获 `usage_delta = {prompt_tokens, completion_tokens, estimated_cost_cny}`，meta 携带 `model`。
  - `classify_record(client, *, content, allowed_kinds, allowed_scopes, allowed_tags, ...)` → `{ok, record_kind, scope, tags, confidence∈[0,1], rationale, model, usage_delta}`；kind/scope 必须命中入参 allowlist（默认取 `ALLOWED_RECORD_KINDS / ALLOWED_SCOPES`）；tags 自动过滤；confidence clamp。
  - `extract_candidates(client, *, content, source_record_id=None, ...)` → `{ok, candidates:[{kind∈{claim_candidate,rule_candidate}, content_markdown, confidence, tags, rationale, source_record_id}], ...}`；空数组合法。
  - `merge_candidates(client, *, candidates, ...)` → `{ok, groups:[{representative_id, member_ids[], merged_content_markdown, rationale}]}`；必须输出输入 id 的**全分区**（无遗漏 / 无重复 / 无未知 id），否则 `LLMEnhanceError`。
  - `generate_skill_candidate(client, *, records, max_chars_per_record=4000, ...)` → `{ok, title, content_markdown, tags, confidence, rationale, source_record_count, ...}`；记录正文超长按字符截断。
  - `explain_conflict(client, *, record_a, record_b, ...)` → `{ok, record_ids:[a,b], conflict_type∈{contradiction,overlap,scope_mismatch,no_conflict,unclear}, severity∈{low,medium,high}, explanation, resolution_options[], ...}`；severity 自动小写规范化。
  - `generate_handoff(client, *, records, task_id=None, branch=None, ...)` → `{ok, task_id, branch, summary_markdown, key_points[], open_questions[], next_actions[], source_record_count, ...}`；空字符串元素自动过滤。

### 接入

- **`server_dispatch.py`**：新增 `_ENHANCE_OPS = {"classify_record","extract_candidates","merge_candidates","generate_skill_candidate","explain_conflict","generate_handoff"}` 与 `_dispatch_memory_enhance(config, args)`；按 `operation` 路由；try/except → `error_result("enhance_failed:<op>", str(exc))`；LLM 工厂返回 `(None, error_result("llm_unavailable", ...))` 时直接透传。`_dispatch_tool` 中 `name == "memory_enhance"` 路由到该函数；`__all__` 加入 `_dispatch_memory_enhance`。
- **`server_descriptions.py`**：新增 `memory_enhance` 条目（read-only opt-in LLM facade，6 ops 列举）。
- **`server_tools.py`**：在 `_build_facade_tools` 注册 `Tool(name="memory_enhance", ...)`，schema 含 `operation` 枚举（6 op）+ `content_markdown` / `content` / `source_record_id` / `allowed_kinds|scopes|tags` / `candidates[]` / `records[]` / `record_a` / `record_b` / `task_id` / `branch` / `max_tokens` / `max_chars_per_record(default 4000)` / `thinking` / `reasoning_effort`，`required:["operation"]`，`additionalProperties:false`。

### 测试

- **`tests/memory_server/test_llm_enhance.py`**（NEW，22 项）：`_parse_json_response` 围栏 / 杂散提取 / 空 / 非法 JSON / wrong top；`classify_record` 过滤非法 tag + clamp confidence / 拒绝 allowlist 外 kind / 拒绝 allowlist 外 scope / 拒绝空 content；`extract_candidates` 类型规范化 / 空数组合法 / 拒绝非法 kind / 拒绝空 content；`merge_candidates` 全分区 / 漏 id / 重复 id / 未知 id / 空输入；`generate_skill_candidate` 结构 / 截断 / 空记录；`explain_conflict` severity 规范化 / 拒绝非法 conflict_type / 拒绝空记录；`generate_handoff` 字段透传 / 过滤空字符串 key_point / 拒绝缺字段。
- **`tests/memory_server/test_llm_enhance_dispatch.py`**（NEW，6 项）：classify 派发 happy path / 未知 operation → `invalid_input` / extract 派发（source_record_id 透传）/ generate_handoff 派发（task_id 透传）/ LLM 不可用 in-band `llm_unavailable` / LLM 响应非法 → `enhance_failed:classify_record`。
- **`test_server_split.py`**、**`test_mcp_protocol.py`**、**`test_budget_rotation_dynamic.py`**：facade 工具列表 expectations 同步加入 `memory_enhance`（默认 facade 由 3 → 4；admin 23 → 24）。

### 真接口冒烟（DeepSeek）

```text
client.complete_text("Reply with just the word OK.")
→ 'OK'   prompt_tokens=11, completion_tokens=1, est. cost ¥0.000013
```

确认 `MCP/Memory/llm_config.local.json` 配置（`base_url=https://api.deepseek.com`，`model=deepseek-v4-flash`）的 OpenAI-compatible wire format 可端到端联通；`load_llm_config(plugin_root=Path('.'))` 必须传 `Path` 而不是 `str`。

### 验证

```powershell
cd MCP/Memory
.\.venv\Scripts\python.exe -m pytest tests/ -q   # 475 passed
```

### 硬约束保留

- `memory_enhance` 是 **read-only**：从不写盘；上层决定是否以 `status="candidate"` 通过 `memory_write_record` 落盘。
- raw 仍 immutable + authoritative；distilled 仍 replaceable。
- 全部能力 opt-in，未调用即 0 LLM 调用 0 token。
- LLM 失败统一 in-band 错误（`enhance_failed:<op>` / `llm_unavailable` / `invalid_input`），绝不 silent 失败。
- 6 项能力名最终对应 `memory_llm_policy.LLM_CAPABILITY_MATRIX` 的注册项；未注册即 `UnknownCapability`。

---

## 2026-04-25 (v0.7.0-P4 LLM pipeline 接入主路径)

> **状态：v0.7.0 路线 P4 完成。`memory_write` / `memory_context` 主路径首次具备 opt-in LLM 蒸馏与召回概括能力。测试 416 → 442 (+26)。raw 仍 immutable + authoritative；LLM 失败仅 in-band 错误，绝不丢失主写。**

### 新增

- **`memory_llm_pipeline.py`**（NEW，~430 行）：单一 LLM token-spend orchestrator。
  - `compute_distill_cache_key(raws, *, model, system, user)` → SHA-256 over `{model, system, user, records:[id,content,source,captured_at]}`，同输入同模型不重复花 token。
  - `DistillCache(entries: dict[str,str])` 进程内字典缓存。
  - `chunk_raw_records(raws, *, max_input_tokens, overhead_tokens=512)` 按 token 估算贪心切块，floor `MIN_CHUNK_BUDGET_TOKENS=1024`。
  - `map_reduce_distill(client, raws, *, ...) -> dict` 多 chunk 自动 map-reduce（每 chunk 独立蒸馏 → REDUCE_SYSTEM_PROMPT 合并），单 chunk 直出；返回的 distilled 记录附 `pipeline={chunks, llm_calls, cache_hits, reduced}`；拒绝 distilled 输入；空集抛 `LLMConfigError`。
  - `summarize_records_for_recall(client, records, *, query=None, ...) -> dict` 对召回结果做相同的 chunk + map-reduce 概括，返回 `{summary, model, chunks, llm_calls, cache_hits, reduced}`。

### 接入

- **`memory_write` / `op=record`**：新增 opt-in `distill: bool` + `distill_user_instruction: str` + `distill_tags: array` + `distill_max_tokens: int`。
  - 主写成功后构造 `make_raw_record(record_id=raw_id, source="memory_write:<path>", captured_at=now, author=...)`；
  - `map_reduce_distill(...)` 蒸馏并 `memory_write_record(record_kind="observation", scope="user_private", derived_from_record_ids=[raw_id], ...)` 落第二条记录；
  - 返回 `result.distilled = {ok, summary, distilled_record_id, distilled_path, model, pipeline, usage, persist_result}`；
  - LLM 不可用 / 失败 → `{ok: false, error: "llm_unavailable"|...}`，主写已落盘不会被回滚。
- **`memory_context` / `op=retrieve_context`**：新增 opt-in `summarize: bool` + `summary_query: str` + `summary_max_tokens: int` + `summary_max_chars_per_record: int`(default 4000)。
  - 召回成功且有记录后，对 `context_items`/`selected_records` 截断每条 `summary_max_chars_per_record` 字符，喂给 `summarize_records_for_recall`；
  - 返回 `result.summary = {ok, summary, model, pipeline:{chunks, llm_calls, cache_hits, reduced}, usage}`；
  - 无记录 → `{ok: false, error: "summarize_skipped"}`。

### 测试

- **`tests/memory_server/test_llm_pipeline.py`**（NEW，20 项）：缓存 key 确定性 / 模型敏感 / prompt 敏感 / 顺序敏感 / 额外元数据不影响；chunk 单 / 拆分 / 零上限 / 空集 / 类型守卫；map-reduce 单 chunk 无 reduce / 缓存命中短路 / 多 chunk 触发 reduce / 拒绝 distilled / 空集报错；summarize 单 chunk / 缓存 / 截断 / 空集 / query 改变 cache key。
- **`tests/memory_server/test_llm_pipeline_dispatch.py`**（NEW，6 项）：distill 持久化 → 1 raw + 1 observation；distill 默认关闭（0 transport call）；LLM 不可用时 in-band `llm_unavailable`；summarize 附挂 summary；summarize 默认关闭；summarize 无记录返回 `summarize_skipped`。

### 验证

```powershell
cd MCP/Memory
.\.venv\Scripts\python.exe -m pytest tests/ -q   # 442 passed
```

### 硬约束保留

- raw 写一次冻结（immutable=True，authoritative=True），LLM 输出**永不**直接落入 raw 真源或非 LLM 索引。
- distilled 仍 `replaceable=True` + `derived_from=[raw_ids]`，可被任意 LLM 重做。
- 全部 LLM 接入 opt-in；未触发开关时 `memory_write` / `memory_context` 行为与 v0.6.1 完全一致，0 LLM 调用 / 0 token。
- 能力归属仍走 `memory_llm_policy.LLM_CAPABILITY_MATRIX` 单一事实源；未注册能力 → `UnknownCapability`。

---

## 2026-04-26 (v0.6.0 完整发布: P0+P1+P2 全部落地)

> **状态：v0.6.0「开箱即用稳健性版」全部 11 项 (P0×3 + P1×5 + P2×3) 完成。测试 230 → 333 (+103)。所有项严格遵循 §15.9.5 TDD 纪律：先写失败测试 → 最小实现 → 全量回归。**

### P0（数据安全）

- **P0-1 user id 强校验**：`memory_users.py` + `memory_write` 前置守卫，hard-reject 占位 / 路径注入字符；`mcp.allow_unknown_user=true` 显式覆盖。+32 测试。
- **P0-2 共享文件 overwrite 强制拒绝**：`memory_write` 对 `append_only` 路径 + `mode=overwrite` 默认返回结构化 `shared_overwrite_forbidden`，旧的 silent-downgrade 行为通过 `mcp.shared_overwrite_policy="downgrade"` opt-in。+4 测试。
- **P0-3 启动 auto-maintenance**：新增 `memory_auto_maintenance.run_if_due(config)` 模块；server 启动时 best-effort 调用，按时间/索引陈旧度/事件量阈值自动跑 health + rebuild_index；状态写入 `.ai-memory/last_maintenance.json`，失败记录到 events.jsonl 但不阻塞主链路；`mcp.auto_maintenance.enabled=false` 可关闭。+9 测试。

### P1（开箱即用 / 体验）

- **P1-1 `bootstrap.ps1` 单一入口**：venv → deps → user-id 提示 → `.vscode/settings.json` + `.vscode/mcp.json` 合并 → 健康检查绿灯。所有风险 JSON 操作下沉到 `memory_bootstrap.py` 并被 pytest 覆盖。+8 测试。
- **P1-2 UE facet 自动推断**：`memory_ue_facets.detect_ue_facets` 扫描 `*.uproject` + `Source/**/*.Build.cs` + `Plugins/**/*.uplugin` → `.ai-memory/ue_facets.json`；`memory_write_record` 对未识别的 `module_names` / `plugin_names` 给出非阻塞 `ue_unknown_components` 警告。+11 测试。
- **P1-3 shared append auto-compact**：`memory_shared_compactor.auto_compact_shared_file` 当行数超阈值时把旧条目折叠到 `memory-bank/archive/<basename>-YYYYWW.md`，活跃文件保留尾部 N 行 + 横幅。同周内多次执行追加而非覆盖。+6 测试。
- **P1-4 `config_diagnose`**：`memory_context.operation="config_diagnose"` 返回每个关键字段的 `value` + `source` (`default`/`file`/`env`/`vscode`)。+5 测试。
- **P1-5 `link_artifact` 路径归一化**：`Content/Foo/Bar.uasset` ↔ `/Game/Foo/Bar` 双向标准化 + 去重；当 `.git/` 存在时附加 `git_sha` 到 event 与返回值。+15 测试。

### P2（健康 / 演进）

- **P2-1 `cli scale-baseline`**：`memory_baseline.write_baseline` 写 `.ai-memory/baseline.json` (memory_bank 文件数/字节、records 数、events 字节、index 大小、可选 git_sha)；`detect_regressions` 在 health_check 中以 `scale_regression` issue 出现，默认 2× 因子。+5 测试。
- **P2-2 health 启动自愈**：`memory_health_check` 调用 `_self_heal`，清理 60s 以上的 `*.tmp` 孤儿与陈旧 `.lock` sidecar；`self_heal: {tmp_removed, stale_locks_removed}` 出现在结果中。+2 测试。
- **P2-3 scoring 策略哈希一致性**：`memory_strategy_hash.STRATEGY_MANIFEST` 显式声明组件 + 默认权重，SHA-256 截短哈希；auto_maintenance event 中写入 `scoring_strategy_hash`；health_check 扫描最近 events，发现哈希漂移时报 `scoring_strategy_changed`。+6 测试。

### 验证

```powershell
.\.venv\Scripts\python.exe -m pytest tests/  # 333 passed in 15.25s
```

### 后续

- v0.7.0 LLM 摘要 / RAG 整合：保留为下个版本，与 OOTB 硬化解耦。
- 已对 `bootstrap.ps1` 做了无副作用 dry-run 设计；下一轮在真实开发机上完整 e2e 验证。
- MemorySystemDesignDocument §15.9 已与实现对齐；可作为 v0.6.0 验收清单。

---

## 2026-04-25 (v0.6.0-P0-1: user id 强校验)

> **状态：v0.6.0「开箱即用稳健性版」第一项落地。新增 `memory_users.py`，hard-reject 占位用户名（`""` / 空白 / `unknown` / 含 `/\:` 等路径注入字符），soft-warn 共享 OS 账号（`Administrator` 等）；`memory_write` 在写盘前调用 `validate_effective_user`，用结构化 `error="user_not_configured"` + `setup_hint` 替代旧 `user_required`。可通过 `mcp.allow_unknown_user=true` 显式覆盖。测试 230 → 262（+32）。**

### 范围

1. **`memory_users.py`（新增）**
   - `is_placeholder_user(name)`：hard-reject 集（`""` / 空白 / `unknown` 大小写 / 含 `/` `\` `:` `\n` `\r` `\0`）。
   - `is_ambiguous_user(name)`：soft-warn 集（`administrator` / `user` / `admin` / `root` / `guest` / `default`）。
   - `validate_effective_user(config)`：facade 写路径前置守卫；返回 `None` 通过；占位返回 `{ok: false, error: "user_not_configured", setup_hint: "..."}`；模糊返回 `{ok: true, warning: "user_ambiguous", ...}`。

2. **配置**
   - `MemoryConfig` 新增 `mcp_allow_unknown_user: bool = False`；`load_config` 解析 `mcp.allow_unknown_user`。

3. **集成**
   - `memory_writer.memory_write` 在 mode 校验之前调用 `validate_effective_user`，placeholder 时直接 short-circuit 返回 `{**err, request_id}`，不写盘、不锁、不备份。

4. **测试**
   - 新增 `tests/memory_server/test_user_validation.py`：32 项（pure-function 19、config 4、facade 2 + parametrize 展开）。
   - 调整 `test_multi_user.py::test_user_scoped_write_requires_known_user`：旧 `user_required` 统一为 `user_not_configured` + `setup_hint`。

5. **文档**
   - 设计文档 §15.9 写入 v0.6.0 P0/P1/P2 完整开发计划。
   - DEVLOG 加本条目。

### 验证

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ --tb=short
# 262 passed in ~15s
```

### 后续（v0.6.0 剩余）

- P0-2 shared 文件 overwrite 强制拒绝（结构化 `error="shared_overwrite_forbidden"`）
- P0-3 启动期 auto-maintenance（`memory_auto_maintenance.run_if_due`）
- P1/P2 详见设计文档 §15.9

---

## 2026-04-25 (v0.5.11: guard 旧配置兜底 + retrieval 预筛 + 规模冒烟)

> **状态：落实非 UE 专项稳健性收尾。修复旧配置下 guard 与写入侧多人策略不一致；`retrieve_context` / `important_memories` 增加 SQLite metadata/facet 预筛并保留 Markdown fallback；新增可重复运行的 5k 结构化记录 scale smoke；修正文档时间线。测试 227 → 230。**

### 范围

1. **guard 旧配置兜底**
   - `memory_guard.py` 新增 `_target_write_policy`，在 target 缺少 `write_policy` 时复用默认 `multi_user.user_scoped_paths` / `shared_paths_policy`。
   - `memory_guard_check` 与 `check_total_budget` 现在和 `memory_write` 一样处理旧 `.ai-memory/config.json`：`activeContext.md` 按用户分区扫描，共享 warm 文件按 append-only 策略解释。
   - 新增旧配置回归：`test_guard_uses_default_multi_user_policy_for_legacy_config_without_write_policy`。

2. **retrieval SQLite 预筛**
   - `memory_record_index.prefilter_record_paths` 基于 SQLite 派生索引按 scope/status/user/task/branch/system_area/facet 先筛候选路径。
   - `memory_retrieval._collect_records` 优先读取预筛路径；索引缺失、损坏或查询失败时无损回退全量 Markdown 扫描。
   - `iter_parsed_records` / `iter_compilable_records` 增加 `include_rel_paths`，预筛命中时直接解析候选路径，不再遍历整个 `memory-bank`。
   - `memory_retrieve_context` 只有选中记录携带 `conflicts_with` 时才扫描全量冲突，避免无冲突场景隐藏 O(N) 成本。
   - 新增回归：索引预筛命中只扫描 1 个候选；索引缺失时 fallback 仍能返回正确记录。

3. **规模冒烟与文档**
   - 新增 `MCP/Memory/scripts/run_memory_scale_smoke.py`，默认生成 5000 条通用结构化记录，输出 rebuild/search/retrieve 指标。
   - README、设计文档、`memory-admin` skill 同步 v0.5.11 状态与恢复演练。
   - 修正 DEVLOG 中 v0.5.6 条目的未来日期（2026-04-26 → 2026-04-24）。

### 验证

```powershell
.\.venv\Scripts\python.exe MCP\Memory\scripts\run_memory_scale_smoke.py --records 5000
# retrieve_prefilter.enabled=true, candidate_paths=50, retrieve_seconds≈0.07s

powershell -ExecutionPolicy Bypass -File MCP/Memory/scripts/run_memory_all_tests.ps1
# 230 passed
```

---

## 2026-04-24 (v0.5.10: 管理 CLI 入口)

> **状态：admin 能力新增一级 CLI（`python -m servers.memory_server.cli`），覆盖 guard / health / backup / compact / rebuild-index / migrate / validate / publish / archive / delete / compile / snapshot-rebuild / runtime-digest。skill `memory-admin` / `memory-snapshot-review` 已同步切换到 CLI 范式。新增 9 项 CLI 回归测试，整体 227 passed。**

### 范围

1. **CLI 入口（`servers/memory_server/cli.py`）**
   - argparse 子命令；默认输出 JSON，`--pretty` 切到缩进；exit code 与 `ok` 字段一致（`ok=true → 0`，否则 `1`）。
   - 复用现有 in-process 函数：`memory_guard_check` / `memory_health_check` / `backup_files` / `compact_memory` / `memory_rebuild_index` / `memory_migrate_records` / `memory_validate_candidate` / `memory_publish_candidate` / `memory_archive_record` / `memory_delete_record` / `memory_compile` / `memory_get_runtime_digest`，无重复实现。
   - 管理能力改为直接走 CLI；为 CI / shell 自动化提供稳定入口。

2. **skill 同步**
   - `.github/skills/memory-admin/SKILL.md`：工具矩阵改为 CLI 命令 + in-process 等价；标准维护流程改写为 PowerShell 直跑；rollback 配方改用 CLI。
   - `.github/skills/memory-snapshot-review/SKILL.md`：review 循环、历史快照重放、verification 全部切到 CLI。

3. **测试**
   - 新增 `tests/memory_server/test_cli.py`：9 项行为回归（health / guard / backup / rebuild-index / compile / snapshot-rebuild / pretty / 错误码）。
   - 整体 218 → 227 passed，无回归。

4. **文档**
   - 设计文档 §15.8 加入 v0.5.10。
   - README §1 状态行升 v0.5.10、§5 后续计划移除已完成的 CLI 入口项。

### 验证

```powershell
powershell -ExecutionPolicy Bypass -File MCP/Memory/scripts/run_memory_all_tests.ps1
```

结果：227 passed in ~15s。

---

## 2026-04-24 (v0.5.9: compiler 二轮拆分 + 管理 skill 首版)

> **状态：在 v0.5.8 基础上完成 compiler 二轮拆分与一致性 review。`memory_compiler.py` 由约 774 行降到约 330 行 thin orchestration router；新增 `memory_compile_writer` / `memory_compile_views` 两个模块；`memory_events.py` 清理遗留死代码；新增两份 `.github/skills/memory-*` 管理 skill。测试保持 218 passed，无回归。**

### 范围

1. **`memory_compiler.py` 二轮拆分**
   - 新增 `memory_compile_writer.py`：cache_key + write_compiled_view 纯 IO 层。
   - 新增 `memory_compile_views.py`：snapshot / level digest / review queue / rollback context 四个 compile 视图 + `memory_compare_snapshots`。
   - `memory_compiler.py` 现在只剩 `_matches_filter` / `memory_compile`（router）/ `memory_get_runtime_digest` 三个核心函数。
   - 通过 `__all__` re-export `memory_compare_snapshots` / `find_compile_cache_entry` / `get_record_last_used_at` / `load_compile_cache_entries`，保证 `server_dispatch.py`、`memory_retrieval.py` 与所有现存测试模块的旧 import 继续生效。

2. **死代码清理**
   - `memory_events.py` 移除遗留的 `_lock_file` / `_unlock_file` 与未使用的 `import sys`（v0.5.6 起所有事件追加都已统一走 `memory_locks.file_lock`）。

3. **管理 skill 首版**
   - 新增 `.github/skills/memory-admin/SKILL.md`：guard / backup / compact / health / index / migrate / governance 的工具矩阵、标准维护流程、故障码解读、回滚配方。
   - 新增 `.github/skills/memory-snapshot-review/SKILL.md`：review_queue 编译 → 候选走查 → validate/publish/archive → 重新编译 digest → 快照对比的固定循环；含历史快照可重放说明。

4. **文档同步**
   - 设计文档 §15.6 / §15.6.1 / §15.8 加入 v0.5.9 条目，§15.5 更新为「下一步：LLM 软增强 / 向量补召回」。

### 验证

```powershell
powershell -ExecutionPolicy Bypass -File MCP/Memory/scripts/run_memory_all_tests.ps1
```

结果：218 passed in ~15s（与 v0.5.8 同基线，无回归）。

### 风险与后续

- compiler router 的对外 surface 完全保持兼容；唯一行为差异是 `memory_compare_snapshots` 现在从 `memory_compile_views` 实现，但通过 re-export 保持原 import path 可用。
- 管理 skill 文档以「先用现有 admin tool / Python REPL」为今天的可执行路径；正式 CLI 入口仍是后续任务（计划 v0.6.x）。

---

## 2026-04-24 (v0.5.8: retrieve_context budget-first + compiler 首轮拆分)

> **状态：完成 README 后续计划 1/2。`retrieve_context` 与 `important_memories` 共用预算打包路径；`memory_compiler.py` 拆出 targets / render / scoring 三个模块。测试保持 218 passed。**

### 范围

1. **`retrieve_context` 严格 budget-first**
   - `memory_retrieve_context` 现在始终先通过 `_pack_ranked_records` 打包 canonical `context_items`。
   - 默认预算复用 important-memory 原语；`top_k` 只作为未显式传 `max_items` 时的条数上限。
   - 返回体新增 `context_items`、`budget_report`、`dropped_candidates`、`evidence_refs`。
   - `core_constraints` / `relevant_rules` / `key_evidence` 保留为分类索引，但不再重复展开正文，避免同一记录正文多处占用上下文预算。
   - 修正短正文记录被 `IMPORTANT_MEMORY_MIN_BODY_CHARS` 误丢弃的问题：只有原正文足够长但预算裁剪过短时才判为 `insufficient_body_budget`。

2. **`memory_compiler.py` 首轮拆分**
   - 新增 `memory_compile_targets.py`：compile target 常量、slug、compiled path 规则。
   - 新增 `memory_compile_render.py`：runtime/task/system/publish Markdown render 与 legacy memory section。
   - 新增 `memory_compile_scoring.py`：record time、sort key、scored records、score summary。
   - `memory_compiler.py` 继续保留对外入口和 snapshot / review / rollback / dao-fa-shu 编译流程，主文件从约 929 行降到约 724 行。

3. **测试与文档**
   - 更新 `test_p3_retrieve_context_accepts_budget_controls`：预算输出现在是正式契约。
   - README 后续计划移除已完成的 1/2，把下一步改为管理 skill 与 compiler 长尾拆分。
   - 设计文档同步 v0.5.8 状态。

### 验证

```powershell
powershell -ExecutionPolicy Bypass -File MCP/Memory/scripts/run_memory_all_tests.ps1
# 218 passed in 15.35s
```

---

## 2026-04-24 (v0.5.7: 默认启用多人安全策略)

> **状态：多人安全策略改为默认生效；旧配置缺 `write_policy` 时也会根据默认多人策略执行 activeContext 用户分流与共享文件 append-only。测试 216 → 218。** 2026-05-10 后该策略进一步收敛为 always-on，不再保留关闭开关。

### 范围

1. **默认多人策略**
   - `DEFAULT_CONFIG_CONTENT` 增加默认 `multi_user` 策略块。
   - 新工作区无需手动配置，即可把 `memory-bank/activeContext.md` 读写重定向到 `memory-bank/activeContext/{user}.md`。

2. **旧配置兼容**
   - `memory_writer._lookup_write_policy` 增加 `multi_user.user_scoped_paths` 兜底。
   - 即使旧 `.ai-memory/config.json` 覆盖了 `guard.targets` 且没有 `write_policy: "user_scoped"`，也会对 `activeContext.md` 执行用户分流。
   - 共享 warm 文件继续通过默认 `multi_user.shared_paths_policy` 强制 overwrite → append 降级。

3. **测试与文档**
   - 新增默认配置启用多人模式测试。
   - 新增旧配置缺 `write_policy` 的 activeContext 分流 / progress append-only 回归测试。
   - README 与设计文档同步为“多人默认启用，不需要配置”口径。

### 验证

```powershell
powershell -ExecutionPolicy Bypass -File MCP/Memory/scripts/run_memory_all_tests.ps1
# 218 passed
```

---

## 2026-04-24 (v0.5.6 P0/P2 follow-up: 锁 fd 异常推担 + disk_full 结构化错误)

> **状态：补三话的二项评估后续：`file_lock` 轮询期间的异步异常（`KeyboardInterrupt` / `SystemExit`）不再泄漏 sidecar fd；`_atomic_write_text` 在 `ENOSPC` / `EDQUOT` 时抛专用 `DiskFullError`，`memory_write` 转成结构化 `error="disk_full"`。+3 测试，213 → 216。**

### 范围

1. **`memory_locks.file_lock` BaseException 守卫**（[memory_locks.py](servers/memory_server/memory_locks.py)）
   - `_acquire_os_lock(fd, timeout)` 原本只 catch `LockTimeoutError` 退出分支 close fd。`KeyboardInterrupt` / `SystemExit` / 其他异步注入异常会跳过清理路径 → fd 泄漏（POSIX 上还会连带 OS 锁未释放，后续获锁者永久阻塞）。
   - 改为 `except BaseException` 守卫，所有退出路径都先 close fd 再重抛。
   - 测试：`test_file_lock_keyboard_interrupt_during_polling_does_not_leak_fd`。Holder 线程我0.8s 锁；waiter 线程 `timeout=5.0` 轮询；中间通过 `ctypes.pythonapi.PyThreadState_SetAsyncExc` 注入 `KeyboardInterrupt`；holder 释放后主线程以 `timeout=1.0` 重新获锁必须 < 0.5s。

2. **`DiskFullError` + `disk_full` 错误码**（[memory_record_io.py](servers/memory_server/memory_record_io.py) + [memory_writer.py](servers/memory_server/memory_writer.py)）
   - 新增 `DiskFullError(OSError)` 与 `_DISK_FULL_ERRNOS = {ENOSPC, EDQUOT, EFBIG}`。
   - `_atomic_write_text` 的 `except OSError` 处理路径：清理 tmp 后，若 errno 在磁盘压力集合，提升为 `DiskFullError`。
   - `memory_writer.memory_write` 额外 catch `DiskFullError`，返回 `error_result("disk_full", ..., errno=...)`。原文件不动、tmp 不泄漏。
   - 测试：注入 `os.replace` 抛 `OSError(ENOSPC, ...)`。`_atomic_write_text` 抛 `DiskFullError`、`memory_write` 返回 `error="disk_full"` + `errno=ENOSPC`、原文件字节比特不变、`.tmp` 零泄漏。
   - 注：最初试图 `patch("os.write")` 未生效——Python C-level `FileIO.write` 不走 Python 级 `os.write`。改用生产路径上真实会被 `ENOSPC` 击中的 `os.replace` 作为注入点。

### 影响

- **取消调试/中断导致的锁永久携带**：之前用户在锁超时期间 Ctrl+C 会造成 sidecar fd 泄漏（同一进程内后续重试锁类似路径可能遭遇难以诊断的垃圾状态）。现在这条路安全。
- **运维可观测**：磁盘几乎被占满的环境调用方能从 `result["error"]` 明确区分「磁盘压力」与「未知 I/O 故障」，授权上层调度压缩 / 告警 / 切换。
- **向后兼容**：`DiskFullError` 是 `OSError` 子类，现有 `except OSError` 代码不需修改。

### 验证

```powershell
.\.venv\Scripts\python.exe -m pytest MCP\Memory\tests -q
# 216 passed in ~15s
```

3 次 soak 跡多进程压力套件 + 本补丁新测试（8 进程压测 + 4 补丁测试）全部 12/12 稳定。

---

## 2026-04-24 (v0.5.6 P1 follow-up: 读路径与路径规范化加固)

> **状态：补齐读路径在多 agent 写入下的瞬态容错；测试由"容忍瞬态"收紧为"零容忍"；213 测试保持全绿，6 次连跑稳定。**

### 范围

落实 v0.5.6 评估遗留的 P1 两项：

1. **`PathManager` 长路径前缀规范化**（[memory_paths.py](servers/memory_server/memory_paths.py)）
   - 新增 `_strip_long_path_prefix(value)` / `_normalize_path(path)`：在 Windows 上把 `\\?\C:\...` / `\\?\UNC\server\share\...` 还原为常规形式后再做 `relative_to` 比较。
   - `_is_within` / `resolve` / `to_repo_relative` 全部改走 `_normalize_path`，避免 `Path.resolve()` 自动加前缀后导致下游 `relative_to(repo_root)` 抛 `ValueError`。
   - `iter_files` 在 `to_repo_relative` 抛 `ValueError`（极端情形：符号链接出仓 / 前缀仍无法对齐）时跳过该条目而非崩溃。

2. **`safe_read_text` 读路径瞬态重试**（[memory_record_io.py](servers/memory_server/memory_record_io.py)）
   - 新增 `safe_read_text(path, *, encoding, errors, max_attempts=20, delay_seconds=0.005)`：bounded retry on `PermissionError` / `FileNotFoundError`，覆盖 Windows writer `os.replace` 短窗口（默认 ≤100ms）。其他 OSError 立即抛出。
   - 接入位置：`memory_search.memory_search`、`memory_reader.memory_get`、`memory_record_index._index_record_rows` 全套（建索引扫描 + 显式索引指定路径分支）。
   - 写路径不需要——它们本来就在 `file_lock` 内，不存在 reader-vs-writer 短窗口。

### 影响

- **生产读 API 不再被并发写击穿**。此前在 Windows 高并发场景下 `memory_search` / `memory_get` 偶发 `PermissionError` 反映为外部错误；现在透明重试，调用方无感。
- **`PathManager` 不再因 `\\?\` 漏出**抛 `ValueError`。pytest tmp 路径较长场景下尤其常见。
- **测试收紧**：`test_mixed_workload_no_db_lock_no_corruption` 的 reader worker 之前用 `transient_path_errors` 计数容忍 `ValueError`，现已改为零容忍——任何异常即测试失败。6 次连跑稳定通过。

### 验证

```powershell
.\.venv\Scripts\python.exe -m pytest MCP\Memory\tests -q
# 213 passed in ~14s
# soak 6×: test_multi_agent_stress.py 全绿
```

---

## 2026-04-24 (v0.5.6: 多 agent 并发严格评估 + 真 bug 修复 + 9 项压力测试)

> **状态：在 v0.5.5 之上严格评估「同一台设备多 agent 同时写入」的真实健壮性，过程中发现 2 个隐性 bug 并修复，新增 9 项多进程压力测试；总计 213 测试全部通过（204 → 213，+9）；6 次连跑无 flaky。**

### 评估结论

**结论：v0.5.4/0.5.5 的并发设计在 9 项严苛真多进程压力下整体成立，但有 2 处隐性缺陷在常规 5 进程基线测试中被掩盖，已修复。**

具体性质：

- **正确性已成立**：高竞争同文件覆写（10 进程 × 20 写）零损坏、append 模式 8 × 25 全部 200 行原样保留、`target_exists` 发布竞争双进程恰好 1 赢、`if_match` 乐观锁 6 × 8 增量零丢失、`LockTimeoutError` 在真实争用下及时返回结构化错误、可重入锁同进程不死锁、混合负载（writer+record_writer+reader 各 3）连续 1.5s 无 DB 锁错误、锁文件 sidecar 复用不泄露。
- **修复 bug A — fd 泄漏**（`memory_locks.py`）：原 `file_lock` 上下文中 `yield` 抛异常时 `else: os.close(fd)` 不执行，仅 `finally` 释放 OS 锁；POSIX 下 `flock` 仍正确释放，但 fd 会泄漏。重写 `try/finally` 嵌套，无条件 close fd。
- **修复 bug B — events.jsonl 多进程丢事件**（`memory_events.py`）：`append_event` 之前用 `msvcrt.locking(fd, LK_LOCK, 1)` 在「`a` 模式打开后的当前位置」锁 1 字节。两个进程 append 时各自的 EOF 偏移不同，锁的字节范围不重叠，**互不阻塞**。压测复现：8 进程 × 60 写入实际只落盘 478/480（丢 2 条，无 torn）。改用统一的 sidecar `file_lock` 包住「rotate + append + flush + fsync」整个临界区。
- **改进 — `os.replace` Windows 共享读重试**（`memory_record_io.py`）：Windows 上 reader（`memory_search` / `read_text`）持有 read 句柄时，writer 的 `os.replace` 会以 `PermissionError` 失败，反映为 `write_failed`。加最多 20×10ms 重试覆盖此短窗口；非 Windows 路径不受影响。

### 落地清单

1. **`memory_locks.py`**：重写 `file_lock` 释放路径。OS lock 获取失败立即 close fd，正常路径 `try: yield ... finally: 释放 reentrance state + OS lock + close fd`，三者均无条件执行。任何异常路径不再泄漏文件描述符。
2. **`memory_events.py`**：导入 `file_lock`；`append_event` 用 `with file_lock(config.repo_root, config.events_file):` 保护「rotate→open(a)→write→flush→fsync」。`_lock_file` / `_unlock_file` 留作向后兼容（带说明 docstring），调用方不再使用。
3. **`memory_record_io.py`**：`_atomic_write_text` 的 `os.replace` 加 PermissionError 短窗口重试（20 次 × 10ms = 200ms 上限），仅命中 Windows reader 共享句柄场景；其他异常立即抛出。新增 `import time`。
4. **`tests/memory_server/test_multi_agent_stress.py`**（**新文件**，9 项测试，全部 `multiprocessing.spawn`）：
   1. `test_high_contention_overwrite_no_corruption` — 10 × 20 同文件覆写：200 次全成功、200 个唯一 request_id、最终文件恰好 1 个完整 worker 签名、payload 64 字节未截断。
   2. `test_concurrent_append_no_torn_lines_no_lost_lines` — 8 × 25 append：200 行 (worker, round) 配对完整保留、无 torn line（每行恰好 1 个匹配）。
   3. `test_publish_target_race_exactly_one_winner` — 2 进程同 record-id 发布：恰好 1 ok、1 `target_exists`，验证 v0.5.4 的锁内 TOCTOU 重检。
   4. `test_lock_timeout_returns_structured_error` — holder 持锁 1.5s，waiter `timeout=0.2s` 在 < 1s 内拿到 `LockTimeoutError`。
   5. `test_reentrance_same_thread_no_deadlock` — 同线程嵌套 `file_lock` + 另一线程等待，事件顺序严格 outer→inner→inner_release→outer_release→waiter。
   6. `test_if_match_optimistic_retry_no_lost_updates` — 6 × 8 增量 RMW + retry-on-conflict：最终计数 = 48，至少 1 次 conflict 证实真争用，处理了 Windows 读取竞态。
   7. `test_events_jsonl_multi_proc_no_torn_lines` — 8 × 60 `append_event`：480 行全部合法 JSON，所有 (worker, round) 配对存在。**这条测试在修复 bug B 前会丢 2 条。**
   8. `test_mixed_workload_no_db_lock_no_corruption` — 3 writer + 3 record_writer + 3 reader 并行 1.5s：零失败（reader 容忍已知 Windows 长路径瞬态 ValueError）、notes.md 非空。
   9. `test_lock_files_are_reused_not_orphaned` — 6 × 5 突发后，`.ai-memory/locks/` ≤ 5 个 sidecar、每个 ≤ 16 字节，证实 sha-keyed 锁路径稳定不累积。
5. **版本号**：`server_descriptions.SERVER_VERSION` → `0.5.6`。

### 验证

```powershell
.\.venv\Scripts\python.exe -m pytest MCP\Memory\tests -q
# 213 passed in ~13s
```

并连跑 6 次新压力套件 + 既有并发/健壮性套件（共 22 测试）全部 22/22 稳定。

---

## 2026-04-25 (v0.5.5: 写入健壮性收尾 — fsync 严格模式 + events.jsonl 落盘 + 写路径统一)

> **状态：4 处写入路径加固完成；新增 8 项 robustness 回归；总计 204 测试全部通过（196 → 204，+8）**

### 范围

承接 v0.5.4 的并发安全工作，把「磁盘故障 / 进程崩溃」维度补齐。所有改动遵守现有契约（默认 best-effort，不破坏既有调用方）。

### 落地清单

1. **新增 config 开关 `mcp.fsync_strict`**（[memory_config.py](servers/memory_server/memory_config.py)）
   - `MemoryConfig.mcp_fsync_strict: bool = False`，默认关。
   - 关：`os.fsync` 失败被吞掉（兼容老行为，应对 tmpfs / WSL bind mount 等不支持 fsync 的卷）。
   - 开：`os.fsync` 失败抛 `OSError`，调用方收到 `error="write_failed"`，磁盘真有问题时不会假装写成功。
2. **`_atomic_write_text` 加固**（[memory_record_io.py](servers/memory_server/memory_record_io.py)）
   - 新签名：`_atomic_write_text(target, content, *, fsync_strict=False)`。
   - rename 之后**追加** `_fsync_parent_dir(target)`：POSIX 上 `os.open(parent, O_RDONLY) + os.fsync + close`，Windows 跳过（rename 元数据由 NTFS 在 `os.replace` 内部刷盘）。原本只 fsync 数据 fd，rename 自身不持久化，crash 后可能整个文件凭空消失。
   - strict 模式同时让 data fd 与 parent dir 的 fsync 错误穿透。
   - 测试覆盖：data fsync 调用次数、parent dir 在 POSIX 被 fsync、strict 模式 OSError 穿透 + tmp 文件清理 + 原文件保留、default 模式 OSError 被吞但 rename 已完成。
3. **`memory_writer.memory_write` 写路径统一**（[memory_writer.py](servers/memory_server/memory_writer.py)）
   - 删除原本的「`config.temp_dir` 内 tmp + `os.replace`」分支（曾有跨卷 `EXDEV` 风险、无 fsync、无 O_EXCL）。
   - 改为直接调用 `_atomic_write_text(resolved, final_content, fsync_strict=config.mcp_fsync_strict)`，与记录写路径共用同一原子助手。
   - 顺手清掉不再使用的 `import os` / `import uuid` / `from pathlib import Path`。
   - 测试覆盖：tmp 文件确实是 `resolved` 的同目录兄弟（不再落 `config.temp_dir`）；strict + 注入 fsync 失败 → 返回 `write_failed` 且原文件零损伤。
4. **`memory_events.append_event` 落盘**（[memory_events.py](servers/memory_server/memory_events.py)）
   - 持锁、写入、flush 之后追加 `os.fsync(handle.fileno())`（best-effort）。
   - 尊重 `mcp.fsync_strict`：开启时 fsync 失败穿透到调用方。原来只 flush，crash 后审计行可能丢失。
   - 测试覆盖：fsync 被调用；strict 模式 OSError 穿透。
5. **`record_usage_stats` 改用原子助手**（[memory_compiler_cache.py](servers/memory_server/memory_compiler_cache.py)）
   - JSON dump 改为 `_atomic_write_text(stats_path, ..., fsync_strict=config.mcp_fsync_strict)`。
   - 既保留了 v0.5.4 加的文件锁（防止丢增量），又获得「strict 模式下 fsync 失败 → 旧 JSON 完整保留」的语义。
   - 测试：种子先写一个 `mem_keep` 计数；strict + 注入 fsync 失败 → 旧 JSON 一字不差留在原地。
6. **`memory_maintenance.tombstones.jsonl` 落盘**（[memory_maintenance.py](servers/memory_server/memory_maintenance.py)）
   - 写入 + flush 后追加 `os.fsync`，strict 模式穿透。

### 测试

新增 [tests/memory_server/test_write_robustness.py](tests/memory_server/test_write_robustness.py)（8 测试）：

```
.\.venv\Scripts\python.exe -m pytest MCP\Memory\tests -q
204 passed in 5.58s
```

（v0.5.4 基线 196 + 8 = 204，无回归。）

### 不变量 / 边界

- 默认 `fsync_strict=False`，与 v0.5.4 行为完全兼容；不需要的部署不必改 config。
- Windows 不做 parent-dir fsync（API 不通用），`os.replace` 元数据由 NTFS 内部处理，与 POSIX 默认行为一致。
- tmp 文件统一落目标同目录，跨卷 `EXDEV` 不再可能。
- `config.temp_dir` 现在仅供备份等其它子系统使用，主写路径已不依赖。

### 已知后续

- v0.5.4 §5 计划剩余项（retrieve_context budget-first / memory_compiler 进一步拆分 / 管理 skill 配套 / P4 LLM / P5 RAG）维持原优先级，本轮不动。
- 若发现 SQLite WAL 与 strict fsync 在某些极端场景（断电 + WAL 还没 checkpoint）的耐久性边界，可再加一道 `PRAGMA wal_checkpoint(FULL)` 的显式触发开关；当前默认 `synchronous=NORMAL` 已足够大多数场景。

---

## 2026-04-25 (v0.5.4: 单机多 agent 单 mcp 服务并发安全 — 跨进程文件锁 + WAL + 乐观锁 + UUID7 请求 ID)

> **状态：6 处写入路径加固完成；新增 5 项多进程并发回归测试；总计 196 测试全部通过（191 → 196，+5）**

### 背景

`setup_mcp.ps1` 把 memory MCP 注册为 `python -m servers.memory_server --root <workspace>` 的 stdio 命令，每个 VS Code 窗口 / Codex 会话都启动**独立**的 Python 进程。多个进程指向同一 `--root` 是真实部署形态，因此「多 agent + 单 mcp 规格 + 同工作区」必须做**跨进程**串行化，纯线程锁不够。

### 落地清单

1. **`memory_locks.py`（新）：跨进程可重入文件锁**
   - 锁文件：`.ai-memory/locks/<sha1(rel_target)>.lock`，sha1 哈希避免 Windows MAX_PATH 与目录分隔符问题；按目标文件粒度，不同文件零争用。
   - POSIX 用 `fcntl.flock(LOCK_EX|LOCK_NB)`，Windows 用 `msvcrt.locking(LK_NBLCK,1)`，均按 50ms 间隔轮询，默认 30s 超时，超时抛 `LockTimeoutError`。
   - 进程内按线程做引用计数：同一线程嵌套 `with file_lock(...)` 不死锁（关键，因为 `memory_write` 内部会调 `backup_files` 再次拿锁）。
2. **`memory_request_id.py`（新）：UUID7 + content_sha**
   - `new_request_id()` 直接调 Python 3.14 的 `uuid.uuid7()`（RFC 9562 时间排序）。
   - `content_sha(text)` = SHA-256 hex，作为乐观锁 ETag。
3. **SQLite WAL（`memory_record_index.py`）**
   - `_connect()` 改为 `connect(path, timeout=30, isolation_level=None)` + `journal_mode=WAL` / `synchronous=NORMAL` / `busy_timeout=30000`。读不阻塞写；多进程写者按 30s 等待自然排队，不再 `database is locked`。
4. **写入路径全面加锁**
   - `memory_writer.memory_write`：`resolved` 解析后整段（read → if_match → budget → backup → 原子写 → 审计 → guard）落入 `with file_lock(config.repo_root, resolved):`；`LockTimeoutError` 转 `error_result("lock_timeout", ...)`。
   - `memory_record_io.write_same_record`：`_atomic_write_text` 调用包入文件锁。
   - `memory_record_io.write_record_to_target`：发布/迁移记录的 `target_exists` 检查 + 原子写 + 旧文件 unlink **整体**锁住 new + old 两侧路径，关闭原本 check-then-write 的 TOCTOU 窗口。
   - `memory_compiler_cache.record_usage_stats`：`usage-stats.json` 的 read-modify-write 进锁，杜绝 `compile_hit_count` 增量丢失。
   - `memory_maintenance.memory_delete_record`：`tombstones.jsonl` append 进锁 + `flush()`，防止多进程同删导致行撕裂。
   - `memory_events`（事件审计）此前已用 `fcntl.flock` / `msvcrt.locking`，本次未改，与新锁体系正交共存。
5. **乐观锁（If-Match / ETag）**
   - `memory_write` 新增可选 `if_match: str | None`：客户端传入读到的 `content_sha`；服务端在锁内重读、比对，不一致则返回 `error="conflict"` + `current_sha` + `expected_sha` + `request_id`。客户端可重读、合并后用新 sha 重试。
   - `if_match=""` 表示「期望文件不存在」（保守创建语义）。
6. **审计 / 返回值新增字段**
   - `memory_write` 返回值与 `memory_write` 审计事件均带 `request_id`（UUID7）+ `new_sha`（写入后 SHA-256），便于多进程链路追踪与下次乐观锁。
   - `request_id` 也可由调用方传入做幂等重试。

### 测试

新增 `tests/memory_server/test_concurrent_writes.py`（5 测试，全部走 `multiprocessing.spawn` 真实多进程）：

- `test_concurrent_overwrites_same_file_serialize_cleanly`：5 进程同写 `notes.md`，最终内容必须是某一个 worker 的完整 banner（不撕裂、`request_id` 全部唯一）。
- `test_concurrent_distinct_records_all_survive`：5 进程各自创建一条独立记录，全部成功且文件落盘（不同 target 不互相阻塞）。
- `test_concurrent_usage_stats_no_lost_increments`：8 进程同记一条记录的 usage，最终 `compile_hit_count == 8`、`compile_targets` 8 项不丢。
- `test_if_match_rejects_stale_precondition`：错误 sha → conflict；正确 sha → ok 且返回新 `new_sha`；用旧 sha 再写 → 再次 conflict。
- `test_request_id_uniqueness_under_contention`：4 进程 × 200 = 800 个 UUID7 全部唯一且单进程内基本时间排序。

### 测试矩阵

```
.\.venv\Scripts\python.exe -m pytest MCP\Memory\tests -q
196 passed in 4.20s
```

（基线 191 + 5 = 196，无回归。）

### 不变量 / 边界

- 锁是「sidecar」文件，不会对目标文件持有独占句柄，因此外部编辑器/工具仍可正常读取目标文件。
- 锁默认 30s 超时；正常 IDE 节奏远低于此。超时返回 `lock_timeout` 而不是死等，调用方可决定是否重试。
- `memory_events` 自有锁与新文件锁互相独立；并发写者的事件追加仍然原子。
- 乐观锁是**可选**契约：不传 `if_match` 时维持原 last-write-wins 语义，向后兼容。

### 已知后续

- 后续若引入 retrieve_context budget-first 重排（计划 §5 项 1）/ compiler 进一步拆分（项 2）/ fsync 严格模式（项 3a），可在此基础上叠加。
- 跨机协作（CRDT 合并、远端 sync）暂不在范围。

---

## 2026-04-24 (v0.5.3: P1 批次重构 — server/compiler/budget 拆分 + 写入加固 + evidence_refs 扩展)

> **状态：6 项 P1 全部落地；`server.py` 1348 → 102 行；新增 4 个独立模块；新增 26 项回归测试；总计 184 测试全部通过（146 → 184，+38）**

### 范围

按 P1 评估顺序执行：P1-C → P1-A → P1-B → P1-F/G → P1-E → P1-D。每项均补充针对性测试，高风险项（写入加固、budget 拆分）含独立回归覆盖。

### 落地清单

1. **P1-C：抽 `memory_frontmatter.py`**
   - 从 `memory_records.py` 提取 7 个 YAML Front Matter 处理函数（`parse_front_matter` / `dump_front_matter` / `parse_record_markdown` / `render_record_markdown` / `_parse_scalar` / `_format_scalar` / `_SCALAR_RE`）。
   - `memory_records.py` 通过 import re-export 维持原符号可见，行数下降。
   - 新增 `tests/memory_server/test_frontmatter_roundtrip.py`（8 测试）：标量/列表往返、CJK Unicode、引号特殊字符、null/bool 字面量、缺头/未闭合的错误路径、re-export 一致性。
2. **P1-A：拆 `server.py`（1348 → 102 行）**
   - 新增 `server_descriptions.py`（`SERVER_NAME` / `SERVER_VERSION` / `_BASE_DESCRIPTIONS`）、`server_tools.py`（`_build_file_roles` / `_build_facade_tools` / `_build_legacy_tools` / `_build_tools`）、`server_dispatch.py`（`_check_required` / `_dispatch_memory_read` / `_dispatch_memory_write` / `_dispatch_memory_context` / `_dispatch_tool`）。
   - `server.py` 改为 thin entry-point + back-compat re-export，11 个测试模块的旧 import 全部继续工作。
   - 新增 `tests/memory_server/test_server_split.py`（11 测试）：facade 默认 3 个工具、admin 模式注册 legacy/admin 工具、`memory_write` 不重复、context schema 中所有 operation 均被 dispatch 覆盖、unknown tool 返回 `unknown_tool` 错误等。（该 admin 模式已在 2026-05-10 移除。）
3. **P1-B：抽 `memory_compiler_cache.py`**
   - 提取 `load_compile_cache_entries` / `find_compile_cache_entry` / `record_usage_stats` / `get_record_last_used_at`（即 `.ai-memory/compile-cache/*.json` 与 `.ai-memory/usage-stats.json` 的纯文件 I/O 层）。
   - `memory_compiler.py` 通过 `_record_usage_stats = record_usage_stats` 别名保持原内部调用，外部 5 个测试 import 通过 re-export 不变。
4. **P1-F/G：写入安全加固（`memory_record_io.py`）**
   - 新增 `_atomic_write_text(target, content)`：tmp 文件用 `O_CREAT | O_EXCL | O_WRONLY` 创建（防止同 tick 内并发 tmp 冲撞）；`fh.flush()` + `os.fsync()`（best-effort）后再 `os.replace`；tmp 与 target 强制同目录避免跨卷。
   - `write_same_record` 与 `write_record_to_target` 全部改用此助手，去除原本散落的 try/except 临时路径逻辑。
   - `write_record_to_target` 在 `same_path == False` 时新增「目标已存在则拒绝覆盖」防护，返回 `target_exists` 错误并保留原 candidate 文件，杜绝悄默 clobber 已发布记录的可能。
   - 新增 `tests/memory_server/test_record_io_hardening.py`（6 测试）：成功路径、覆盖既有文件、自动建父目录、O_EXCL 拒绝（注入固定 uuid + 预占 tmp 文件触发 `OSError`，且不破坏他人文件）、`target_exists` 拒绝路径、`write_same_record` 往返。
5. **P1-E：`important_memories.evidence_refs` 扩展**
   - 之前只聚合 `source_refs`。现追加 `related_artifact_ids`、记录 `path`、记录 `id`，供消费方做完整 provenance 审计。
   - 同步在 `_build_memory_item` 输出加上 `related_artifact_ids` 字段。
   - 新增 `tests/memory_server/test_evidence_refs.py`：写入 candidate → validate → publish 后断言 evidence_refs 同时包含 `source_refs` 元素 / artifact_id / 文件路径 / 记录 ID。
6. **P1-D：抽 `memory_budget.py`（共享 budget 原语）**
   - 提取 `IMPORTANT_MEMORY_DEFAULT_MAX_*` 常量、`validate_budget_inputs`、`fit_text_to_budget`。`memory_retrieval.py` 通过 `from .memory_budget import ... as _validate_budget_inputs / _fit_text_to_budget` 维持本地下划线别名，所有现有调用零修改。
   - 注意：未变更 `memory_retrieve_context` 公共返回结构（`test_p3_retrieve_context_accepts_budget_controls` 仍要求其不暴露 `budget_report` / `important_memories` / `dropped_candidates`），保持向后兼容。
   - 新增 `tests/memory_server/test_budget_primitives.py`（11 测试）：None/0/-1 边界、空输入、字符截断、token 截断、常量合理性、向后兼容别名 `is` 检查。

### 验证

- `C:\Work\GIT\ToolTest\.venv\Scripts\python.exe -m pytest MCP\Memory\tests\memory_server -q` → **191 passed in 2.41s**（146 → 191，+45）。
- 其中新增 `tests/memory_server/test_mcp_protocol.py`（7 测试）：通过真实 `mcp.server.Server.request_handlers` 调度 `ListToolsRequest` / `CallToolRequest`，端到端覆盖 facade 默认 3 工具、admin 模式 23 工具、memory_read/write/context 调用、unknown_tool 错误信封、admin 模式下 legacy `memory_get` 可达。
- `SERVER_VERSION` 已升至 `"0.5.3"`。
- 文件与符号映射：

| 旧位置 | 新位置 | 备注 |
|---|---|---|
| `memory_records.py` (7 funcs) | `memory_frontmatter.py` | re-export 别名保留 |
| `server.py` 大块逻辑 | `server_descriptions.py` / `server_tools.py` / `server_dispatch.py` | `server.py` ≈ 102 行 thin shim |
| `memory_compiler.py` cache+usage | `memory_compiler_cache.py` | re-export 别名保留 |
| `memory_retrieval.py` budget 工具 | `memory_budget.py` | re-export 别名保留 |

### 影响范围

- 公共 facade（`memory_read` / `memory_write` / `memory_context`）API 与返回结构未变。
- `MCP\Memory\servers\memory_server\server.py.bak` 已临时备份后清理。
- 模块依赖更清晰：`memory_budget` 仅依赖 `memory_result + token_estimator`；`memory_compiler_cache` 仅依赖 `memory_config + memory_corpus`；`server_*` 三件套形成 facade/dispatch/descriptions 边界。
- `_record_usage_stats` 在 compiler 内仍以 `_` 私有别名存在；外部仅 `find_compile_cache_entry` / `load_compile_cache_entries` / `get_record_last_used_at` 是公共接口。

### 后续待办

- `memory_compiler.py` 仍约 950 行（render / targets / scoring）尚未细拆，可作为 v0.5.4 候选；本轮风险/收益不划算未动。
- `memory_retrieve_context` 是否要在新版本暴露 `budget_report` 需先与现有 `test_p3_retrieve_context_accepts_budget_controls` 契约协商，列入 v0.6 设计议题。
- `_atomic_write_text` 的 `os.fsync` 在某些 Windows 卷会抛 `OSError`，目前 best-effort swallow；后续若发现实际生产场景中需要严格持久性，可加 config 开关。

---

## 2026-04-24 (v0.5.2: user_private 作者隔离 + memory_corpus 解耦)

> **状态：1 处 P0 安全缺陷修复；P1-3 解耦重构完成；新增 1 个回归测试；总计 146 测试全部通过**

### 背景

按 `MemorySystemDesignDocument.md` §0.3 / §15.5 / §15.6 收敛 v0.5.1 之后的剩余 P0/P1 项：

- P0-2：`schema v2` 引入 `user_private` 作用域，但 `memory_compiler._matches_filter` 与 `memory_retrieval._collect_records` 仍只对 V1 `personal` 执行作者隔离，导致 `user_private` 记录会被其他用户在 `memory_retrieve_context` / `important_memories` 中读到。
- P1-3：`memory_retrieval` 反向依赖 `memory_compiler` 的 `CompilableRecord` / `_compact_body` / `_iter_records` 等私有符号，违反层次方向。

### 修复与重构

1. **`user_private` 作者隔离（P0-2）**
   - `memory_compiler._matches_filter`：`scope == "personal"` → `scope in {"personal", "user_private"}`。
   - `memory_retrieval._collect_records`：抽出 `private_scopes = {"personal", "user_private"}` 并复用同一过滤分支。
   - 两条路径同步修复，确保 retrieval / compiler / important_memories 三个出口都不会越权。
2. **`memory_corpus.py` 抽离（P1-3）**
   - 新增 `MCP/Memory/servers/memory_server/memory_corpus.py`：包含 `CompilableRecord` 数据类、`first_heading` / `body_without_title` / `markdown_sections` / `clip_text` / `compact_body` / `iter_compilable_records` 与 `COMPACT_SECTION_PRIORITY` / `COMPACT_BODY_CHAR_LIMIT` 常量。
   - `memory_compiler.py`：删除本地实现，改为从 `memory_corpus` 导入并保留 `_first_heading` / `_compact_body` 等下划线别名供原有内部调用方过渡使用；`_iter_records` 简化为 `return iter_compilable_records(config)`。
   - `memory_retrieval.py`：`from .memory_compiler import ...` 改为 `from .memory_corpus import CompilableRecord, compact_body as _compact_body, iter_compilable_records as _iter_records`，不再依赖 compiler 私有符号。
3. **回归测试**
   - `tests/memory_server/test_p3_completion.py` 新增 `test_p3_private_scopes_isolate_authors_in_retrieval`：alice 分别写入 `personal` 和 `user_private` 各一条，bob 通过 `memory_retrieve_context` / `memory_get_important_memories` 都无法读到，alice 自己仍可读到。
   - 注意：`tags` 必须使用受控词表（如 `mcp`）；`pipeline` 等会被 `invalid_input` 拒绝。

### 验证

- `C:\Work\GIT\ToolTest\.venv\Scripts\python.exe -m pytest MCP\Memory\tests\memory_server -q` → **146 passed in 2.29s**（145 → 146）。
- 注意：`MCP/Memory/.venv` 不带 `pytest`，应使用仓库根 `.venv` 或 `scripts/run_memory_all_tests.ps1`（脚本会自动回退）。

### 影响范围

- 公共 facade（`memory_read` / `memory_write` / `memory_context`）行为未变。
- `memory_compiler` 仍对外保留 `_first_heading` / `_compact_body` / `CompilableRecord` 名称，可向后兼容外部潜在调用。
- 后续 P0-1 / P0-2（拆 `memory_compiler.py` / `server.py`）保持原计划，本次未触及。

---

## 2026-04-23 (健壮性深度测试 + 两处加固)

> **状态：新增 10 个健壮性测试；2 处真实缺陷已修复；总计 143 测试全部通过**

### 背景

P0-3 完成后对系统做一轮深度健壮性评估，覆盖原 133 测试矩阵未触达的边界：原子写并发、崩溃残留 `.tmp`、损坏 Front Matter、SQLite 索引文件损坏、路径攻击变体（NUL 字节 / 绝对路径 / `..` 链 / Windows 盘符）、Unicode 往返、全局预算上限、`memory_write_record` 拒绝非法元数据。

### 发现并修复的真实缺陷

1. **路径含 NUL 字节会让 `pathlib` 抛 `ValueError`，越过安全层。**
   - 修复：`memory_paths.PathManager.resolve` 在最前面拒绝 `\x00`，统一抛 `PathSecurityError`，调用方得到 `path_not_allowed` 而不是 500-级异常。
2. **SQLite 索引文件损坏后 `memory_rebuild_index` 永久失败，无法自愈。**
   - 修复：新增 `_is_index_healthy`（基于 `PRAGMA integrity_check`）与 `_reset_corrupted_index`（含 GC + 重试，处理 Windows 文件锁），rebuild 前自动检测并清掉坏 db / WAL / SHM 副本。

### 新增测试（`tests/memory_server/test_robustness_deep.py`）

- 并发 overwrite 不产生半截/交错文件（成功者写入完整 payload，失败者得到 `write_failed`）。
- 残留 `.tmp` 兄弟文件不阻塞下一次写入。
- `iter_parsed_records` 跳过坏 YAML / 无 Front Matter 的 markdown，并在 stats 中可观测。
- `find_record_by_id` 在空 corpus 下返回 `not_found`。
- 损坏 `search.db` 后 rebuild 自愈、search 可命中。
- 6 种路径攻击（含 NUL）全部被 `path_not_allowed` 拒绝。
- CJK + emoji + RTL 字符 write→read 字节级保持。
- 超出全局预算（5000 chars）被预先拒绝。
- 非法 `record_kind` 被拒绝且不留下任何文件。

### 验证

`cd MCP/Memory; ..\..\.venv\Scripts\python.exe -m pytest tests\memory_server -q` -> **143 passed**。

---

## 2026-04-23 (P0-3 重构：抽取 record IO 公共层)

> **状态：已实现，测试 133 全部通过**

### 背景

随着 P3 完成，22 个模块里有 4 处独立实现的 `_iter_records` / `_find_record` / `_refresh_index_if_exists` / `_write_record_to_target`（散落在 `memory_governance.py`、`memory_lineage.py`、`memory_maintenance.py`、`memory_compiler.py`），签名不一致、容易漂移。本轮把这一层抽出公共模块，作为 P0 重构的第一步。

### 改动

- 新增 `servers/memory_server/memory_record_io.py`：
  - `ParsedRecord` dataclass。
  - `iter_record_files(config)`：返回 `memory-bank/` 下未编译的 markdown 文件。
  - `iter_parsed_records(config)`：解析记录并附带 scan stats。
  - `find_record_by_id(config, record_id)`：返回 4 元组或 `error_result`。
  - `refresh_index_if_exists(config, path)`：FTS 索引存在则增量刷新，best-effort。
  - `write_same_record(...)`：原子重写同路径记录（用于 lineage facet 追加）。
  - `write_record_to_target(...)`：临时文件 + `os.replace` + 旧路径清理，用于 governance 状态迁移。
- `memory_governance.py`：删除本地 `_iter_record_paths` / `_find_record` / `_write_record_to_target` / `_refresh_index_if_exists`，改为从 `memory_record_io` 导入；`_other_records` 改用 `iter_parsed_records`。
- `memory_lineage.py`：删除本地 `_iter_records` / `_find_record` / `_write_same_record` / `_refresh_index_if_exists`；保留薄壳 `_iter_records` 适配旧 4 元组用法。
- `memory_maintenance.py`：删除本地 `_iter_record_files` / `_find_record`，改为公共层导入。
- `memory_compiler.py`：`_iter_records` 改为在 `iter_parsed_records` 之上做 `CompilableRecord` 投影，保留对外签名 `(records, stats)`。
- `memory_retrieval.py`：仍通过 `memory_compiler` 的 `CompilableRecord` 接口工作，间接受益。

### 受益

- 4 处重复实现 → 1 处公共实现，行为差异从此固定（特别是 `os.replace` 原子写入和 PathSecurityError 错误码）。
- 单文件最大行数：`memory_compiler.py` 1188→1158，`memory_governance.py` 326→215，`memory_lineage.py` 385→306。
- MCP 对外接口零变更，默认仍只暴露 3 个 facade。
- 测试零修改通过：`memory_server` 全量 133 passed。

### 后续重构计划（已识别，未实施）

- P0-1 拆 `memory_compiler.py`（cache / render / targets / 入口分文件）。
- P0-2 拆 `server.py`（schema / dispatch / admin / main 分文件）。
- P1-4 把 `CompilableRecord` 与 `_compact_body` 上提到独立 corpus 模块，消除 `memory_retrieval` 对 compiler 私有函数的反向依赖。
- P1-5 把 `parse_front_matter` / `dump_front_matter` 抽到 `memory_frontmatter.py`，便于未来替换实现。

### 验证

```powershell
cd MCP/Memory; ..\..\.venv\Scripts\python.exe -m pytest tests\memory_server -q
# 133 passed in 2.02s
```

## 2026-04-23 (P3 completion：snapshots / scoring / retrieve context)

> **状态：已实现，测试 133 全部通过**

### 背景

上一轮已完成 schema v2、evidence/lineage 和 conflict listing。本轮收尾 P3B/P3C/P3D 剩余项，在不增加默认 MCP tool 数量的前提下，把时间快照、deterministic scoring、review/rollback 分层视图、snapshot compare 和 context retrieval v1 接入 `memory_context` / `memory_compile`。

### 改动

- 新增 `memory_scoring.py`：
  - `score_governance()`、`score_usage()`、`score_impact()`、`score_novelty()`、`score_conflict()`、`score_decay()`。
  - scorer 输出 deterministic `importance_score` 和 effective memory tier，不回写源记录。
- 扩展 `memory_compile`：
  - 新增 `daily_snapshot`、`weekly_snapshot`、`monthly_snapshot`。
  - 新增 `review_queue`、`rollback_context`、`dao_digest`、`fa_digest`、`shu_digest`。
  - compile cache 记录 snapshot id、窗口、派生 snapshot ids 和 included record ids。
- 新增 `memory_compare_snapshots()`：
  - 基于 compile cache 对比 added / removed / persisted record ids。
- 新增 `memory_retrieval.py`：
  - `memory_retrieve_context()` 固定执行 scope filter -> time window filter -> facet filter -> metadata/FTS recall -> importance rerank -> context assembly。
  - 输出 `core_constraints`、`relevant_rules`、`recent_snapshots`、`key_evidence`、`open_conflicts`、`next_steps`。
- `memory_context` 新增：
  - `operation="compare_snapshots"`
  - `operation="retrieve_context"`
- 默认 MCP facade 仍保持 3 个工具。

### 验证

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests\memory_server -q
# 133 passed
```

## 2026-04-23 (P3D conflict listing：memory_context list_conflicts)

> **状态：已实现，测试 130 全部通过**

### 背景

P3D 计划把检索升级为上下文装配，其中 open conflicts 是后续 `memory_retrieve_context` 和 review queue 的关键输入。本轮不新增 MCP tool，保持默认 3 个 facade，只在 `memory_context` 中增加一个 operation。

### 改动

- 新增 `memory_list_conflicts(config, include_resolved=False)`：
  - 扫描 Markdown + Front Matter 真源记录。
  - 读取 `conflicts_with` 谱系字段。
  - 返回冲突双方 record summary、缺失目标、resolved 状态和统计信息。
  - 默认隐藏已 archived / degraded 的 resolved 冲突，可用 `include_resolved=true` 查看。
- `memory_context` 新增 `operation="list_conflicts"`。
- 不新增 MCP tool，默认对外仍只有 `memory_read`、`memory_write`、`memory_context`。
- 更新 README 与设计文档 P3D 状态。

### 验证

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests\memory_server -q
# 130 passed
```

## 2026-04-23 (MCP facade 收敛实现：默认 3 工具)

> **状态：已实现，测试 127 全部通过**

### 背景

按最新开发计划，优先把 MCP 对外工具列表从开发期的细粒度工具集收敛为少量 facade，降低 AI 客户端误选低频管理工具的概率。

### 改动

- 默认 MCP tool list 只暴露 3 个工具：
  - `memory_read`
  - `memory_write`
  - `memory_context`
- `memory_read` 支持：
  - `operation="get"`
  - `operation="search"`
  - `operation="search_records"`
  - `operation="runtime_digest"`
- `memory_write` 支持：
  - `operation="file"`，并作为默认操作兼容旧 `memory_write(path, content, ...)` 调用
  - `operation="record"`
  - `operation="observation"`
  - `operation="link_artifact"`
- `memory_context` 支持：
  - `operation="compile"`
  - `operation="runtime_digest"`
  - `operation="trace_lineage"`
- 当时新增过 admin MCP 扩展配置，用于开发调试或纯 MCP 客户端兼容；该配置已在 2026-05-10 移除。
- 默认只暴露 3 个 facade。（当前已收敛为 `memory_read` / `memory_write` 两个工具。）
- 旧细粒度工具的 `_dispatch_tool` 兼容入口仍保留，内部函数未删除。

### 验证

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests\memory_server -q
# 127 passed
```

## 2026-04-23 (开发计划调整：MCP facade 收敛列为最高优先级)

> **状态：文档计划更新，无代码变更**

### 背景

当前 Memory MCP 为了开发验证和测试覆盖，已经暴露到 18/21 个细粒度工具。这个形态适合开发期调试，但对 AI 客户端不够友好，也会把 guard、backup、migrate、governance、delete 等低频或高风险管理动作直接放到默认工具列表里。

### 决策

后续最高优先级改为先收敛 MCP 对外接口：

- 默认 MCP 只暴露 3 个 facade tools：
  - `memory_read`
  - `memory_write`
  - `memory_context`
- 现有细粒度能力继续作为内部 Python 函数保留。
- 管理动作迁移到 CLI / scripts / skill：
  - guard / backup / compact
  - index rebuild/update
  - health / migrate
  - validate / publish / archive / delete
  - snapshot rebuild / compare
  - conflict review / promote / degrade
- 后续补管理 skill：
  - `memory-admin`
  - `memory-governance`
  - `memory-snapshot-review`
- 当时计划增加配置开关用于兼容开发期和纯 MCP 客户端；该方向已在 2026-05-10 改为 CLI-only。

### 文档

- 已更新 `MemorySystemDesignDocument.md` 的 MCP 接口设计、落地顺序建议和最终优先级。
- 已更新 `README.md` 的后续开发计划。

## 2026-04-23 (v0.5.0 P3A 继续推进：observation / artifact / lineage 工具)

> **状态：已实现，测试 124 全部通过**

### 背景

上一轮已完成 schema v2 字段、扩展 kind/scope、tier/cognitive/facet 索引基础。本轮继续沿 P3A/P3D 交界处推进，让 schema v2 不只是一组可写字段，而是具备基础证据写入、artifact 关联和谱系追踪入口。

### 改动

- 新增 `memory_lineage.py`。
- 新增 `memory_record_observation`：
  - 固定写入 `schema_version="2.0"`、`record_kind="observation"`、`scope="session"`、`status="raw"`。
  - 默认 `memory_tier="hot"`、`cognitive_level="shu"`。
  - 支持 artifact / 工程 facet 字段。
- 新增 `memory_link_artifact`：
  - 给既有记录追加 `related_artifact_ids`、`asset_paths`、`map_names`、`plugin_names`、`module_names`、`class_names`、`blueprint_paths`、`system_area`。
  - 自动升级记录为 schema v2。
  - 如果 `.ai-memory/search.db` 已存在，会增量刷新对应记录索引。
- 新增 `memory_trace_lineage`：
  - 从指定记录开始追踪 `derived_from_record_ids`、`supersedes`、`conflicts_with`。
  - 返回 `nodes`、`edges`、`missing` 和统计信息。
- MCP 工具数从 18 增至 21。
- README 与设计文档已同步 P3A 当前状态。

### 验证

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests\memory_server -q
# 124 passed
```

## 2026-04-23 (v0.5.0 P3A 启动：schema v2 记录模型基础)

> **状态：已实现，测试 119 全部通过**

### 背景

按新的 P3 结构升级计划，优先从不依赖 LLM 的记录模型扩展开始。目标是让现有 Markdown + Front Matter 真源先能承载时间、分层、谱系和工程 facet 信息，并让 SQLite 派生索引可以检索这些结构化字段。

### 改动

- `memory_write_record` 支持 `schema_version="2.0"`，并在使用 P3 record kind、scope 或 v2 字段时自动升级为 schema v2。
- 扩展 `record_kind`：
  - `observation`
  - `artifact_ref`
  - `incident`
  - `decision`
  - `procedure`
  - `snapshot_daily`
  - `snapshot_weekly`
  - `snapshot_monthly`
- 扩展 `scope`：
  - `session`
  - `user_private`
  - `task_or_branch`
  - `project_shared`
  - `org_shared`
- 新增 v2 metadata 字段：
  - 时间：`occurred_at`、`valid_from`、`valid_to`
  - 分层：`memory_tier`、`cognitive_level`
  - 谱系：`derived_from_record_ids`、`derived_from_snapshot_ids`、`derived_from_revision_ids`、`supersedes`、`conflicts_with`
  - 工程 facet：`related_artifact_ids`、`asset_paths`、`map_names`、`plugin_names`、`module_names`、`class_names`、`blueprint_paths`、`system_area`
  - 评分预留：`importance_score`
- `memory_record_index.py` 将 schema v2 字段写入 SQLite metadata 表，并把 tier、cognitive level、system area 和 facets 纳入 FTS 搜索文本。
- MCP `memory_write_record` 工具 schema 同步暴露 P3A 新字段。

### 兼容性

- 默认写入仍保持 schema `1.0`，旧记录和旧调用方不受影响。
- 如果显式传 `schema_version="1.0"` 同时使用 P3 kind / scope / v2 字段，会返回 `invalid_input`，避免 silently 丢字段。
- `memory-bank/shared/` 现在承接 `project_shared` / `org_shared` 记录；个人、任务和会话级记录仍进入 `memory-bank/people/{user}/`。

### 验证

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests\memory_server -q
# 119 passed
```

## 2026-04-23 (开发计划重排：P3 结构升级优先)

> **状态：文档规划更新，无代码变更**

### 背景

此前 README / 设计文档将后续方向写成 P3 LLM 增强、P4 本地 RAG / 向量召回。结合后续评估，当前系统真正更急的不是先接 LLM，而是把现有 Markdown + Front Matter 真源、SQLite 派生索引、deterministic compile、governance 主干升级为可支撑多人联合项目的结构化记忆编译器。

### 调整

- 新 P3：结构升级与联合项目记忆能力。
  - schema v2
  - daily / weekly / monthly snapshot
  - lineage / derived_from / supersedes / conflicts_with
  - memory_tier: hot / warm / cold / fossil
  - cognitive_level: dao / fa / shu
  - importance scoring
  - facet / artifact / scope 扩展
  - `memory_retrieve_context` 上下文装配
- 新 P4：LLM 软增强。
  - query rewrite
  - tag / facet 推荐
  - candidate draft
  - snapshot narrative
  - conflict explanation
- 新 P5：本地 RAG / 向量补召回。
  - 仅做语义模糊召回、长尾别名补召回、低关键词命中补召回。

### 约束

- 真源仍然是 Markdown + Front Matter。
- SQLite / FTS / CJK n-gram 仍然只是派生索引。
- deterministic compile 仍然是主编译链路。
- governance 仍然是正式发布入口。
- 无 LLM 必须完整可运行。
- LLM 和向量检索均不得替代真源、发布权限或 deterministic compile。

### 维护

- 已运行 `memory_compact(policy=warm_context)` 压缩 `memory-bank/activeContext.md`：约 8974 chars -> 2576 chars。

## 2026-04-22 (v0.4.2 编译默认 compact 输出)

> **状态：已实现，测试 115 全部通过**

### 背景

实测多段记忆编译后发现，旧 `runtime_digest` 能按 task/status/tag 过滤记录，但默认会把匹配记录的详细 metadata 和完整正文都写入编译产物。它能减少“不相关记录”的上下文，却不能减少“相关记录本身”的上下文体积。

### 改动

- `memory_compile` 新增 `body_mode` 参数。
- 默认 `body_mode="compact"`：
  - 每条记录只输出 `id`、`source`、`status`。
  - 不逐条输出 `author`、`task_id`、`branch`、`tags` 等筛选 metadata。
  - 优先抽取 `Decision`、`Expected Behavior`、`Acceptance Checks`、`Next Step(s)`、`Notes`、`Details` 等关键段落。
  - 没有关键段落时截取正文开头。
- 保留 `body_mode="full"`，用于旧版完整记录渲染和调试。
- MCP `memory_compile` schema 暴露 `body_mode`，并在 cache manifest / audit event / tool result 中记录实际模式。
- 修复 `Resolve-MemoryTestPython.ps1`：候选 Python 缺少 pytest 时继续尝试下一个环境，避免官方测试脚本在回退到仓库 `.venv` 前中断。

### 验证

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests\memory_server -q
# 115 passed
```

## 2026-04-21 (后续开发计划：LLM 与 RAG 优先级)

> **状态：历史规划；已被 2026-04-23 的 P3 结构升级路线后移和替代**

### 结论

- P3 仍以 LLM 增强为优先方向：分类、tag 推荐、候选生成、冲突解释和摘要提炼。
- 不再把中文分词增强列为独立计划；当前继续保留已实现的 CJK bigram/trigram 作为中文检索兜底。
- 中文模糊查询优先通过 LLM query rewrite / metadata hint 增强，再交给当前 FTS 检索。
- 本地 RAG / 向量召回排在 LLM 增强之后，仅用于语义模糊召回、相似记录推荐和候选冲突提示。
- RAG 索引必须是 `.ai-memory/` 下的派生产物，可删除重建，不能替代 Markdown + Front Matter 真源。

### 约束

- LLM 和 RAG 都不能直接发布系统记忆。
- `candidate -> validated -> published` 治理流不变。
- 无 LLM、无向量模型时，基础写入、FTS 检索、编译和治理必须继续可用。

## 2026-04-21 (v0.4.1 严肃评审 P0 修复轮)

> **状态：已实现，向后兼容；测试 106 → 113 全部通过**

### 背景

针对 vNext 设计目标做了一轮严肃评审，定位到 7 个会在多人 / 多客户端 / 真实文本场景下踩雷的正确性与设计风险，本次集中修复。

### 修复清单

1. **FTS5 MATCH 查询语法**（`memory_record_index.py`）
   - 旧实现 `f"search_text : {query_text}"` 同时存在两个错误：
     1) FTS5 列限定符不允许空格；
     2) 用户输入直接拼接，含 `-` / `OR` / `"` / `:` 时会触发 `OperationalError`。
   - 新实现 `build_fts5_match_query()`：复用索引侧 tokenizer，对每个 token 加双引号包成 phrase，去掉列限定符（`search_text` 仍是覆盖最广的索引列，所有列 MATCH 等价或更宽）。
   - 新增 `_escape_fts5_token` / `build_fts5_match_query` 公共函数，便于复用与单测。

2. **编译器禁止反向修改源记录**（`memory_compiler.py`）
   - 旧 `_mark_records_used` 会把 `last_used_at` 写回真源 `.md`，违反"编译产物可重建、源是真源"的设计原则，污染 Git diff，且不刷新 FTS 索引。
   - 新实现 `_record_usage_stats` 把使用情况写到 `.ai-memory/usage-stats.json`（与 compile-cache 同级，可整体删除重建）。
   - 新增 `get_record_last_used_at(config, record_id)` 公共读 API。

3. **记录写入原子化**（`memory_records.py`）
   - 改用 `os.open(path, O_CREAT | O_EXCL | O_WRONLY)` 创建记录，关闭 `already_exists` 检查与 `write_text` 之间的 TOCTOU 窗口；并发 MCP 客户端碰到同 id 时一方失败而非互相覆盖。
   - 写入失败时回滚被 `O_EXCL` 创建出来的空文件，避免留下半成品。

4. **治理状态迁移原子化**（`memory_governance.py`）
   - 旧 `write_text(new) → unlink(old)` 路径在异常时可能留下两份或丢失记录。
   - 新实现：临时文件 `tmp` → `os.replace(tmp, new)` → 仅在新路径就绪后删除旧路径；任一步失败都不会破坏既有真源。

5. **`memory_write` 用户标签注入策略**（`memory_writer.py` + `server.py`）
   - 旧实现无差别向所有写入末尾注入 `<!-- last overwritten by ... -->`；写入 JSON / YAML / TOML / 源码会破坏文件。
   - 新实现新增 `inject_user_tag` 参数：默认按扩展名自动判断（仅 `.md` / `.markdown` 注入），`True` 强制开启，`False` 强制关闭。
   - MCP `memory_write` 工具 schema 同步暴露 `inject_user_tag`。

6. **`resolve_user_path` 自动迁移加归属警告**（`memory_paths.py`）
   - 旧逻辑会把多人共写的旧 `activeContext.md` 直接复制到第一个登场的用户分区文件，造成事实归属错误。
   - 新逻辑在迁移内容前插入 `<!-- migrated-from-shared: ... attribution to '{user}' is NOT verified ... -->` banner，提示人工核对。

7. **审计日志 rotation**（`memory_events.py`）
   - 新增 `_rotate_events_if_needed()`：`events.jsonl` 超过阈值（默认 5 MB，可由 `MEMORY_MCP_EVENTS_MAX_BYTES` 覆盖）时按时间戳重命名为 `events.jsonl.YYYYMMDDTHHMMSS`，仅保留最新 N 份归档（默认 5，可由 `MEMORY_MCP_EVENTS_MAX_ARCHIVES` 覆盖）。
   - rotation 过程中的所有 OS 错误都被吞掉，绝不阻塞审计写入路径。

8. **去除默认 tag 词表的双源**（`memory_config.py` + `memory_records.py`）
   - 把内置默认 tag 集合提取为 `memory_config.DEFAULT_ALLOWED_TAGS` 单一定义。
   - `memory_records.ALLOWED_TAGS` 改为从 `memory_config` 引入；`DEFAULT_CONFIG_CONTENT` 也复用同一份列表。后续增减 tag 只改一处。

### 新增测试

- `test_record_index.py::test_search_records_handles_fts5_reserved_characters` — 覆盖 `texture-size` / `round-trip` / `OR fallback` / `"quoted phrase"` 四种触发旧 syntax error 的查询。
- `test_record_index.py::test_search_records_empty_after_normalization_returns_no_hits` — 全标点查询不再抛错。
- `test_record_index.py::test_search_records_query_plan_uses_sqlite_fts_index` — 同步到新查询表达式。
- `test_runtime_maintenance.py::test_compile_updates_last_used_at_and_writes_cache_manifest` — 改为断言"源记录未被改动 + `usage-stats.json` 写入 + 二次编译不改源 mtime"。
- `test_governance.py::test_validate_candidate_does_not_leave_two_copies` — 状态迁移后无残留 staging 文件。
- `test_write.py::test_inject_user_tag_default_skips_non_markdown` — `.json` 写入不被注释污染，且仍是有效 JSON。
- `test_write.py::test_inject_user_tag_default_marks_markdown` — `.md` 默认仍带尾注。
- `test_write.py::test_inject_user_tag_can_be_force_disabled` — 显式关闭注入。
- `test_multi_user.py::test_user_scoped_migration_includes_attribution_banner` — 自动迁移携带归属警告 banner。

### 验证结果

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests\memory_server -q
# 113 passed

.\scripts\run_memory_all_tests.ps1
# 113 passed
```

### 行为兼容性说明

- MCP 工具数量不变（仍 18 个）。
- 默认场景下：旧的 `.md` 写入仍带用户尾注；旧的搜索查询仍可命中；治理迁移路径不变；候选发布规则不变。
- 行为差异需调用方注意：
  - `memory_compile` 不再修改源记录的 `last_used_at`；改读 `.ai-memory/usage-stats.json` 或 `get_record_last_used_at()`。
  - `memory_write` 写入非 `.md` 路径默认不再注入 HTML 注释；如需保留旧行为，显式传 `inject_user_tag=true`。
  - 多人模式下首次自动迁移会带 banner，下游脚本读取 `activeContext/{user}.md` 时若期望"纯净文本"需自行剥离 banner 行。

---

## 2026-04-21 (P0/P1/P2 完成: 治理质量、运行时维护、配置化)

> **状态：已实现，不包含 P3 LLM 增强**

### 背景

在记录写入、检索、编译和基础治理闭环之后，继续补齐非 LLM 的 P0/P1/P2 能力：治理质量、运行时使用追踪、维护健康检查、schema 迁移、增量索引和删除策略。

### 本次实现

#### P0 治理质量

- `governance.min_confidence`：候选验证时检查最低可信度。
- `governance.require_source_refs_for`：指定候选类型必须有 `source_refs`。
- `governance.reviewers`：限制 `validated_by`。
- `governance.publish_owners`：限制 `published_by`。
- 验证时检查重复标题/正文。
- 发布时检查与 existing shared published system rule 标题相同但正文不同的冲突。

#### P1 编译与运行时

- `memory_compile` 会更新 included records 的 `last_used_at`。
- `memory_compile` 会写入 `.ai-memory/compile-cache/{target...}.json` manifest。
- `runtime_digest` 会包含旧文件摘要 section：`activeContext.md` / `progress.md`，保持与旧文件级记忆兼容。

#### P2 配置化与维护

- `tag_schema.allowed_tags` / `tag_schema.version` 配置化。
- `memory_health_check`：检查缺失 metadata、未知 tag、缺失 `search.db`。
- `memory_migrate_records`：迁移记录 `schema_version`，写入 `schema_migrated_from`。
- `memory_update_index`：对指定记录路径增量更新 SQLite FTS。
- `memory_delete_record`：只允许删除 archived 记录，并写入 `.ai-memory/tombstones.jsonl`。
- MCP 工具列表从 14 个扩展为 18 个。
- 更新 `README.md` 与 `MemorySystemDesignDocument.md`。

#### 健壮性收口

- `memory_compile` 明确拒绝非 `list[str]` 的 `include_scopes` / `include_statuses` / `preferred_tags`，避免字符串被误拆成字符过滤器。
- `memory_get_runtime_digest` 在读文件前校验 `max_chars >= 0`，错误输入稳定返回 `invalid_input`。
- `memory_write_record` 的 tag schema 校验改为显式参数传递，去掉函数属性形式的隐式全局状态，避免多配置/并发场景串配置。
- `memory_compile` 的 legacy section 与 `last_used_at` 更新改为显式传入 config，去掉编译流程中的模块级临时状态。
- `memory_health_check` 增加未闭合 Front Matter 报告。
- `memory_update_index` 增加 `paths` 参数类型防御；治理记录移动后会在已有 `search.db` 上刷新索引。
- 新增 MCP stdio `tools/list` 烟测，确认 18 个工具可被 MCP 客户端发现。
- 修复 `scripts/run_memory_*_tests.ps1`：测试脚本优先使用 `MCP/Memory/.venv`，若该环境缺少 `pytest` 则自动回退到仓库根 `.venv`；同时从 `MCP/Memory` 目录执行 pytest，确保 `tests/memory_server/conftest.py` 被加载。

### TDD 验证

新增：

- `tests/memory_server/test_governance_quality.py`
- `tests/memory_server/test_runtime_maintenance.py`
- `tests/memory_server/test_robustness_edges.py`

覆盖：

- 缺少 `source_refs` 的候选验证拒绝。
- 低 `confidence` 的候选验证拒绝。
- 重复 candidate 拒绝。
- 与已发布系统规则冲突的 candidate 发布拒绝。
- 非 owner 发布拒绝。
- tag schema version 来自配置。
- 编译更新 `last_used_at`。
- 编译写入 compile-cache manifest。
- runtime digest 包含旧文件 section。
- health check 报告坏记录和缺失 search.db。
- schema migration 更新旧记录。
- `memory_update_index` 增量索引单条记录。
- 非 archived 记录禁止删除。
- archived 记录删除时写 tombstone。
- MCP dispatch 暴露维护工具。
- 缺失记录治理返回 `not_found`。
- 非列表编译过滤器返回 `invalid_input`。
- runtime digest 负数截断长度返回 `invalid_input`。
- dispatch 层错误参数不会触发内部异常。
- 配置化 tag schema 不会泄漏到其他 config。

验证结果：

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests\memory_server -q
# 106 passed

.\scripts\run_memory_all_tests.ps1
# 106 passed
```

---

## 2026-04-21 (vNext 治理闭环: validate / publish / archive)

> **状态：已实现**

### 背景

记录级写入、检索、编译已经可用，但候选记录仍缺少从 candidate 到 published / archive 的治理闭环。根据设计文档，系统记忆不能由 LLM 直接发布，必须经过验证与发布流程。

### 本次实现

- 新增 `memory_governance.py`。
- 新增 `memory_validate_candidate`：
  - 支持 candidate/raw -> validated。
  - 写入 `validated_by` 和 `updated_at`。
  - 将候选从 `memory-bank/candidates/` 移入对应治理层。
- 新增 `memory_publish_candidate`：
  - 只允许发布 `status=validated` 且带 `validated_by` 的记录。
  - 发布后写入 `published_by` / `published_at`。
  - 发布后进入 `memory-bank/shared/{id}.md`。
  - `*_candidate` 发布后转为 `system_rule`。
- 新增 `memory_archive_record`：
  - 将任意记录移入 `memory-bank/archive/{id}.md`。
  - 写入 `archive_reason` / `archived_at`。
- 扩展 `memory_compile`：
  - 新增 `target="system_digest"`。
  - 新增 `target="publish_queue"`。
- MCP 工具列表从 11 个扩展为 14 个。
- 更新 `README.md` 与 `MemorySystemDesignDocument.md`。

### TDD 验证

新增 `tests/memory_server/test_governance.py`，覆盖：

- candidate 验证后元数据更新，并移出候选池。
- validated candidate 发布为 shared system rule。
- 未验证 candidate 发布被拒绝。
- 任意记录可归档，并写入归档元数据。
- `publish_queue` 编译列出 candidate 记录。
- `system_digest` 编译列出 published shared rule。
- MCP dispatch 暴露并调用 validate / publish / archive。
- `_build_tools` 包含治理工具。

验证结果：

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests\memory_server -q
# 81 passed
```

---

## 2026-04-21 (vNext 编译层: runtime digest 与 task handoff)

> **状态：已实现**

### 背景

记录写入、Front Matter 解析、SQLite FTS 和记录搜索已经完成，但设计文档中的编译层仍缺失。根据 vNext 设计，编译必须是无 LLM 可运行、确定性、可重建的派生视图，而不是新的真源。

### 本次实现

- 新增 `memory_compiler.py`。
- 新增 `memory_compile`：
  - 支持 `target="runtime_digest"`。
  - 支持 `target="task_handoff"`。
  - 默认只包含 `validated` / `published` 记录。
  - 支持 `user`、`task_id`、`branch`、`include_scopes`、`include_statuses`、`preferred_tags` 过滤。
  - 个人记录在指定 `user` 时只包含该用户自己的记录。
  - 输出确定性 Markdown，不包含当前时间戳，重复编译内容稳定。
- 新增 `memory_get_runtime_digest`，读取已有 runtime digest。
- 编译产物路径：
  - `memory-bank/compiled/runtime/task/{task_id}.md`
  - `memory-bank/compiled/runtime/branch/{branch}.md`
  - `memory-bank/compiled/runtime/people/{user}-digest.md`
  - `memory-bank/compiled/runtime/system-digest.md`
  - `memory-bank/compiled/runtime/task/{task_id}-handoff.md`
- MCP 工具列表从 9 个扩展为 11 个。
- 更新 `README.md` 与 `MemorySystemDesignDocument.md`。

### TDD 验证

新增 `tests/memory_server/test_compile.py`，覆盖：

- `runtime_digest` 默认过滤 candidate，只包含 validated/published。
- personal 记录按 `user` 过滤。
- shared published 记录可被编译进 digest。
- 编译结果重复生成内容稳定。
- `memory_get_runtime_digest` 可读取已有编译产物。
- `task_handoff` 可生成交接视图。
- 未知 target 返回 `invalid_input`。
- MCP dispatch 暴露并调用 `memory_compile` / `memory_get_runtime_digest`。
- `_build_tools` 包含编译工具。

验证结果：

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests\memory_server -q
# 73 passed
```

---

## 2026-04-21 (测试加固: 多人读写与用户分区搜索)

> **状态：已完成，未改生产代码**

### 背景

Memory MCP 是项目协作基础设施，多人模式下的读写行为必须被测试明确固定，尤其是 `activeContext.md` 到 `activeContext/{user}.md` 的重定向、旧文件迁移、用户身份识别和共享文件 append 策略。

### 本次新增测试

新增 `tests/memory_server/test_multi_user.py`，覆盖：

- 同一逻辑路径 `memory-bank/activeContext.md` 在不同用户下写入独立文件：
  - `memory-bank/activeContext/alice.md`
  - `memory-bank/activeContext/bob.md`
- 不同用户读取同一逻辑路径时，只返回自己的用户分区文件。
- 首次读取旧版 `activeContext.md` 时自动迁移到当前用户分区，且保留旧单文件。
- 缺少有效用户身份时，`user_scoped` 写入返回 `user_required`。
- `.vscode/settings.json` 中的 `memory-mcp.userName` 优先于环境变量。
- 共享文件 `progress.md` 在 `append_only` 策略下将 overwrite 请求降级为 append。
- `memory_guard_check` 对每个用户分区文件分别报告。
- `_dispatch_tool` 下 `memory_write` / `memory_get` 使用同一套用户重定向。
- `memory_search` 可以扫描 `activeContext/{user}.md`，并可用 `include_paths` 定位单个用户文件。

验证结果：

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests\memory_server -q
# 65 passed
```

---

## 2026-04-21 (测试加固: SQLite FTS 查询计划)

> **状态：已完成，未改生产代码**

### 背景

记录级搜索已经通过功能测试证明能重建 `.ai-memory/search.db` 并命中记录，但还需要直接固定“搜索确实走 SQLite FTS 虚拟表索引”，避免后续改动退化成普通表扫描或绕过 FTS。

### 本次新增测试

- 在 `tests/memory_server/test_record_index.py` 新增 `test_search_records_query_plan_uses_sqlite_fts_index`。
- 测试会写入记录、重建索引、检查 `memory_records_fts` 包含 `search_text` 列。
- 使用 `EXPLAIN QUERY PLAN` 验证 `MATCH` 查询计划包含 `VIRTUAL TABLE INDEX`。

验证结果：

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests\memory_server -q
# 66 passed
```

---

## 2026-04-21 (记录级检索增强: 零依赖中文 n-gram)

> **状态：已实现**

### 背景

`memory_search_records` 初版依赖 SQLite FTS 默认 tokenizer。该方案对英文和 metadata 检索可用，但中文短词搜索效果不稳定；如果立即引入 `jieba` 等分词库，又会增加离线 wheel 预下载和部署维护成本。

### 本次实现

- 未新增任何 Python 第三方依赖。
- 在 `memory_record_index.py` 中新增 `build_search_text`：
  - 英文/数字/下划线/短横线按普通 token 保留。
  - 中文连续文本生成 bigram/trigram。
  - `tags`、`record_kind`、`scope`、`status`、`author`、`task_id`、`branch` 一并写入搜索文本。
- `memory_rebuild_index` 的 FTS 表新增 `search_text` 列。
- `memory_search_records` 查询统一转换为同一套 search text，再查 `search_text`。
- 增加旧版 `.ai-memory/search.db` 自动迁移：如果已有 FTS 表缺少 `search_text`，重建索引时自动 drop/recreate 派生表。
- 更新 `README.md` 与 `MemorySystemDesignDocument.md`，明确中文检索策略为无依赖 CJK n-gram。

### TDD 验证

- 新增中文短词搜索测试：`尺寸约束` 可命中 `导出链路尺寸约束`。
- 新增 metadata 搜索测试：`task_id` 可命中记录。
- 新增 tokenizer 单元测试：确认生成 CJK bigram/trigram。
- 新增旧 FTS schema 迁移测试。

验证结果：

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests\memory_server -q
# 55 passed
```

---

## 2026-04-21 (vNext 实现起步: 记录级写入与 SQLite FTS 索引)

> **状态：已实现首个 TDD 增量**

### 背景

基于 `MemorySystemDesignDocument.md` 的阶段 1 建议，本次没有推翻现有文件级接口，而是在 `v0.4.0` 的 `memory_get` / `memory_write` / `memory_search` / `memory_guard_check` 等能力旁边新增记录级基础能力。

### 本次实现

- 新增 `memory_write_record`，支持将结构化记忆写为 `Markdown + YAML Front Matter`。
- 新增记录级基础字段校验：`record_kind`、`scope`、`status`、受控 `tags`、`confidence` 范围。
- 新增候选、共享、个人、归档的基础落盘路由：
  - `memory-bank/candidates/{id}.md`
  - `memory-bank/shared/{id}.md`
  - `memory-bank/people/{user}/{id}.md`
  - `memory-bank/archive/{id}.md`
- 新增 Front Matter 解析与序列化工具，当前使用无额外依赖的 YAML 子集解析，保证无 LLM / 无新增运行时依赖时可用。
- 新增 `memory_rebuild_index`，从记录 Markdown 重建 `.ai-memory/search.db`。
- 新增 `memory_search_records`，通过 SQLite FTS 查询结构化记录，并返回记录元数据。
- MCP 工具列表从 6 个扩展为 9 个，同时保留既有文件级工具行为不变。

### TDD 验证

- 新增 `tests/memory_server/test_records.py` 覆盖记录写入、Front Matter 解析、非法枚举、受控标签和 dispatch。
- 新增 `tests/memory_server/test_record_index.py` 覆盖索引重建、记录级搜索和 dispatch。
- 更新动态工具描述测试，确认新增工具暴露在 MCP tool list 中。

验证结果：

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests\memory_server -q
# 51 passed
```

### 后续建议

- 下一步可在现有记录层上继续实现 `memory_compile` / `memory_get_runtime_digest`。
- 候选验证、发布、归档流程仍应放在记录层稳定之后推进。
- 当前 Front Matter 解析器只覆盖本项目记录 schema 需要的 YAML 子集，若后续需要复杂 YAML，应再评估是否引入运行时依赖。

---

## 2026-04-21 (vNext 设计稿: 从文件级 Memory MCP 走向记录级治理与编译架构)

> **状态：设计方案，未执行开发**

### 背景

`v0.4.0` 已经解决了多人协作下最现实的几个问题：

- `activeContext` 高频写入冲突
- 共享记忆文件的 append 策略
- 用户分区重定向
- guard / backup / compaction / 审计等基础设施

但当前系统的主抽象仍然是“文件”，而不是“记忆记录”。这会带来几个后续开发阶段绕不开的问题：

1. 系统记忆、个人记忆、候选记录、归档记录还没有统一的结构化外壳
2. 检索仍以文件粒度为主，尚未形成记录级索引与检索排序能力
3. 缺少候选验证、正式发布、降级归档这条治理链路
4. 缺少运行时 digest、handoff、publish queue 等“编译产物”层
5. MCP 对外能力仍偏向文件操作，尚未升级为记录级接口

因此，本次设计稿的目标不是推翻 `v0.4.0`，而是在兼容现有 `memory_get` / `memory_write` / `memory_search` / `memory_guard_check` 工作方式的前提下，补齐 Memory MCP 的长期架构。

### 本次设计结论

#### 1. 真源定义上移到“记录层”

系统真源不再仅仅理解为几份 Markdown 摘要文件，而是以下对象的组合：

- Markdown 记忆文件
- YAML Front Matter 元数据
- 事件日志
- 候选记录
- 已发布系统记忆

LLM 只能参与提炼、分类和填表，不能成为唯一真源。

#### 2. 记忆分层从 3 层扩展为 5 层

在当前 `memory-bank/`、`.ai-context/`、`.ai-memory/` 的基础上，设计稿将记忆职责明确拆分为：

- 个人记忆
- 系统记忆
- 历史记忆
- 本地临时记忆
- 编译记忆

这意味着后续实现不应再把所有长期知识都塞进少数几个共享 Markdown 文件，而应引入 `shared/`、`people/{user}/`、`candidates/`、`archive/`、`compiled/` 等正式目录层。

#### 3. 正式存储格式确定为 Markdown + Front Matter

本次设计明确了后续正式记录格式：

- MCP 传输层使用 JSON
- 落盘正文使用 Markdown
- 结构化元数据使用 YAML Front Matter
- 检索索引使用 SQLite FTS
- 审计与事件流使用 JSONL

这一步很关键，因为它把当前“文件写入工具”继续保留下来，同时为后续的记录级 schema、索引、编译和发布能力提供稳定底座。

#### 4. 正式引入“记忆编译”概念

设计稿将编译定义为一种确定性聚合过程，而不是让 LLM 再写一遍总结。编译输入优先依赖结构化字段，例如：

- `record_kind`
- `scope`
- `status`
- `tags`
- `source_refs`
- `confidence`
- `task_id`
- `branch`

编译输出初步划分为：

- `runtime digest`
- `task handoff`
- `system digest`
- `publish queue`

这意味着当前的 `activeContext.md`、`progress.md` 等文件，在长期演进里更适合作为“编译视图”或“人类可读摘要”，而不是唯一主真源。

#### 5. 正式引入候选治理与发布流

本次设计稿把系统记忆与 skill 治理流程统一定义为：

`raw -> candidate -> validated -> published -> degraded/archive`

同时明确以下边界：

- 允许自动创建候选、归档建议、编译视图
- 不允许 LLM 自动发布正式系统记忆
- 不允许 LLM 自动发布正式 skill
- 不允许 LLM 自动删除正式系统规则

这让后续 Memory MCP 能从“记录信息”升级到“治理知识”。

#### 6. 无 LLM 兜底能力被提升为硬要求

设计稿明确要求：即使没有 LLM，系统也必须完整支持：

- 原始记录写入
- Front Matter 解析
- 规则校验
- SQLite FTS 检索
- 模板式 digest 编译
- 候选验证与发布流程

换句话说，LLM 在后续架构中的定位是增强器，不是依赖前提。

### 目录模型调整建议

设计稿提出的 vNext 目标目录如下：

```text
memory-bank/
  shared/
  people/{user}/
  candidates/
  archive/
  compiled/
    runtime/
    publish/

.ai-context/{user}/

.ai-memory/
  config.json
  search.db
  events.jsonl
  compile-cache/
  temp/
  backups/
```

这里最重要的不是目录本身，而是职责变化：

- `shared/` 用于已发布系统记忆
- `people/{user}/` 用于个人长期沉淀
- `candidates/` 用于 system / skill 候选池
- `archive/` 用于降级和长期历史
- `compiled/` 用于运行时和发布流程所需的派生视图

### 与 v0.4.0 的关系

这次设计稿不是否定 `v0.4.0`，而是把 `v0.4.0` 放在更清晰的阶段定位上：

- `v0.4.0` 解决的是“多人协作下文件级记忆系统如何可用”
- 2026-04-21 设计稿解决的是“记忆系统如何从文件工具演进为长期治理系统”

因此后续实施应遵循以下顺序：

1. 先保留现有文件级接口，避免破坏当前工作流
2. 在此基础上增加记录级 schema、Front Matter 解析和 SQLite FTS 索引
3. 再补齐 compile / validate / publish / archive 能力
4. 最后才接入本地小模型或云端 LLM 做增强能力

### 对后续开发的直接约束

根据本次设计稿，后续继续开发 `ToolTest/MCP/Memory` 时应遵守以下约束：

1. 不再把少量共享 Markdown 文件当作唯一真源
2. 新能力优先围绕“记录级 schema”设计，而不是继续堆文件级特判
3. 所有 compile 结果必须可重建，不得成为唯一真源
4. 系统记忆正式发布必须经过候选验证，不允许 LLM 直接跳过治理流程
5. 任何增强能力都不得阻断无 LLM 的基础链路

### 产出

- 新增设计文档：`MemorySystemDesignDocument.md`
- 文档用途：作为 `MCP/Memory` 后续开发的 vNext 架构基线
- README 应继续维护“当前实现说明”，设计文档负责维护“下一阶段架构设计”

---

## 2026-03-31 (v0.4.0 设计稿: 十人团队多人协作 — 用户分区写入 + 冲突消除)

> **状态：设计方案，未执行开发**

### 背景与问题

团队即将扩展至 10 人规模，当前 Memory MCP 的多人协作支持存在以下瓶颈：

| # | 问题 | 严重度 | 影响范围 |
|---|------|--------|----------|
| 1 | `activeContext.md` 使用 overwrite 模式，10 人同时写入必然产生全文 Git 冲突 | **P0** | 每次 push/pull |
| 2 | overwrite 模式不注入任何用户标签，无法区分内容归属 | **P1** | 代码审查、冲突解决 |
| 3 | `.ai-context/` 不进 Git 但也无用户隔离，同一机器多人共用时互相覆盖 | **P1** | 共享开发机 |
| 4 | `config.json` 中 `preferred_mode: "append"` 在 DEVLOG v0.3.0 中提及但代码未实现 | **P2** | 配置与行为不一致 |
| 5 | 全局预算 60K chars 在 10 人场景下可能不够 | **P2** | 写入被拒绝 |

### 当前架构分析

```
memory-bank/              ← 进 Git，全团队共享
  activeContext.md         ← overwrite 模式，冲突高发区
  progress.md              ← 低频写入，冲突风险低
  techContext.md            ← 低频写入，冲突风险低
  systemPatterns.md         ← 低频写入，冲突风险低
  projectbrief.md           ← 极低频，冲突风险极低

.ai-context/              ← 不进 Git，个人临时上下文
  current-task.md          ← 个人任务，不冲突
  latest-error.md          ← 个人错误，不冲突

.ai-memory/               ← config.json 进 Git，其余不进
  config.json              ← 共享配置
  events.jsonl             ← 不进 Git
  backups/                 ← 不进 Git
  temp/                    ← 不进 Git
```

**核心矛盾**：`activeContext.md` 是写入最频繁的文件（每次会话结束都写），但使用 overwrite 模式，10 人 = 10 个 AI 会话 = 高频全文替换 = Git 冲突地狱。

### 设计方案

#### 总体策略：按用户分文件 + 共享文件只读/append

```
memory-bank/
  activeContext/                    ← 新：目录替代单文件
    _shared.md                      ← 团队共享上下文（只读 or append-only）
    alice.md                  ← 个人活跃上下文
    zhangsan.md                     ← 个人活跃上下文
    ...                             ← 每人一个文件，最多 10 个
  progress.md                       ← 保持不变（低频，append 模式）
  techContext.md                    ← 保持不变（低频，append 模式）
  systemPatterns.md                 ← 保持不变（低频，append 模式）
  projectbrief.md                   ← 保持不变（极低频）
```

#### 改动 1：activeContext 从单文件改为用户分区目录

**原理**：每人写自己的文件，Git 合并零冲突。

| 操作 | 旧行为 | 新行为 |
|------|--------|--------|
| 会话开始读取 | `memory_get("memory-bank/activeContext.md")` | `memory_get("memory-bank/activeContext/{user}.md")` + `memory_get("memory-bank/activeContext/_shared.md")` |
| 会话结束写入 | `memory_write("memory-bank/activeContext.md", ..., mode="overwrite")` | `memory_write("memory-bank/activeContext/{user}.md", ..., mode="overwrite")` |
| 团队公告/决策 | 无 | `memory_write("memory-bank/activeContext/_shared.md", ..., mode="append")` |

**`{user}` 来源**：`get_current_user(config.repo_root)` — 已有实现，优先读 `.vscode/settings.json["memory-mcp.userName"]`。

**实现要点**：
- `memory_writer.py`：新增 `resolve_user_path(path, user)` 函数
  - 当 path 匹配 `memory-bank/activeContext.md` 时，自动重定向到 `memory-bank/activeContext/{user}.md`
  - 其他路径不受影响
- `memory_reader.py`：`memory_get` 同理，读取时自动重定向
- `server.py`：工具描述更新，说明 activeContext 的用户分区行为
- 向后兼容：如果旧的 `activeContext.md` 单文件仍存在，首次启动时自动迁移到 `activeContext/{user}.md`

#### 改动 2：overwrite 模式注入用户标签尾注

**原理**：即使是个人文件，也需要可追溯性。

```python
# memory_writer.py — overwrite 分支
else:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    footer = f"\n<!-- last overwritten by {current_user} at {timestamp} -->\n"
    final_content = content.rstrip("\n") + "\n" + footer
```

**影响**：所有 overwrite 写入的文件末尾都会有 `<!-- last overwritten by xxx at xxx -->` 标签。

#### 改动 3：共享文件强制 append + 用户标签

**原理**：`progress.md`、`techContext.md`、`systemPatterns.md` 是团队共享知识，多人写入应使用 append 模式。

**实现**：
- `config.json` 的 guard targets 新增 `write_policy` 字段：

```json
{
  "path": "memory-bank/progress.md",
  "write_policy": "append_only",
  "role": "feature completion status, milestones"
}
```

- `memory_writer.py`：写入前检查 target 的 `write_policy`
  - `"append_only"`：如果调用方传入 `mode="overwrite"`，自动降级为 `append` 并在返回中标注 `"policy_override": "append_only"`
  - `"user_scoped"`：触发改动 1 的用户分区逻辑
  - `null` / 不设置：保持当前行为

#### 改动 4：`.ai-context/` 用户隔离（共享开发机场景）

**原理**：`.ai-context/` 不进 Git，但同一机器多人共用时会互相覆盖。

```
.ai-context/
  alice/
    current-task.md
    latest-error.md
  zhangsan/
    current-task.md
    latest-error.md
```

**实现**：与改动 1 类似，`resolve_user_path` 对 `.ai-context/` 下的路径也做用户分区。

**注意**：这个改动只在共享开发机场景下有意义。如果每人独立机器，`.ai-context/` 天然隔离，此改动可延后。

#### 改动 5：全局预算扩容

| 参数 | 当前值 | 10 人建议值 | 理由 |
|------|--------|-------------|------|
| `total_max_chars` | 60,000 | 150,000 | 10 人 × 8K activeContext + 共享文件 |
| `total_max_tokens` | 15,000 | 40,000 | 同比例扩容 |
| `guard_default_max_chars` | 12,000 | 12,000 | 单文件上限不变 |
| 每人 activeContext 上限 | — | 6,000 chars | 新增 per-user guard target |

#### 改动 6：config.json 新增结构

```json
{
  "multi_user": {
    "user_scoped_paths": [
      "memory-bank/activeContext.md",
      ".ai-context/current-task.md",
      ".ai-context/latest-error.md"
    ],
    "shared_paths_policy": {
      "memory-bank/progress.md": "append_only",
      "memory-bank/techContext.md": "append_only",
      "memory-bank/systemPatterns.md": "append_only",
      "memory-bank/projectbrief.md": "append_only"
    }
  },
  "guard": {
    "total_max_chars": 150000,
    "total_max_tokens": 40000,
    "per_user_max_chars": 6000,
    "targets": [
      {
        "path": "memory-bank/activeContext/{user}.md",
        "max_chars": 6000,
        "write_policy": "user_scoped",
        "role": "per-user sprint focus and recent decisions"
      },
      {
        "path": "memory-bank/activeContext/_shared.md",
        "max_chars": 8000,
        "write_policy": "append_only",
        "role": "team-wide announcements, shared decisions"
      }
    ]
  }
}
```

### 迁移计划

```
Phase A — 基础设施（无破坏性变更）
  1. memory_writer.py: 实现 overwrite 尾注（改动 2）
  2. memory_config.py: 新增 multi_user / write_policy 配置解析
  3. memory_writer.py: 实现 write_policy 检查（改动 3）
  4. 测试：全部原有测试通过 + 新增 write_policy 测试

Phase B — 用户分区（核心改动）
  5. memory_writer.py + memory_reader.py: 实现 resolve_user_path（改动 1）
  6. server.py: 工具描述更新
  7. config.json: 新增 multi_user 配置（改动 6）
  8. 迁移脚本: activeContext.md → activeContext/{user}.md
  9. 测试：用户分区读写 + 迁移 + 向后兼容

Phase C — 扩容与可选改动
  10. config.json: 全局预算扩容（改动 5）
  11. .ai-context 用户隔离（改动 4，可选，视是否有共享开发机需求）
  12. copilot-instructions.md: 更新 Memory Bank 硬规则中的路径说明
```

### Git 冲突分析（改动后）

| 文件 | 写入频率 | 写入模式 | 冲突概率 |
|------|----------|----------|----------|
| `activeContext/{user}.md` | 高（每次会话） | overwrite | **零**（每人独立文件） |
| `activeContext/_shared.md` | 低（团队决策时） | append | **极低**（append 天然可合并） |
| `progress.md` | 低 | append | **极低** |
| `techContext.md` | 低 | append | **极低** |
| `systemPatterns.md` | 低 | append | **极低** |
| `projectbrief.md` | 极低 | append | **几乎为零** |

### 风险与注意事项

1. **AI 指令适配**：`copilot-instructions.md` 中的 Memory Bank 硬规则需要同步更新路径（`activeContext.md` → `activeContext/{user}.md`），否则 AI 会话仍会尝试写入旧路径
2. **向后兼容**：需要处理旧的 `activeContext.md` 单文件到新目录结构的迁移，建议首次检测到旧文件时自动迁移
3. **用户名一致性**：10 人团队必须确保每人的 `.vscode/settings.json` 中配置了唯一的 `memory-mcp.userName`，否则会出现用户名冲突。建议在 `memory_writer.py` 中增加用户名校验（非空、非 unknown）
4. **compaction 适配**：`memory_compact` 的 `warm_context` 策略需要适配目录结构，对每个用户文件独立执行 compaction
5. **memory_search 适配**：搜索范围需要包含 `activeContext/` 目录下的所有用户文件
6. **guard 适配**：per-user guard target 需要动态匹配 `{user}` 占位符

### 工作量估算

| Phase | 改动文件数 | 预估工时 | 优先级 |
|-------|-----------|----------|--------|
| A（基础设施） | 2-3 | 2h | 高 |
| B（用户分区） | 4-5 | 4h | 高 |
| C（扩容与可选） | 2-3 | 2h | 中 |
| 测试 | 1-2 | 2h | 高 |
| 文档更新 | 2 | 1h | 中 |
| **合计** | | **~11h** | |

---

## 2026-03-24 (v0.3.1: 用户名可配置覆盖 — .vscode/settings.json)

### 背景
v0.3.0 的用户标识完全依赖 OS 用户名（`USERNAME`/`USER` 环境变量），但存在以下场景需求：
1. 多人共用同一台机器/同一 OS 账户时无法区分
2. 用户希望使用自定义名称（如昵称、工号）而非系统用户名
3. 需要一个**不进 Git、不影响 VSCode 本身**的本地配置方式

### 实现

#### 1. `.vscode/settings.json` 用户名覆盖 (`memory_events.py`)
- `get_current_user()` 新增可选参数 `repo_root: Path | None`
- 优先级变为：`.vscode/settings.json["memory-mcp.userName"]` → `USERNAME` → `USER` → `'unknown'`
- 新增 `_read_vscode_username(repo_root)` 内部函数，带进程级缓存（同一 repo_root 只读一次文件）
- 文件不存在、解析失败、key 不存在时静默回退，零副作用

#### 2. 调用方适配 (`memory_events.py` + `memory_writer.py`)
- `append_event()` 调用 `get_current_user(config.repo_root)` 传入项目根目录
- `memory_writer.py` 中 `get_current_user(config.repo_root)` 同步适配

#### 3. 配置示例
`.vscode/settings.json`（已被 `.gitignore` 排除，不进 Git）：
```json
{
    "memory-mcp.userName": "alice"
}
```
- VSCode 对未知 key 完全忽略，不影响 IDE 本身行为
- 每位开发者可在本地自定义用户名

### Verification
- `pytest tests/ -v` → 44 passed（全部原有测试通过，向后兼容）
- 不传 `repo_root` 时行为与 v0.3.0 完全一致

### 影响
- 向后兼容：`get_current_user()` 无参调用仍返回 OS 用户名
- `.vscode/` 目录已在 `.gitignore` 中排除，配置不进 Git
- 进程级缓存避免频繁文件 I/O

---

## 2026-03-24 (v0.3.0: 多人协作支持 — 用户标识 + Git 共享策略)

### 背景
多人同一项目使用 Memory MCP 时存在三个问题：
1. 审计日志不记录操作者，无法追溯谁写了什么
2. `events.jsonl`、`backups/`、`temp/` 等运行时基础设施文件进 Git 会产生无意义冲突
3. `activeContext.md` 使用 overwrite 模式，多人写入必然产生全文 Git 冲突

### 实现

#### 1. 用户自动识别 (`memory_events.py`)
- 新增 `get_current_user()` 工具函数
- 读取 `USERNAME`（Windows）/ `USER`（POSIX）环境变量，完全无感无需配置
- 未找到时回退到 `'unknown'`

#### 2. 审计日志注入用户 (`memory_events.py`)
- `append_event()` 记录自动注入 `"user"` 字段
- 所有写操作（write、backup、compact）的审计记录均可追溯操作者

#### 3. append 模式用户标识头 (`memory_writer.py`)
- 当 `mode == "append"` 时，自动在追加内容前插入 HTML 注释标识头：
  `<!-- written by {user} at {timestamp} -->`
- 多人追加不冲突，且可追溯每段内容的作者

#### 4. Git 共享策略 (`.gitignore`)
- `memory-bank/` 全部进 Git（全团队共享项目记忆）
- `.ai-memory/config.json` 进 Git（共享配置）
- `.ai-memory/events.jsonl`、`backups/`、`temp/` 排除出 Git（运行时基础设施）
- `.ai-context/` 维持不进 Git（个人临时上下文）

#### 5. 配置标注 (`memory_config.py`)
- `activeContext.md` 的 guard target 新增 `preferred_mode: "append"` 字段
- 作为多人协作时的推荐写入模式标注

### Verification
- `pytest tests/ -v` → 44 passed（原 44 全部通过，无破坏性变更）
- `get_current_user()` 在 Windows / POSIX 环境均可正常获取用户名

### 影响
- 向后兼容：`user` 字段为新增，旧审计记录无此字段不影响解析
- `preferred_mode` 为提示性字段，不改变实际写入逻辑
- `.gitignore` 变更不影响已有 Git 历史

---

## 2026-02-25 (v0.2.0: 备份轮转 + 全局预算 + 动态描述)

### 背景
P3 遗留问题：备份无限增长、无全局记忆预算、工具描述硬编码无法跟随配置变化。

### 实现

#### 1. 备份轮转 (`memory_backup.py`)
- 新增 `_list_batches()` / `_dir_size()` / `_rotate_backups()` 辅助函数
- 每次 `backup_files()` 执行后自动调用轮转
- 两重限制：`max_batches`（默认 50）+ `max_total_bytes`（默认 50MB）
- 先按 batch 数裁剪，再按总大小裁剪，从最旧开始删除
- 清理空日期目录

#### 2. 全局记忆预算 (`memory_guard.py` + `memory_writer.py`)
- `memory_guard_check` 返回新增 `total_budget` 字段：总 chars/tokens + 状态 + 消息
- 新增 `check_total_budget()` 可复用函数：预估写入后总量是否超限
- `memory_write` 写入前调用 `check_total_budget(extra_chars=net_new_chars)`
  - 超限 → 返回 `total_budget_exceeded` 错误，拒绝写入
  - 默认全局预算：60000 chars / 15000 tokens

#### 3. 动态工具描述 (`server.py`)
- 拆分为"静态基础描述"（`_BASE_DESCRIPTIONS`）+ "动态文件角色"（从 config 读取）
- `_build_file_roles(config)` 从 guard targets 的 `role` 字段组装文件说明
- `_build_tools(config)` 在 `create_server` 时按需生成 Tool 定义
- 移除全局 `TOOLS` 常量（160+ 行硬编码），改为按 config 动态生成
- 版本号升级至 `0.2.0`

#### 4. 配置文件扩展 (`memory_config.py` + `.ai-memory/config.json`)
- `GuardTarget` 新增 `role: str | None` 字段
- `MemoryConfig` 新增：`guard_total_max_chars`, `guard_total_max_tokens`, `backup_max_total_bytes`, `backup_max_batches`
- `DEFAULT_CONFIG_CONTENT` 新增 `backup` 节点 + `guard.total_max_*` + target `role`
- 配置默认值：备份 50 batch / 50MB，全局 60K chars / 15K tokens

### config.json 新增结构
```json
{
  "backup": {
    "max_total_bytes": 52428800,
    "max_batches": 50
  },
  "guard": {
    "total_max_chars": 60000,
    "total_max_tokens": 15000,
    "targets": [
      {"path": "...", "role": "file description for AI tool hints"}
    ]
  }
}
```

### Verification
- `pytest tests/ -v` → 42 passed（原 30 + 新 12）
- 新增测试覆盖：备份轮转（batch数/总大小/无需轮转/触发轮转）、全局预算（guard返回/通过/超限/写入拒绝/写入通过）、动态描述（role 组装/6工具生成/path hints）

### 影响
- 向后兼容：`role`/`backup`/`total_max_*` 缺省时功能不变
- 破坏性变更：移除全局 `TOOLS` 常量（外部若直接 `from server import TOOLS` 将失败，应改用 `_build_tools(config)`）

---

## 2026-02-25 (新增 memory_write 工具)

### 背景
评估发现 Phase 1 缺少写入工具，AI 无法通过 MCP 直接更新记忆文件。

### 实现
- 新增 `memory_writer.py`：受控写入工具，支持 overwrite / append 两种模式
- 安全特性：
  - `allowed_roots` 路径白名单（PathManager 强制）
  - 写入前自动备份（可关闭）
  - 原子写入（temp file + `os.replace`）
  - 每次写入记录审计事件到 `events.jsonl`
  - 写入后自动 guard 检查，返回 `guard_warning`（容量超阈值时）
- 注册为第 6 个工具，TOOLS 列表更新
- 新增 13 个测试用例覆盖：overwrite / append / 安全拒绝 / 备份控制 / guard 告警 / 审计日志 / 创建新文件 / 尾部换行

### Verification
- `pytest MCP/memory/tests/memory_server/ -v` → 20 passed (原 7 + 新 13)
- `from servers.memory_server.server import TOOLS` → 6 个工具全部注册

### 影响
- 工具数从 5 → 6
- 无破坏性变更，原有 5 个工具接口不变

---

## 2026-02-25 (健壮性审查 & 修复)

### 审查发现
对全部 11 个源文件进行代码审查，发现以下问题：

| 优先级 | 编号 | 问题 | 文件 |
|---|---|---|---|
| P0 | #3 | `memory_guard` 读文件未 catch OSError，单个坏文件中断全部检查 | memory_guard.py |
| P1 | #1 | `events.jsonl` 并发写入无锁保护，可能损坏审计日志 | memory_events.py |
| P1 | #7 | `_terms()` 正则只匹配 ASCII，中文关键词被忽略 | memory_search.py |
| P1 | #5 | Token 估算固定 `chars/4`，中文严重低估 | token_estimator.py |
| P2 | #2 | `memory_backup` 部分失败时已复制文件不回滚、不报告 | memory_backup.py |
| P2 | #6 | `memory_search` 只返回单行 snippet，缺上下文窗口 | memory_search.py |
| P3 | #4 | `_dispatch_tool` 缺参时传空字符串，错误信息不精确 | server.py |
| P3 | #10 | `_ensure_layout` 目录创建顺序冗余 | memory_config.py |

### 修复内容
- **memory_guard.py**: 在 `read_text` 外层加 try-catch `OSError`，标记 target 为 error 后 continue
- **memory_events.py**: Windows 使用 `msvcrt.locking`、POSIX 使用 `fcntl.flock` 做文件锁保护
- **memory_search.py**: `_terms()` 增加中文 Unicode 字符类 `[\u4e00-\u9fff]+`；search 返回匹配行 ±2 行上下文窗口并合并相邻命中
- **token_estimator.py**: 区分中文字符（按 ×0.6）和 ASCII（按 /4）分别估算
- **memory_backup.py**: 改为先校验所有路径、再批量复制；失败时返回 partial_success 附带已成功项
- **server.py**: `_dispatch_tool` 增加 required 参数缺失检测，返回精确错误
- **memory_config.py**: 修正 `_ensure_layout` 目录创建顺序

### Verification
- `run_memory_all_tests.ps1` 全部通过

---

## 2026-02-25 (hotfix: MCP SDK 迁移)

### Problem
- VS Code MCP 客户端无法发现 `project-memory-mcp` 的 5 个工具。
- 根因：服务器使用自定义 raw JSON-RPC stdio 循环实现协议握手，与 VS Code MCP 客户端（基于 `mcp` Python SDK 的 stdio 传输）不兼容。
- 对比：同项目中 `ue-editor-mcp` 使用 `mcp.server.Server` + `mcp.server.stdio.stdio_server`，工具发现正常。

### Fix
- 重写 `servers/memory_server/server.py`：
  - 移除自定义 `_read_message` / `_write_message` / `MemoryToolDispatcher` / `run_server` 等 raw JSON-RPC 代码。
  - 改用 `mcp.server.Server` + `mcp.server.stdio.stdio_server`（与 `ue-editor-mcp` 一致）。
  - 工具定义从 `ToolSpec` dataclass 改为 `mcp.types.Tool`。
  - 业务逻辑（5 个工具的 dispatch）保持不变。
- 更新 `requirements.txt`：添加 `mcp>=1.20`。
- 安装依赖：`python -m pip install mcp` → `mcp==1.26.0`。

### Verification
- 导入测试：`from servers.memory_server.server import TOOLS` → 5 个工具全部注册。
- stdio 集成测试：`initialize` → `tools/list` → `tools/call memory_guard_check` → 全部成功。

### Impact
- `.venv` 新增 `mcp` SDK 及其依赖（pydantic, anyio, httpx 等）。
- 无业务逻辑变更，所有 5 个工具功能不受影响。

---

## 2026-02-25

### Summary
- Completed Phase 1 memory MCP server delivery under `MCP/memory/`.
- Consolidated deployment and test scripts for local operations.
- Synced IDE and Codex MCP configurations to the new `MCP/memory` layout.

### Implemented
- MCP tools:
  - `memory_get`
  - `memory_search`
  - `memory_guard_check`
  - `memory_backup`
  - `memory_compact` (rule-based, safe default `dry_run=true`)
- Server packaging:
  - `servers/memory_server` as module entry (`-m servers.memory_server`)
- Deployment:
  - root deploy script `MCP/memory/deploy.ps1` (venv-first)
  - script-based deploy/run flow in `MCP/memory/scripts/`
- Tests:
  - split tests under `MCP/memory/tests/memory_server/`
  - per-feature test scripts and full test script

### Verification
- `powershell -ExecutionPolicy Bypass -File MCP/memory/scripts/run_memory_all_tests.ps1`
  - result: `7 passed`
- MCP runtime check:
  - initialize: success
  - tool call `memory_guard_check`: `ok=true`

### Notes
- Scope remains Phase 1 only (no vector DB, no embeddings, no FTS, no watcher, no LLM compression).
- Markdown memory files remain source of truth (`memory-bank`, `.ai-context`).

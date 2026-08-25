# Memory MCP Server

给 AI agent 使用的项目记忆服务器。Markdown 是真源，SQLite 是索引。MCP 默认暴露通用的 `memory_read` / `memory_write`、专用的 `memory_board_read` / `memory_board_write`，以及 Graph Agent 任务入口 `memory_task_sync`。

高级维护能力走 CLI：重建、诊断、备份、压缩、快照、谱系、治理、LLM enhance。

---

## 1. 项目说明

- **定位**：跨会话项目记忆。用于保存事实、决策、观察、任务节点、错误摘要、交接信息。
- **真源**：`memory-bank/**/*.md`。SQLite、compiled 文档、runtime digest、cache 都是可重建派生数据。
- **目录**
  - `memory-bank/`：项目记忆，建议入版本管理。
  - `.ai-context/`：当前任务热上下文，不入版本管理。
  - `.ai-memory/`：配置、索引、备份、审计、缓存，不入版本管理；`.ai-memory/config.json` 可入版本管理。
- **工具表面**：普通 agent 使用 `memory_read` / `memory_write`；跨 Agent 留言板使用专用的 `memory_board_read` / `memory_board_write`；任务生命周期使用 `memory_task_sync`。
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

### 2.1.2 启动期依赖自动修复与降级诊断

`servers/memory_server/__main__.py` 在 `import mcp` **之前**调用
`dependency_guard.ensure_ready()`，所以 venv 缺依赖、版本过旧或装成未迁移的 2.x 时，
server 不会以一条无法解释的 import 异常退出：

1. **自己修**。体检 → 离线优先修复（`vendor/` 先过 `SHA256SUMS` 校验，再 `--no-index`
   安装，失败才回退 PyPI）→ 复检 → **真的 import 一次** `mcp.server` → 继续正常启动。
   健康环境只多一次元数据读取和一次反正也要发生的 import，不起子进程、不联网。
2. **修不好也不失联**。`dependency_fallback` 用纯标准库顶上一个最小 MCP server，暴露
   `memory_environment_status`（确切诊断）和 `memory_repair_environment`（主动再修一次），
   并在 `initialize.instructions` 里说明真实工具不可用。**此时不要把缺失的工具理解成
   "功能不存在"，更不能据此判断记忆为空**；降级期间读写都不会落盘。

为什么复检之后还要真的 import 一次：元数据齐全不等于装得能用。`mcp` 在 Windows 上依赖
pywin32，而 pywin32 靠 `pywin32.pth` 往 `sys.path` 注入 `win32/lib`，`.pth` 只在解释器
启动时由 `site` 处理 —— 刚装完它的那个进程仍然 `import pywintypes` 失败，表现和"没修好"
一模一样。修复成功后会用 `site.addsitedir()` 补做一次 site 处理（而不是换进程，换进程会
让客户端持有的 PID 失效、stdio 通道跟着断）。

安全边界与防循环：

- **pip 自己不见了也能修**。删一个正在运行的 venv，Windows 删不掉 server 打开着的 `.pyd`，
  删除半途失败：pip 和一半 site-packages 没了。这种 venv 里每条修复命令都只报
  `No module named pip`。守卫会先用标准库自带的 `ensurepip`（不需要网络）把 pip 装回来。
  这里还有一层实测踩到的坑：`ensurepip` 内部是用捆绑的 wheel 跑 `pip install pip`，而那个
  pip 只看元数据 —— `pip/` 目录没了但 `pip-24.0.dist-info` 还在时，它报
  `Requirement already satisfied: pip`、退出码 0、什么都不装，修复在第一步就静默卡死。所以
  守卫在"`ensurepip` 报成功但 pip 仍不可导入"时会清掉那份已经没有对应包目录的
  `pip-*.dist-info`（`pip/` 还在就不碰）再重试一次，并把清掉了什么写进诊断。
  `deploy.ps1` 的 `-ForceRecreate` 与 cp-tag 不匹配重建都会先列出正在使用该 venv 的进程
  并拒绝执行，避免制造这种环境。
- **元数据齐全但 import 不起来也能修**。上面那条是这个问题在 pip 自身上的特例；一般情况
  同理：pip 只看 `dist-info` 判断"已满足"，包目录被删掉而 `dist-info` 留着时
  `pip install -r` 是空操作：它报成功、体检报"依赖正常"，而 `import mcp.server` 照样炸。
  守卫在"装完仍然 import 不起来"时会自动带 `--force-reinstall` 再走一遍。因此"环境可用"的
  判据是真的 import 一次 `mcp.server`（`PROBE_MODULES`），`--repair` 也按这个判据决定退出
  码，不会在一个起不来的环境上报 ok。这一轮升级重装由 `repair()` 自己在**持锁期间**完成：
  如果让调用方各自"装完→验证→再装一次"，两轮安装之间锁是放开的，别的进程正好能在那个窗口
  里挤进来开第二个 pip。
- 只在虚拟环境里自动装包。系统 Python 和 UE 自带 Python 会被拒绝并给出说明 —— 往共享
  解释器里装东西会影响别的项目。这类环境是直接拒绝，不写状态、不进冷却，因为"永远不该
  装"和"过 10 分钟再来"是两回事。
- **并发互斥**。平时这个 venv 只有一个 server 在用，但客户端重启、手工 `--repair`、启动
  探测都可能和它撞上。pip 自己没有跨进程锁，同时往一套 site-packages 里装同一批 wheel 会
  互相删改对方正在写的文件。修复前必须先拿 `<sys.prefix>/.memory_dependency_guard.lock`；
  等过锁的进程会先复检一次，环境已经被别人修好就直接放行，不再重装。
  这把锁**由内核持有**（Windows `msvcrt.locking`，POSIX `fcntl.flock`），不是"锁文件存在
  就算持锁"。用文件存在性表达持锁就得自己判断持锁进程死没死，而那个判断天生有竞态：两个
  等待者可以先后判定同一个锁陈旧，前者回收并成功持锁，后者随即把前者刚建好的锁当成陈旧的
  搬走，两个 pip 于是一起写同一套 site-packages。内核锁在进程退出时自动释放，因此不需要
  陈旧阈值，也不需要给长安装续心跳；代价是锁文件释放后不删除，"文件存在"不代表被持有。
- 单进程只自动修一次，失败后有 10 分钟冷却（从修复**结束**时刻起算，否则一次几十秒的
  离线安装会让冷却提前到期），避免客户端反复重启造成装包循环。通过
  `memory_repair_environment` 或下面的 CLI 显式请求时不受这两道闸门限制。状态文件是原子
  落盘的：截断式写入在进程被杀时会留下半截 JSON，那会被当成"没有状态"，冷却随之失效。
- **显式修复同样要拿锁**。冷却和"本进程树已试过"是防自动重启循环的，可以绕；跨进程锁不能。
  所有显式入口（兜底工具、`--repair`、以及 `deploy.ps1` / `scripts/bootstrap.ps1` 里的 pip）
  都经 `-m servers.memory_server.dependency_guard --locked-pip [--lock-wait <秒>] <pip 参数>`
  或 `locked_repair()` 走同一把锁；别人正在装时返回退出码 75（EX_TEMPFAIL）并提示稍后重试，
  而不是并行开第二个 pip。
- **带 UI 的调用者要用短 `--lock-wait`**。自动路径默认等到 540 秒，`deploy.ps1` /
  `scripts/bootstrap.ps1` 只等 60 秒然后拿 75 提示稍后重试。脚本自己的超时必须大于
  `--lock-wait` 加一次安装预算，否则脚本会在 pip 还在装时把 wrapper 杀掉，而杀 wrapper
  不会杀掉它下面的 pip —— 结果是一个没人持锁、还在写 site-packages 的孤儿 pip。
  `--locked-pip` 因此给 pip 加了超时并用 `taskkill /T /F` 连进程树一起杀，脚本侧也一样；
  两个 `.ps1` 的每一步 pip 都单独检查退出码，75 单独提示而不是当成安装失败继续往下走。
- 含中文的 `.ps1` 必须带 UTF-8 BOM。Windows PowerShell 5.1 对无 BOM 脚本按 ANSI 代码页
  解码，GBK 机器上落单的 UTF-8 字节会把紧随其后的 `"` 吞成双字节字符的后半，引号不配对，
  整个脚本连语法都过不了（`scripts/bootstrap.ps1` 实测如此）。测试会守住这条。
- pip 的 `PIP_*` 环境变量会被剔除，否则继承来的 `PIP_INDEX_URL` / `PIP_FIND_LINKS` 能把
  `--no-index` 那一步重新指向网络，"只用校验过的 vendor wheel"这条保证就没了。
  `vendor/SHA256SUMS` 的条目也只接受纯文件名，`../` 或绝对路径会让校验读到 vendor 之外的
  文件。
- 版本判定按 PEP 440，含"排他上界 `<V` 也排除 V 的预发布版"这条规则 —— 否则 `mcp<2` 会
  放过 `2.0.0rc1`，而 2.x 正是这条边界要挡的东西。
- pip 带超时，不会把客户端的 initialize 握手无限期挂住。首次面对一个**完全空**的 venv
  时，离线装完整依赖集可能超过客户端的 initialize 超时；此时重连一次即可，因为依赖已经
  装好了。常规的"缺一两个包"场景是秒级。
- `MEMORY_MCP_NO_AUTO_REPAIR=1` 关掉自动装包；`MEMORY_MCP_NO_NETWORK_REPAIR=1` 只用离线
  wheel；`MEMORY_MCP_NO_FALLBACK_SERVER=1` 让进程按原样崩（排障用）。

手动体检 / 修复（`--repair` 之外还可用 `--json`、`--requirements`、`--vendor`）：

```powershell
& <MemoryRoot>/.venv/Scripts/python.exe -m servers.memory_server.dependency_guard --repair
```

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
  "task_command_timeout_seconds": 2,
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

#### 2.3.2 Task Graph 协作与查询

启用 Hub 后，跨 Agent 协作只通过专用 `memory_task_sync` 读取和推进 Task Graph：
`sync` 返回 `{roots,nodes,edges,cursor}`，生命周期命令使用同一图中的 Task、Attempt、
Submission 与 Review 记录。它是执行协调的派生投影，不是新的记忆真源，也不会改变本地离线
读写、Brief、Board 或 `shared_context` 的既有语义。详细接口和部署侧说明见
[`memory_hub/README.md`](memory_hub/README.md)。

任务执行不通过 `memory_write` 的自由 metadata 表达。使用专用 `memory_task_sync` 读取
Graph Bundle，或写入 `create`、`assign`、`claim`、`decline`、`report`、`block`、`resume`、
`submit`、`review`、`reassign`、`cancel`。每个写入都必须携带 `command_id` 与
`expected_version`；执行者动作额外携带 `expected_assignment_epoch`：

```json
{
  "action": "claim",
  "command_id": "claim-task-42-agent-a",
  "task_id": "task-42",
  "actor_id": "agent:a",
  "expected_version": 3,
  "expected_assignment_epoch": 1
}
```

本地命令原子写入 `.ai-memory/task-graph.db` 的 append-only `task_events` 与规范化
Task/Attempt/Submission/Review 投影，并返回 `{roots,nodes,edges,cursor}`。`agent_id` 读取
筛选的含义是“当前 Attempt 的 assignee”；写入回执始终返回被影响任务的完整子图，不受该筛选
影响。

未配置 Hub 时，Task Graph 是完全本地的执行图。配置并启用 Hub 后，所有在线 Task Graph
命令会先经 Hub 的同一事务验证 `command_id`、version、assignment epoch 和内外事件的 Agent ID
一致性；Hub 接受或拒绝后，本地 SQLite 才提交，已接受的命令不会再写入 Outbox。提交进入
`review` 后，审核者必须不同于该 Submission 对应 Attempt 的执行者；违反时本地和 Hub 都返回
`reviewer_conflict`，任务保持在 `review`。对于运输层错误、`408` 和 `5xx`，权威调用会在原
`task_command_timeout_seconds` 总预算内用相同事件 ID 最多立即重试一次；恢复结果会带
`shared_sync.authority_attempts` 与 `shared_sync.recovered_after_retry`，持续失败会带
`authority_attempts`，可据此监控 Hub 可用性。Hub 不可用时，只有
`report` 与 `submit` 可作为离线记录写入本地并等待异步同步；`create`、`assign`、`claim`、
`decline`、`block`、`resume`、`review`、`reassign`、`cancel` 返回
`task_authority_unavailable`。这条同步路径只适用于 `memory_task_sync`，不会让普通
`memory_read` / `memory_write` 等本地 Memory 写入等待网络。

#### 2.3.3 响应预算与大型项目

所有 MCP 返回值在序列化前统一经过响应预算：默认最多 `12,000` 个字符和保守估算
`3,000` tokens。超限时服务端递归限制列表、字段、字符串和嵌套深度，保持有效 JSON，
并返回 `response_truncated=true` 与 `response_budget`。读取参数也在运行时限制，不能通过
绕过 JSON Schema 扩大响应：一般列表最多 50 项，`max_chars` 最多 32,000，`max_tokens`
最多 8,000；Task Graph 的 Hub 查询也有独立的节点、边和事件条数上限。

Hub API 默认使用紧凑投影：Feed 正文为 512 字符预览且 Brief 不返回 structured 详情，
Board 正文为 512 字符预览且不返回 references。Web 面板会显式请求展示所需 Task Graph
详情；agent 应通过 `memory_task_sync` 按需读取任务状态，不要把完整任务投影自动注入
`task_context`。这些限制只控制查询投影和传输，不删除 append-only Event 真源，也不改变
现有本地记忆、Brief 或 Board 数据。

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

该脚本只依赖标准库（CI 在安装依赖之前就会运行它），检查三类问题：

- **内容**：私有项目 ID、子系统名、游戏名、测试夹具、仓库路径、成员身份、私钥与 token。除已跟踪文件外，还会扫描未提交的源码类文件，避免"先写后提交"绕过门禁。二进制与非 UTF-8 文件跳过。
- **跟踪**：`*.local.json` 本机凭据以及 `.ai-memory/`、`.ai-context/`、`.venv/` 等运行时路径一旦被 Git 跟踪即失败，不设公开发布豁免。这些文件留在磁盘上是正常用法，只做跟踪性检查、不做内容检查。
- **vendor**：`vendor/SHA256SUMS` 必须存在，且与 `vendor/*.whl` 集合和摘要逐一对应。

回归测试见 `tests/memory_server/test_public_tree_audit.py`。给它加用例时，违规样本必须分片拼装（例如 `"P1" + "11"`），因为测试文件本身也在扫描范围内。

---

## 3. 使用方式

### 3.1 MCP 工具表面

| 工具 | 用途 |
|---|---|
| `memory_read` | 读取任务上下文与任务简报、读取文件、搜索、检索上下文、获取重要记忆、获取最新记忆、读取 runtime digest |
| `memory_write` | 写入 raw record、observation、checkpoint |
| `memory_board_read` | 查询未解决或历史留言，可按任务、作者、类型、状态和 thread 筛选 |
| `memory_board_write` | 发布留言、回复 thread、关闭已确认事项；本地优先，远端同步不阻塞 |
| `memory_task_sync` | 读取 Task Graph Bundle 与 Timeline；以 command/version/epoch 保护任务、Attempt、Submission、Review 生命周期；共享 Hub 启用时由 Hub 同步裁决编排命令 |

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
3. **执行任务图**：需要协作执行时先用 `memory_task_sync(action="sync")` 读取 roots，再用带版本令牌的专用命令推进生命周期。
4. **结束后写结果**：用 `memory_write(operation="record")` 写一条结构化总结；任务节点再写 `checkpoint`。
5. **管理动作走 CLI**：不要通过普通 MCP 写文件或维护派生文档。

检索默认使用 `ranking_version="v2"`：先区分业务领域记录与 Memory MCP/Task Brief 自评等元记录，再按强查询词的绝对命中数与覆盖率划分 relevance band，并在同一角色与 band 内参考相关性和 importance。领域 Task Brief 只用任务目标检索经验，不拼接活跃文件路径；先排除 band 0/1，再只保留距离本次最佳 query-match 不超过 0.10 的自适应窗口。仅当完全没有强证据时，最多降级装配 8 条 band 1 记录并显式标记 `weak_relevance_fallback_used`，不会为了填满大上下文而混入只命中模块名或三四个泛词的旧记忆。业务查询优先业务事实，记忆系统查询只使用记忆系统自身的工程经验。预算打包前合并 `auto_team_settlement` 跨 scope 镜像、精确重复与显式 supersede 链；代表记录继承组内最佳查询相关性，并用 `collapsed_best_record_id` / `collapsed_record_ids` 保留追溯。`facet_mode="hard"` 保持旧 API 的严格过滤语义；Task Brief 等内部推断使用 `facet_mode="boost"`，facet 缺失不会误删精确查询命中。发生 v2 内部异常时自动回退 v1；调用方也可显式指定 `ranking_version="v1"`。

### 3.2.A LLM 辅助 metadata 对齐（opt-in，§15.2-B）

为了在 agent 习惯性传入业务领域 tag（如 `sample_domain` / `sample_prefab`）时仍能把记忆落地，`memory_write(operation="record")` 与 `memory_read(operation="task_context")` 各暴露一个 **opt-in** 参数，默认关闭，启用且 LLM 已配置时生效，LLM 不可用时降级为原有行为且永远不静默改写。

- `memory_write(operation="record", llm_normalize_tags=True, ...)`：仅当请求 `tags` 含有不在受控词表中的值时触发。服务端会调用 `classify_record` 拿到合规 tag 建议，将 `requested ∩ allowed` 与 LLM 建议合并写入；被拒绝的业务词拼成 `tag1.tag2` 形态写到 `system_area`（仅当调用方未显式提供 `system_area` 时）。写入成功后返回字段：
  - `metadata_suggestion`：`{status: "ok"|"llm_unavailable"|"llm_failed"|"skipped", applied, requested_tags, accepted_tags, rejected_tags, final_tags, suggested_tags, suggested_record_kind, suggested_scope, suggested_system_area, confidence, rationale, model, message}`。
  - `warnings`：当且仅当真正发生归一化（`status == "ok"` 且存在 `rejected_tags`）时追加一条 `{code: "metadata_normalized_by_llm", from_tags, to_tags, rejected_tags, system_area, rationale}`。
  - LLM 不可用 / 调用失败时不改写 args，返回原 `invalid_input`，并附带 `metadata_suggestion` 帮调用方诊断。
- `memory_read(operation="task_context", llm_suggest_metadata=True, user_goal=..., active_files=[...])`：在原返回结构上额外追加 `suggested_metadata`（与上面同构），便于 agent 在动手前预先对齐 `record_kind` 与 tag。

设计要点：保持通用记忆工具与专用 Board 工具的 MCP 表面稳定；不引入 `memory_enhance`；LLM 永远只是“建议器”，最终 tag 仍由服务端 schema 校验保证 ⊆ `tag_schema.allowed_tags`。

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

### 3.2.C Graph Agent Task System 推荐提示词

仅当工作需要共享归属、分配、Review 或可见交接时使用 Task Graph；独立本地开发不需要为了
记录进度而创建协作任务。Task Graph 管理生命周期，Memory record 管理可复用事实，Board 只管理
需要对齐的协作信息。

可复制到 agent 规则的提示词：

```markdown
For work that needs shared ownership, assignment, review, or a visible handoff, use `memory_task_sync` as the only task-lifecycle interface. Start with `memory_task_sync(action="sync", context_token=<context token>, agent_id=<agent name>)`, inspect the returned Graph Bundle and Timeline, and create a task only when coordination needs one. Advance that task only through `create`, `assign`, `claim`, `decline`, `report`, `block`, `resume`, `submit`, `review`, `reassign`, or `cancel`; never encode lifecycle state in free-form `memory_write` metadata or Board posts. For every mutation, use a new stable `command_id` and the latest returned `version` as `expected_version`; whenever the action requires it, also use the latest `assignment_epoch` as `expected_assignment_epoch`. Refresh with `sync` after a conflict, and never reuse a command id with different content. Use `context_token` whenever available so the server derives the actor identity. With an active shared Hub, treat the Hub result as the coordination decision. If shared Hub authority is unavailable, only `report` and `submit` may be retained locally for later synchronization; do not substitute local `claim`, `review`, `reassign`, or a Board post for rejected lifecycle transitions. The Task Graph owns lifecycle state; use the Board only for a blocker, open question, handoff, or cross-agent risk that needs alignment.
```

### 3.2.D Project Board 推荐必要提示词

配置并启用远端 Hub 后，优先使用专用的 `memory_board_read` / `memory_board_write` 进行多人协作留言。原有 `memory_read(operation="board")` / `memory_write(operation="board")` 继续兼容，但不再作为 Agent 规则的推荐入口。Board 用于同步重要变更、结论、待处理事项、回复与关闭状态，不替代正式 Memory 事实。

可复制到 agent 规则的提示词：

```markdown
When a remote Memory Hub is configured and active, use the dedicated `memory_board_read` and `memory_board_write` tools for advisory cross-agent coordination, not task-state transitions. At task start, use unresolved board items injected by `memory_read(operation="task_context")` as advisory context and query `memory_board_read(filter="unresolved", task_id=<task>)` only when it helps avoid duplicate work or clarify a dependency. Create or update a board item only when a blocker, open question, handoff, or cross-agent risk would help others align; include the outcome, affected area, validation state, and remaining risk, but do not post routine progress, every Task Graph transition, or duplicate verified Memory records. Use `memory_board_write(action="post", post_type=<note|question|request|warning|handoff|proposal>, content=<message>, task_id=<task>)`; reply on an existing thread when useful, and resolve it after the outcome is locally observed or validated. Board availability, remote delivery, and replies must never gate local work: if the service is unavailable or nobody replies, continue with the safest local path and record assumptions. Never wait for a reply or remote confirmation solely to advance task state. Board identity and project membership come from the configured Hub token; do not put identity data, API keys, private keys, bearer tokens, database connection strings, or private memory content in board messages. Board messages are non-authoritative, best-effort coordination items; persist verified decisions and conclusions separately with `memory_write(operation="record", ...)`.
```

最小调用示例：

```json
{
  "action": "post",
  "post_type": "question",
  "content": "请确认网络接口修改影响",
  "task_id": "network"
}
```

调用工具：`memory_board_write`

```json
{
  "filter": "unresolved",
  "task_id": "network",
  "max_items": 20
}
```

调用工具：`memory_board_read`

```json
{
  "action": "reply",
  "thread_id": "<thread-id>",
  "reply_to": "<post-id>",
  "content": "回复内容"
}
```

调用工具：`memory_board_write`

```json
{
  "action": "resolve",
  "post_id": "<post-id>"
}
```

调用工具：`memory_board_write`

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

归档写为不可变分片：`memory-bank/archive/record-packs/{user}/YYYYMM-{source_sha[:16]}-{fragment:03d}.md`。其中 `{user}` 优先取源 pack 路径中的稳定用户 ID；旧格式路径从 `user_config.local.json` 读取稳定用户 ID（例如 `your-stable-user-id`）。同一源 pack 的重试会得到完全相同的分片；同名用户在不同设备产生不同源内容时会写入不同路径，绝不追加已有归档文件。归档仍属于 `memory-bank` 真源，`search_records`、runtime digest、key-doc rebuild 仍能读取。

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

在多成员同时使用 Git 的项目中，建议自动重建只包含 user-scoped 的 `activeContext`，并使用 `renderer="auto"` 让 LLM 优先优化个人暖上下文；`progress` 等共享派生文档应由明确的发布者或显式 CLI 低频重建，避免每台客户端整文件覆盖。建议在宿主项目的 `.ai-memory/config.json` 中采用该策略。

### 3.4 多人协作

多人安全模式始终开启。

用户解析优先级：

1. `MEMORY_MCP_USER`
2. `<MemoryRoot>/user_config.local.json["user_name"]`
3. `.vscode/settings.json["memory-mcp.userName"]`（旧配置兼容）
4. `USERNAME` / `USER`
5. `unknown`

推荐把稳定 user id（团队内使用企业微信 ID）放在 Memory 项目根目录的本地配置中，和 LLM 本地配置并列：

```powershell
Copy-Item <MemoryRoot>/user_config.example.json <MemoryRoot>/user_config.local.json
```

```json
{
  "user_name": "your-stable-user-id"
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

- 普通 Agent MCP 表面：通用 `memory_read` / `memory_write`，专用 `memory_board_read` / `memory_board_write`。
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

项目级反思只在 `checkpoint(task_done|test_failed)` 后写入队列。worker 从同一 `task_id` 的结构化记录收集证据（默认最多 256 条 / 1,000,000 字符，利用大上下文但不强制填满），依次执行 extractor 和 adversarial critic；确定性门禁再次校验证据 ID、类型、置信度、秘密信号和重复项。提案动作协议为 `CREATE / UPDATE / MERGE / SUPERSEDE / REJECT`：更新类动作只允许指向尚未被替代的 `project_shared + background_reflection + replaceable=true + authoritative=false` 记录；落盘始终新增记录并写 `supersedes`，绝不原地改写或删除旧记录。只有高置信且具有 `validation_result` 证据，或至少两个不同任务重复支持的提案，才能发布；`REJECT` 不写记录，其余未过门禁的候选只保留在 durable job result，等待 Curator。反思发布后的共享关键文档重建只取 `key_documents.auto_rebuild.targets` 与 `teamContext/progress/techContext/systemPatterns` 的交集；配置仅含 `activeContext` 时不会后台生成任何共享关键文档。

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

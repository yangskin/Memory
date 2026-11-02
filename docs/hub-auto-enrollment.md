# Memory MCP 可信自动接入

2026-11-02：新设备持有项目接入凭据即可由 Memory MCP 后台申请独立业务 Token。入口与 Codex、CodeBuddy、VS Code 等客户端无关，不依赖 HubTaskRunner 安装脚本。

## 配置与一次性部署

1. 在现有 Hub 部署中更新 `memory_hub` API。复用 `device_enrollments` 和 `access_tokens` 表，无新增迁移；旧部署须已完成 `0013_device_enrollment` 及后续既有迁移。保留现有数据库、证书、Token 和环境配置。
2. 管理员通过既有 CLI 签发仅有 `devices:enroll` 的接入凭据：`memory-hub token create --project <project> --user <user> --scope devices:enroll`。此命令向标准输出写出秘密，必须直接重定向到受保护的本地文件，不要粘贴到聊天、日志、Board 或 Git。每个授权用户使用独立接入凭据；它决定新设备所属项目及用户。
3. 通过组织现有安全配置方式，把接入凭据配置为新设备启动 Memory MCP 时继承的 `MEMORY_HUB_ENROLLMENT_TOKEN` 环境变量。新设置环境变量后须重启对应 MCP 进程。业务 Token 使用原有 `MEMORY_HUB_TOKEN`，两者用途不同。
4. 项目的 `.ai-memory/config.json` 只保存非秘密默认值：`shared_memory.enabled=true`、`server_url`、`project_id`、`auto_enroll=true`。`enrollment_token_env` 可指定接入凭据环境变量名。本机显式禁用仍优先。已有有效业务凭据继续使用。

消费项目须自行配置非敏感地址和项目编号。仅拉取仓库而未配置接入凭据的电脑返回 `enrollment_credential_required`，不获得访问权限。接入凭据自身不能读写留言、任务或记忆，也不会授予新设备注册或审批其他设备的权限。

## 接入行为

- MCP 启动后，由既有后台同步 worker 检查缺失的业务凭据。本地记忆读写和 MCP 握手不等待远端。
- 新设备本地生成 Token 和查询秘密，只向注册 API 发送哈希。服务端按接入凭据绑定 `user_id`、`project_id`，忽略客户端身份声明，返回批准状态。
- 新 Token 只有 `events:write` 和 `context:read`。依次检查 `/connection` 权限、留言板查询、任务列表和项目简报读取，全部成功才保存到被 Git 忽略的 `MCP/Memory/local/shared_memory.json`。保存前收紧文件权限，原子替换；多个客户端用同一把跨进程文件锁，避免重复注册。
- 临时申请保存在同目录 `hub-enrollment.json`。丢响应、断网、探测失败均复用同一申请，后台重试最短间隔 30 秒。服务端注册还有来源凭据限流、项目持久配额和 PostgreSQL 项目事务锁。
- 已撤销、过期或拒绝的业务凭据不会被自动替换以绕过撤销。损坏或归属不一致的申请状态报错，需要管理员诊断。撤销接入凭据会阻止新的申请，不会连带撤销此前签发的独立设备 Token；后者通过既有 `token revoke` 单独撤销。
- `hub_connection.status=ready` 表示新设备已完成上述接入探测；`configured` 仅表示有既有凭据，不代表本轮联网探测通过。错误只返回固定原因码，不输出秘密或远端错误正文。
- Token、地址、项目或读写开关改变时，旧的 `ready` 立即失效。留言工具首先确认本地入队，需远端查询确认同步完成；任务按远端权威结果判断。

## 项目重要记忆与客户端使用

每个客户端继续调用同一组 MCP 接口：`memory_read(operation="task_context")` 获得本地任务简报及后台共享上下文；需要立即刷新远端项目简报时用 `memory_read(operation="shared_context", include=["project_brief"], force_refresh=true)`；留言走 `memory_board_read/write`，任务走 `memory_task_sync`。

项目简报来自现有 Hub 汇总机制。`project_brief_available=false` 表示当前尚无已生成的项目简报，不伪造内容；它与接口可访问是两个状态。用户私有简报不会因设备接入而变成项目共享数据。

## 验证边界

测试覆盖本机多客户端并发、原子保存、断线恢复、权限检查、跨项目拒绝、撤销后禁止重新注册，以及真实 HTTP/SQL 路由下的新 Token 留言、任务全生命周期和共享简报读取。路由测试使用 SQLite 适配 PostgreSQL 列类型，不能替代生产 PostgreSQL 锁并发、实际远端部署和两台真实电脑的验收。

复核验证：本地 MCP 定向回归 51 项、Hub 定向回归 13 项通过（合计 64 项）。新增测试通过实际 MCP SDK 请求处理器验证后台注册、原实例配置重载、留言后台发送后远端落库、任务远端权威读取、共享简报读取；修复连接配置改变后仍显示旧 `ready` 的缺陷。路由与本地 MCP 的集成使用测试 HTTP 传输，不等同真实公网或真实双机验收。此前线上只读检查：原留言板、任务读取及项目简报接口可用，新 `/connection` 接口返回 404，尚不能宣称自动注册已上线；返回的项目简报生成于 2026-10-16。接入凭据尚未签发或分发。

仅更新服务器端不会自动更新已有客户端。新设备可用的前提是：服务端发布并完成迁移、客户端更新本包的 Memory MCP、接入凭据由 MCP 进程环境继承、当前用户有本地凭据目录写权限、HTTPS 可达、MCP 工具在客户端获准启用。完成这些一次性配置并重启 MCP 后才进入后台自动注册。项目简报更新仍依赖远端 worker 与实际共享事件输入，需要独立验收。

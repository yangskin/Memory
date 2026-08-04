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
然后在仓库根目录创建权限为 `0600` 的 `user_config.local.json`。该文件包含仅
首次显示的最小权限 Token，不能提交、复制到日志或发送到聊天中。再次运行不会
覆盖现有本机配置，也不会新增活动 Token。

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

## Token 管理

为一个本地 MCP 用户签发最小权限 Token：

```bash
docker compose -p <project-id> exec api memory-hub token create \
	--project <project-id> \
	--user <user-id> \
	--scope events:write \
	--scope context:read
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

`.env`、`user_config.local.json`、证书、私钥和 CSR 已被 Git 忽略。发布前应在
仓库根目录执行：

```bash
python scripts/check_public_tree.py
```
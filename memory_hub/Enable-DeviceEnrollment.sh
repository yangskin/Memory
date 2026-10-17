#!/bin/sh
# 在现有 Hub 部署目录执行；不接收或打印 Token 明文，不改数据库/SSH 凭据。
set -eu
project="${1:?Usage: sh Enable-DeviceEnrollment.sh <compose-project> <admin-token-id>}"
token_id="${2:?Provide administrator token ID, never the raw token}"
case "$project" in *[!A-Za-z0-9._-]*|"") echo "Invalid Compose project" >&2; exit 1;; esac
case "$token_id" in tok_*) ;; *) echo "Expected token ID, not raw token" >&2; exit 1;; esac
case "$token_id" in *[!A-Za-z0-9_-]*) echo "Invalid token ID" >&2; exit 1;; esac
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
umask 077
# 放在 Memory/local，已被仓库忽略；备份包含私有项目数据，不随源码发布。
mkdir -p ../local/enrollment-backups
backup="../local/enrollment-backups/hub-$(date -u +%Y%m%dT%H%M%SZ).sql"
docker compose -p "$project" exec -T postgres pg_dump -U memory_hub memory_hub > "$backup"
test -s "$backup"
docker compose -p "$project" up -d --build api
# 不依赖容器启动瞬间即就绪，最多等待一分钟。
ready=0
for attempt in 1 2 3 4 5 6 7 8 9 10 11 12; do
    if docker compose -p "$project" exec -T api python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:8000/healthz', timeout=3)"; then
        ready=1
        break
    fi
    sleep 5
done
test "$ready" = 1
docker compose -p "$project" exec -T api memory-hub token grant-device-approval --token-id "$token_id"
echo "Device enrollment enabled; administrator can run Approve-P111HubDevice.bat."

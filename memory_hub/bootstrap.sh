#!/bin/sh
set -eu

host="${1:?Usage: ./bootstrap.sh <public-hostname>}"
project_id="${2:?Usage: ./bootstrap.sh <public-hostname> <project-id> [user-id]}"
user_id="${3:-deployment}"
hub_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cert_dir="$hub_dir/certs"
env_file="$hub_dir/.env"
local_config="$(dirname "$hub_dir")/user_config.local.json"
shared_memory_config="$(dirname "$hub_dir")/shared_memory.local.json"
leaf="$cert_dir/$host.crt"
key="$cert_dir/$host.key"
intermediate_url="http://ica.wt.trustasia.com/TrustAsiaDVTLSRSACA2024.crt"

case "$project_id" in
    *[!A-Za-z0-9._-]* | "")
        echo "Project ID may contain only letters, numbers, dots, underscores, and hyphens." >&2
        exit 1
        ;;
esac

case "$user_id" in
    *[!A-Za-z0-9._-]* | "")
        echo "User ID may contain only letters, numbers, dots, underscores, and hyphens." >&2
        exit 1
        ;;
esac

if [ ! -f "$leaf" ] || [ ! -f "$key" ]; then
    echo "Expected certificate files are missing: $leaf and $key" >&2
    exit 1
fi

if [ ! -f "$env_file" ]; then
    umask 077
    password="$(openssl rand -base64 36 | tr -d '\n')"
    printf 'MEMORY_HUB_PUBLIC_HOST=%s\nPOSTGRES_PASSWORD=%s\n' "$host" "$password" > "$env_file"
    echo "Created .env with a generated internal PostgreSQL password."
fi

intermediate="$cert_dir/intermediate.pem"
if [ ! -f "$intermediate" ]; then
    bundle="$cert_dir/root_bundle.crt"
    if [ -f "$bundle" ]; then
        awk 'BEGIN { count = 0 } /-----BEGIN CERTIFICATE-----/ { count++ } count == 1 { print } /-----END CERTIFICATE-----/ && count == 1 { exit }' "$bundle" > "$intermediate"
    else
        temporary="$cert_dir/intermediate.der"
        curl --fail --silent --show-error --location "$intermediate_url" --output "$temporary"
        openssl x509 -inform DER -in "$temporary" -out "$intermediate"
        rm -f "$temporary"
    fi
fi

{
    cat "$leaf"
    printf '\n'
    cat "$intermediate"
} > "$cert_dir/fullchain.pem"
cp "$key" "$cert_dir/privkey.pem"
chmod 600 "$key" "$cert_dir/fullchain.pem" "$cert_dir/privkey.pem"
openssl verify -partial_chain -CAfile "$intermediate" "$leaf" >/dev/null

docker compose -p "$project_id" up -d --build --wait

if [ -f "$local_config" ] || [ -f "$shared_memory_config" ]; then
    echo "TLS chain prepared and $project_id is running. Kept existing local configuration."
    exit 0
fi

umask 077
token="$(docker compose -p "$project_id" exec -T api memory-hub token create \
    --project "$project_id" \
    --user "$user_id" \
    --scope events:write \
    --scope context:read)"

printf '{\n  "user_name": "%s"\n}\n' "$user_id" > "$local_config"
printf '{\n  "enabled": true,\n  "server_url": "https://%s",\n  "project_id": "%s",\n  "user_id": "%s",\n  "token": "%s"\n}\n' \
    "$host" "$project_id" "$user_id" "$token" > "$shared_memory_config"
chmod 600 "$local_config" "$shared_memory_config"
echo "TLS chain prepared, $project_id is running, and local MCP configuration was created at $local_config and $shared_memory_config."
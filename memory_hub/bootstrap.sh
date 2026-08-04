#!/bin/sh
set -eu

host="${1:?Usage: ./bootstrap.sh <public-hostname>}"
cert_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/certs"
env_file="$(dirname "$cert_dir")/.env"
leaf="$cert_dir/$host.crt"
key="$cert_dir/$host.key"
intermediate_url="http://ica.wt.trustasia.com/TrustAsiaDVTLSRSACA2024.crt"

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
echo "TLS chain prepared for $host. Start with: docker compose up -d --build"
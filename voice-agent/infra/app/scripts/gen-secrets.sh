#!/usr/bin/env bash
# Create infra/app/.env from .env.example with strong random secrets.
#
#   scripts/gen-secrets.sh            write ../.env (refuses if it exists)
#   scripts/gen-secrets.sh --stdout   print to stdout instead (no file written)
#
# Never overwrites an existing .env. Also creates nginx/staff-cidrs.conf from
# its example if missing, so the nginx bind mount has a file to mount.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXAMPLE="$APP_DIR/.env.example"
TARGET="$APP_DIR/.env"
TO_STDOUT=0

case "${1:-}" in
    "") ;;
    --stdout) TO_STDOUT=1 ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
esac

die() { echo "gen-secrets: $*" >&2; exit 1; }

[[ -f "$EXAMPLE" ]] || die "$EXAMPLE not found"
if [[ $TO_STDOUT -eq 0 && -e "$TARGET" ]]; then
    die "$TARGET already exists; not overwriting. Move it away first if you really want new secrets (the Postgres password is baked into its volume)."
fi

# Random hex of N bytes. openssl first, /dev/urandom as fallback.
rand_hex() {
    local bytes=$1 out
    if command -v openssl >/dev/null 2>&1; then
        out="$(openssl rand -hex "$bytes")"
    else
        out="$(od -An -tx1 -N"$bytes" /dev/urandom | tr -d ' \n')"
    fi
    [[ ${#out} -eq $((bytes * 2)) ]] || die "random generator returned short output"
    printf '%s' "$out"
}

# Key -> number of random bytes (empty = not a secret). A function rather
# than an associative array so it also runs on bash 3.2 (macOS).
secret_bytes() {
    case "$1" in
        OSS_JWT_SECRET) echo 64 ;;
        POSTGRES_PASSWORD|REDIS_PASSWORD|MINIO_ROOT_PASSWORD|ARI_PASSWORD) echo 24 ;;
        TELEPHONY_WS_TOKEN_SECRET|TURN_SECRET|DOGRAH_DEVOPS_SECRET|SUPPRESSION_API_KEY) echo 32 ;;
        *) echo "" ;;
    esac
}

render() {
    local line key value bytes
    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$line" =~ ^([A-Z0-9_]+)=(.*)$ ]]; then
            key="${BASH_REMATCH[1]}"
            value="${BASH_REMATCH[2]}"
            bytes="$(secret_bytes "$key")"
            if [[ -n "$bytes" && -z "$value" ]]; then
                printf '%s=%s\n' "$key" "$(rand_hex "$bytes")"
                continue
            fi
            if [[ "$key" == "MINIO_ROOT_USER" && -z "$value" ]]; then
                printf '%s=buzz-%s\n' "$key" "$(rand_hex 6)"
                continue
            fi
        fi
        printf '%s\n' "$line"
    done < "$EXAMPLE"
}

if [[ $TO_STDOUT -eq 1 ]]; then
    render
    exit 0
fi

umask 077
tmp="$(mktemp "$APP_DIR/.env.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
render > "$tmp"
# Refuse to race another writer: link fails if .env appeared meanwhile.
ln "$tmp" "$TARGET" 2>/dev/null || die "$TARGET appeared while generating; not overwriting"
rm -f "$tmp"
trap - EXIT
chmod 600 "$TARGET"
echo "gen-secrets: wrote $TARGET (mode 600)."

if [[ ! -e "$APP_DIR/nginx/staff-cidrs.conf" ]]; then
    cp "$APP_DIR/nginx/staff-cidrs.conf.example" "$APP_DIR/nginx/staff-cidrs.conf"
    echo "gen-secrets: created nginx/staff-cidrs.conf from the example; edit it."
fi

echo "Now set by hand in .env: SERVER_IP, PUBLIC_HOST/PUBLIC_BASE_URL (if not the default),"
echo "TLS_* paths, INFERENCE_HOST, VLLM_API_KEY and SPEACHES_API_KEY (copy from the inference host)."

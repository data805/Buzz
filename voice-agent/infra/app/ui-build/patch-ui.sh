#!/usr/bin/env bash
# Apply the Buzz patch to a Dograh source tree (tag dograh-v1.49.0).
#
#   patch-ui.sh <dograh-src-dir>            apply (idempotent)
#   patch-ui.sh --check <dograh-src-dir>    verify every target exists, change nothing
#
# Scope (R5: keep the UI fork small):
#   1. Build variables in ui/Dockerfile: Chatwoot blanked, leads/onboarding
#      endpoint pointed at a local dead path, PostHog/Sentry public keys empty.
#   2. Guards: assert the upstream code still behaves the way our build
#      variables and runtime env rely on (no edit).
#   3. Branding: logo/mark/favicon files replaced with Buzz assets, the
#      Dograh background wordmark blanked, and "Dograh" -> "Buzz" in the page
#      title, logo alt text, mobile header and welcome headings only.
#   4. Dograh's BSD 2-Clause LICENSE copied to /licences/dograh-LICENSE.txt
#      in the image, keeping the copyright notice with the build.
#
# Every step must find its target, or already be applied, or the script exits
# non-zero. An upgrade that moves or renames something therefore breaks loudly.
# Requires bash, python3 and cmp.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRANDING="$(cd "$SCRIPT_DIR/../../../branding" && pwd)"

CHECK=0
if [[ "${1:-}" == "--check" ]]; then CHECK=1; shift; fi
SRC="${1:?usage: patch-ui.sh [--check] <dograh-src-dir>}"
SRC="$(cd "$SRC" && pwd)"
[[ -f "$SRC/ui/Dockerfile" ]] || { echo "patch-ui: $SRC does not look like a Dograh checkout" >&2; exit 1; }

STEPS=0
fail() { echo "patch-ui: FAIL: $*" >&2; exit 1; }
ok()   { STEPS=$((STEPS + 1)); printf 'patch-ui: %-8s %s\n' "$1" "$2"; }

# replace FILE EXPECTED_COUNT OLD NEW
# Literal (not regex) replacement. Applied when OLD occurs exactly
# EXPECTED_COUNT times and NEW is absent; skipped when NEW is present
# EXPECTED_COUNT times (already applied); anything else fails.
replace() {
    local rel=$1 count=$2 old=$3 new=$4 file="$SRC/$1" result
    [[ -f "$file" ]] || fail "$rel: file not found"
    result="$(python3 -I - "$file" "$count" "$old" "$new" "$CHECK" <<'PY'
import sys
path, count, old, new, check = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5] == "1"
text = open(path, encoding="utf-8").read()
n_old, n_new = text.count(old), text.count(new)
# When NEW contains OLD (an insertion after OLD), an applied file still
# contains OLD once per NEW.
already = n_new == count and (n_old == n_new if old in new else n_old == 0)
if already:
    print("already")
elif n_old == count and n_new == 0:
    if not check:
        text = text.replace(old, new)
        open(path, "w", encoding="utf-8").write(text)
    print("ready" if check else "applied")
else:
    print(f"mismatch old={n_old} new={n_new} expected={count}")
PY
)"
    case "$result" in
        applied|ready|already) ok "$result" "$rel: $(printf '%s' "$old" | head -c 70 | tr '\n' ' ')" ;;
        *) fail "$rel: $result for: $old" ;;
    esac
}

# require FILE TEXT: upstream behaviour we rely on, unchanged.
require() {
    local rel=$1 text=$2 file="$SRC/$1"
    [[ -f "$file" ]] || fail "$rel: file not found"
    grep -qF -- "$text" "$file" || fail "$rel: expected text not found: $text"
    ok "guard" "$rel: $(printf '%s' "$text" | head -c 70)"
}

# asset SOURCE TARGET_REL: overwrite an existing upstream file with ours.
asset() {
    local src=$1 rel=$2 dst="$SRC/$2"
    [[ -f "$src" ]] || fail "branding source missing: $src"
    [[ -f "$dst" ]] || fail "$rel: upstream asset not found (renamed upstream?)"
    if cmp -s "$src" "$dst"; then ok "already" "$rel"; return; fi
    [[ $CHECK -eq 1 ]] && { ok "ready" "$rel"; return; }
    cp "$src" "$dst"
    ok "applied" "$rel <- ${src#$BRANDING/}"
}

# generated CONTENT_FILE TARGET_REL [new]: like asset() for generated files.
generated() {
    local src=$1 rel=$2 mode=${3:-replace} dst="$SRC/$2"
    if [[ "$mode" == "replace" ]]; then
        [[ -f "$dst" ]] || fail "$rel: upstream file not found (renamed upstream?)"
    else
        [[ -d "$(dirname "$dst")" || $CHECK -eq 1 ]] || mkdir -p "$(dirname "$dst")"
    fi
    if [[ -f "$dst" ]] && cmp -s "$src" "$dst"; then ok "already" "$rel"; return; fi
    [[ $CHECK -eq 1 ]] && { ok "ready" "$rel"; return; }
    mkdir -p "$(dirname "$dst")"
    cp "$src" "$dst"
    ok "applied" "$rel"
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "patch-ui: source $SRC ($([[ $CHECK -eq 1 ]] && echo check || echo apply))"

# --- 1. Build variables (ui/Dockerfile) -------------------------------------
# F3: the stock image bakes in Chatwoot (https://chat.dograh.com).
# ChatwootWidget.tsx returns early when either value is empty.
replace ui/Dockerfile 1 \
    'ENV NEXT_PUBLIC_CHATWOOT_URL="https://chat.dograh.com"' \
    'ENV NEXT_PUBLIC_CHATWOOT_URL=""'
replace ui/Dockerfile 1 \
    'ENV NEXT_PUBLIC_CHATWOOT_TOKEN="3fkFx2mCEjNHjM9gaNc4A82X"' \
    'ENV NEXT_PUBLIC_CHATWOOT_TOKEN=""'
# Leads: onboardingServiceClient.ts falls back to https://api-leads.dograh.com
# when NEXT_PUBLIC_ONBOARDING_API_URL is EMPTY, so blank is not enough. Point
# it at a same-origin path that nginx answers with 404.
replace ui/Dockerfile 1 \
    'ENV BACKEND_URL="http://api:8000"' \
    'ENV BACKEND_URL="http://api:8000"
# Buzz: no third-party endpoints baked into the browser bundle.
ENV NEXT_PUBLIC_ONBOARDING_API_URL="/_buzz/disabled"
ENV NEXT_PUBLIC_POSTHOG_KEY=""
ENV NEXT_PUBLIC_SENTRY_DSN=""
ENV NEXT_PUBLIC_GTM_ID=""
ENV NEXT_PUBLIC_META_PIXEL_ID=""
ENV NEXT_PUBLIC_REO_CLIENT_ID=""
ENV NEXT_PUBLIC_EVENT_BANNER=""'
require ui/Dockerfile 'ENV NEXT_TELEMETRY_DISABLED="1"'
require ui/Dockerfile 'ENV NEXT_PUBLIC_NODE_ENV="oss"'

# --- 2. Guards: upstream code paths our settings depend on -----------------
require ui/src/components/lead-forms/onboardingServiceClient.ts \
    'const BASE_URL = process.env.NEXT_PUBLIC_ONBOARDING_API_URL || "https://api-leads.dograh.com";'
require ui/src/components/ChatwootWidget.tsx \
    'if (!CHATWOOT_BASE_URL || !CHATWOOT_WEBSITE_TOKEN) {'
require ui/src/app/api/config/posthog/route.ts \
    "enabled: process.env.ENABLE_TELEMETRY === 'true',"
require ui/src/app/api/config/sentry/route.ts \
    "enabled: process.env.ENABLE_TELEMETRY === 'true',"
require ui/src/instrumentation-client.ts \
    'const hasPublicConfig = process.env.NEXT_PUBLIC_POSTHOG_KEY;'
require ui/src/app/layout.tsx \
    'const gtmId = process.env.NEXT_PUBLIC_GTM_ID?.trim();'
# nginx answers these locally; fail if upstream moves them.
require ui/src/app/api/config/latest-version/route.ts 'https://ghcr.io/token'
require ui/next.config.ts 'source: "/ingest/:path*",'
require ui/next.config.ts 'tunnelRoute: "/monitoring",'

# --- 3. Branding --------------------------------------------------------------
# BrandLogo.tsx: dograh-logo.png on light surfaces, -inverse on dark, mark in
# the sidebar. Same file names, so no component edits are needed.
asset "$BRANDING/png/buzz-logo-horizontal-color.png"   ui/public/dograh-logo.png
asset "$BRANDING/png/buzz-logo-horizontal-reverse.png" ui/public/dograh-logo-inverse.png
asset "$BRANDING/png/buzz-icon-color-512.png"          ui/public/dograh-mark.png

# The faint giant "dograh" wordmark behind app and auth pages (globals.css
# --brand-imprint). Replaced with an empty image of the same size.
printf '%s\n' "<svg xmlns='http://www.w3.org/2000/svg' width='1000' height='282' viewBox='0 0 1000 282'/>" > "$TMP/imprint.svg"
generated "$TMP/imprint.svg" ui/public/brand-imprint-light.svg
generated "$TMP/imprint.svg" ui/public/brand-imprint-dark.svg

# favicon.ico: wrap the Buzz 64 px PNG in an ICO container (PNG-in-ICO).
python3 -I - "$BRANDING/png/favicon-64.png" "$TMP/favicon.ico" <<'PY'
import struct, sys
png = open(sys.argv[1], "rb").read()
assert png[:8] == b"\x89PNG\r\n\x1a\n", "favicon source is not a PNG"
w, h = struct.unpack(">II", png[16:24])
entry = struct.pack("<BBBBHHII", w % 256, h % 256, 0, 0, 1, 32, len(png), 6 + 16)
open(sys.argv[2], "wb").write(struct.pack("<HHH", 0, 1, 1) + entry + png)
PY
generated "$TMP/favicon.ico" ui/src/app/favicon.ico

# Product name where the UI shows it as branding. Provider names ("Dograh"
# provider, "Dograh Service Keys") are left alone: they name Dograh's cloud.
replace ui/src/app/layout.tsx 1 'title: "Dograh",' 'title: "Buzz",'
replace ui/src/components/BrandLogo.tsx 4 'alt="Dograh"' 'alt="Buzz"'
replace ui/src/components/layout/AppLayout.tsx 1 \
    'className="text-lg font-bold md:hidden">Dograh</Link>' \
    'className="text-lg font-bold md:hidden">Buzz</Link>'
replace ui/src/app/overview/page.tsx 1 '"Welcome to Dograh"' '"Welcome to Buzz"'
replace ui/src/components/lead-forms/OnboardingModal.tsx 1 \
    'title="Welcome to Dograh"' 'title="Welcome to Buzz"'

# --- 4. Licence notice ------------------------------------------------------
require LICENSE 'Copyright (c) 2025, Zansat Technologies Private Limited'
generated "$SRC/LICENSE" ui/public/licences/dograh-LICENSE.txt new

echo "patch-ui: OK, $STEPS steps."

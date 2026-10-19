#!/usr/bin/env bash
# Build buzz/dograh-ui:<version>-buzz<n> from Dograh source (F3, D3).
#
#   ui-build/build-ui.sh              clone, verify, patch, docker build
#   ui-build/build-ui.sh --dry-run    clone, verify, check the patch, apply it
#                                     to the clone and re-check; no docker build
#   ui-build/build-ui.sh --save FILE  also write the image to FILE (docker save),
#                                     for hosts that cannot reach npm/GitHub
#
# Needs internet (GitHub, npm registry, Google Fonts for next/font) during the
# build. Build on a workstation, or open egress for the build window
# (infra/firewall/README.md), then move the image with --save / docker load.
set -euo pipefail

# ---- Pins: change together when upgrading (README "Upgrade") ---------------
DOGRAH_REPO="https://github.com/dograh-hq/dograh.git"
DOGRAH_TAG="dograh-v1.49.0"
DOGRAH_COMMIT="b86e8edddf4e4c9ba174792b5ceaba40c394a9bb"
IMAGE="buzz/dograh-ui:1.49.0-buzz1"
# ----------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$SCRIPT_DIR/work"
SRC="$WORK/dograh"
DRY_RUN=0
SAVE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --save) SAVE="${2:?--save needs a file}"; shift ;;
        -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

die() { echo "build-ui: $*" >&2; exit 1; }
command -v git >/dev/null || die "git is required"
command -v python3 >/dev/null || die "python3 is required"
[[ $DRY_RUN -eq 1 ]] || command -v docker >/dev/null || die "docker is required"

# Fresh clone every time: the patch must apply to pristine upstream source.
rm -rf "$SRC"
mkdir -p "$WORK"
echo "build-ui: cloning $DOGRAH_REPO at $DOGRAH_TAG"
git -c advice.detachedHead=false clone --quiet --depth 1 --branch "$DOGRAH_TAG" "$DOGRAH_REPO" "$SRC"

actual="$(git -C "$SRC" rev-parse HEAD)"
[[ "$actual" == "$DOGRAH_COMMIT" ]] || die "tag $DOGRAH_TAG is $actual, expected $DOGRAH_COMMIT (tag moved?)"
echo "build-ui: commit $actual verified"

# Prove every target exists before touching anything, then apply, then
# re-check (must report everything as already applied).
"$SCRIPT_DIR/patch-ui.sh" --check "$SRC"
"$SCRIPT_DIR/patch-ui.sh" "$SRC"
"$SCRIPT_DIR/patch-ui.sh" --check "$SRC" | grep -E '^patch-ui: (ready|applied) ' && die "patch not fully applied"

# Nothing from the original Chatwoot/leads values may remain in the UI tree.
if grep -rqF -e 'chat.dograh.com' -e '3fkFx2mCEjNHjM9gaNc4A82X' "$SRC/ui/Dockerfile"; then
    die "Chatwoot values still present in ui/Dockerfile"
fi

git -C "$SRC" --no-pager diff --stat

if [[ $DRY_RUN -eq 1 ]]; then
    echo "build-ui: dry run complete; source left in $SRC"
    exit 0
fi

echo "build-ui: building $IMAGE"
# Dograh's ui/Dockerfile expects the repository root as the build context.
docker build \
    -f "$SRC/ui/Dockerfile" \
    -t "$IMAGE" \
    --label "org.opencontainers.image.source=$DOGRAH_REPO" \
    --label "org.opencontainers.image.revision=$DOGRAH_COMMIT" \
    --label "org.opencontainers.image.version=$IMAGE" \
    --label "org.opencontainers.image.licenses=BSD-2-Clause" \
    --label "zm.co.bongohive.buzz.patch=infra/app/ui-build/patch-ui.sh" \
    "$SRC"

# Belt and braces: the built bundle must not contain the third-party endpoints.
echo "build-ui: scanning the image for third-party endpoints"
if docker run --rm --entrypoint sh "$IMAGE" -c \
    'grep -rlF -e "chat.dograh.com" -e "3fkFx2mCEjNHjM9gaNc4A82X" /app/.next /app/server.js 2>/dev/null' ; then
    die "Chatwoot endpoint or token found in the built image"
fi

docker image inspect --format '{{.Id}}' "$IMAGE"
if [[ -n "$SAVE" ]]; then
    docker save "$IMAGE" | gzip > "$SAVE"
    echo "build-ui: saved to $SAVE (load with: gunzip -c $SAVE | docker load)"
fi
echo "build-ui: done. Record the image ID in infra/app/versions.lock."

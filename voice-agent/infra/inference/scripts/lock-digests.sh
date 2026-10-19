#!/usr/bin/env bash
# Fill in the image digests in versions.lock from the images on this host.
#
# For each "image" line, reads the repo digest (sha256:...) of the local image
# with `docker image inspect` and writes it into the last column. Pull first
# (or pass --pull) so the digest is the one you are about to run.
#
# --check compares instead of writing: exits 1 if a local image differs from
# the recorded digest, or if a digest is still PENDING. Use it after upgrades
# and in the Phase 1 acceptance checks.
#
# Prints the matching .env lines (tag@digest) at the end, so you can pin the
# running stack to exactly these images.
#
# Usage: scripts/lock-digests.sh [--pull] [--check] [--lock-file PATH]
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
lock_file="${here}/versions.lock"
pull=0
check=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pull) pull=1; shift ;;
    --check) check=1; shift ;;
    --lock-file) lock_file="$2"; shift 2 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[[ -f "${lock_file}" ]] || { echo "missing ${lock_file}" >&2; exit 1; }
command -v docker >/dev/null || { echo "docker not found" >&2; exit 1; }

# Repository part of a reference: strip the tag (a ":" after the last "/").
repo_of() {
  local ref="$1" last="${1##*/}"
  if [[ "${last}" == *:* ]]; then echo "${ref%:*}"; else echo "${ref}"; fi
}

# Repo digest of a local image, matching its repository.
digest_of() {
  local ref="$1" repo
  repo="$(repo_of "${ref}")"
  docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "${ref}" 2>/dev/null \
    | awk -v r="${repo}" -F@ '$1 == r || $1 == "docker.io/" r { print $2; exit }'
}

# .env variable that selects each image.
env_var() {
  case "$1" in
    vllm) echo VLLM_IMAGE ;;
    speaches) echo SPEACHES_IMAGE ;;
    dcgm-exporter) echo DCGM_EXPORTER_IMAGE ;;
    node-exporter) echo NODE_EXPORTER_IMAGE ;;
    *) echo "# $1" ;;
  esac
}

tmp="$(mktemp)"
trap 'rm -f "${tmp}"' EXIT
status=0
env_lines=()

while IFS= read -r line || [[ -n "${line}" ]]; do
  read -r kind name ref recorded _ <<<"${line}" || true
  if [[ "${kind:-}" != "image" ]]; then
    printf '%s\n' "${line}" >> "${tmp}"
    continue
  fi
  if [[ "${pull}" -eq 1 ]]; then
    docker pull --quiet "${ref}" >/dev/null
  fi
  digest="$(digest_of "${ref}")"
  if [[ -z "${digest}" ]]; then
    echo "!! ${name}: ${ref} is not present locally (use --pull)" >&2
    status=1
    printf '%s\n' "${line}" >> "${tmp}"
    continue
  fi
  if [[ "${check}" -eq 1 ]]; then
    if [[ "${recorded}" == "${digest}" ]]; then
      echo "ok  ${name}: ${digest}"
    else
      echo "!!  ${name}: local ${digest}, locked ${recorded}" >&2
      status=1
    fi
    printf '%s\n' "${line}" >> "${tmp}"
  else
    [[ "${recorded}" != "PENDING" && "${recorded}" != "${digest}" ]] \
      && echo "changed ${name}: ${recorded} -> ${digest}"
    printf '%-6s %-14s %-56s %s\n' "${kind}" "${name}" "${ref}" "${digest}" >> "${tmp}"
    echo "locked  ${name}: ${ref}@${digest}"
  fi
  env_lines+=("$(env_var "${name}")=${ref}@${digest}")
done < "${lock_file}"

if [[ "${check}" -eq 0 ]]; then
  cat "${tmp}" > "${lock_file}"
  echo
  echo "Optional: pin .env to these exact images:"
  printf '  %s\n' "${env_lines[@]}"
fi

exit "${status}"

#!/usr/bin/env bash
# Read-only server inspection for PLAN section 11 (Phase 1 entry criteria).
# Runs the six agreed commands and changes nothing on the host.
#   usage: scripts/inspect-server.sh <role> [outfile]     role: inference | app
#   or:    ssh user@host 'bash -s' -- inference < scripts/inspect-server.sh
set -uo pipefail

role="${1:-}"
case "${role}" in inference|app) ;; *) echo "usage: $0 inference|app [outfile]" >&2; exit 2 ;; esac
out="${2:-}"

section() {
  # section <title> <command...>: print the command's output, or why it is missing.
  local title="$1"; shift
  printf '\n### %s\n' "${title}"
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "NOT-INSTALLED: $1"
    return 0
  fi
  "$@" 2>&1 || echo "EXIT-CODE: $?"
}

run() {
  echo "# Buzz server inspection"
  echo "role: ${role}"
  echo "date_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "hostname: $(hostname)"
  section "lsb_release -a" lsb_release -a
  section "nvidia-smi" nvidia-smi
  section "lspci (display and 3D controllers)" sh -c "lspci 2>&1 | grep -iE 'vga|3d|nvidia' || echo 'no GPU found by lspci'"
  section "df -h" df -h
  section "ss -tulpn" ss -tulpn
  section "docker version" docker version
}

if [[ -n "${out}" ]]; then
  run | tee "${out}"
else
  run
fi

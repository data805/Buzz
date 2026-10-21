#!/usr/bin/env bash
# Run every offline test suite. Each suite runs from its own folder because
# several of them use a top-level "tests" package.
set -euo pipefail
cd "$(dirname "$0")/.."

suites=(
  integrations/suppression-service
  integrations/redaction
  integrations/webhook-schemas
  tools/dograh
  tests/lint
  tests/scenarios
  tests/egress
  tests/bench
  infra/asterisk
  tests/inspection
)

failed=()
for s in "${suites[@]}"; do
  echo "== ${s}"
  if ! (cd "${s}" && "${PYTHON:-python3}" -m pytest -q -p no:cacheprovider); then
    failed+=("${s}")
  fi
done

echo "== workflow lint"
"${PYTHON:-python3}" tests/lint/lint_workflows.py --strict || failed+=("workflow lint")

echo "== front-desk prompt in sync"
"${PYTHON:-python3}" agents/front-desk/build_prompt.py --check || failed+=("prompt sync")

echo "== scenario files"
"${PYTHON:-python3}" tests/scenarios/validate_scenarios.py tests/scenarios/*.yaml || failed+=("scenario files")

if ((${#failed[@]})); then
  echo "FAILED: ${failed[*]}" >&2
  exit 1
fi
echo "All suites passed."

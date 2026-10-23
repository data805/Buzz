#!/usr/bin/env python3
"""Create an agent in Dograh from a local workflow.json (no cloud call).

The UI's "Create agent" button posts to /workflow/create/template, which asks
Dograh's cloud (MPS) to generate the agent. Buzz blackholes that service, so
the button returns 503. This uses /workflow/create/definition instead
(api/routes/workflow.py), which is purely local.

    python3 tools/dograh/import_workflow.py agents/front-desk/workflow.json --env-file infra/local/.env
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from configure_providers import R_LOGIN, merged_env  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("workflow", type=Path)
    ap.add_argument("--env-file", type=Path, default=Path(".env"))
    ap.add_argument("--api-url", default=None)
    args = ap.parse_args()

    env = merged_env([args.env_file])
    api = (args.api_url or env.get("DOGRAH_API_URL") or "http://127.0.0.1:8000").rstrip("/")
    spec = json.loads(args.workflow.read_text())

    with httpx.Client(base_url=api, timeout=30) as c:
        r = c.post(R_LOGIN, json={"email": env["DOGRAH_ADMIN_EMAIL"], "password": env["DOGRAH_ADMIN_PASSWORD"]})
        r.raise_for_status()
        h = {"Authorization": f"Bearer {r.json()['token']}"}
        existing = c.get("/api/v1/workflow/fetch", headers=h)
        existing.raise_for_status()
        if any(w.get("name") == spec["name"] for w in existing.json()):
            print(f"'{spec['name']}' already exists; nothing to do")
            return 0
        body = {"name": spec["name"], "workflow_definition": spec["workflow_definition"]}
        r = c.post("/api/v1/workflow/create/definition", json=body, headers=h)
        if r.status_code >= 400:
            print(f"error {r.status_code}: {r.text[:800]}", file=sys.stderr)
            return 2
        print(f"created '{spec['name']}' (id {r.json().get('id')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

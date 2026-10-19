# Testing plan: from base code to a working staging system

What has been tested, what is left, and the order to test it in once the servers are available. Each stage links to the runbook that has the exact commands and pass criteria.

## Already done (offline)

Run `pip install -r requirements-ci.txt && scripts/test-all.sh`. CI runs the same on every push, plus shell, Prometheus and Compose checks.

| Suite | Tests | What it proves |
|---|---|---|
| `integrations/suppression-service` | 174 | Zambian number rules, opt-out, fail-closed answers, no phone numbers in logs |
| `integrations/redaction` | 55 | PINs, cards, NRC and ID numbers are masked; dates and prices are not |
| `integrations/webhook-schemas` | 16 | Our Dograh webhook template produces the agreed payload |
| `tools/dograh` | 15 | Provider and telephony setup is idempotent and never prints secrets |
| `tests/lint` | 58 | The workflow linter catches cloud providers, missing opt-out, PII capture |
| `tests/scenarios` | 15 | Scenario files are valid |
| `tests/egress` | 19 | The packet-capture analyser flags any unexpected destination |
| `tests/bench` | 30 | Benchmark maths and audio conversion match what Dograh sends |
| `infra/asterisk` | 46 | Config renders cleanly; every dialplan jump exists; compliance steps run in order |
| promtool | 30 rules | Alert rules are valid and the two rule unit tests pass |

**Nothing has run on Docker, a GPU, Asterisk or a phone line yet.** Every item marked "(verify)" in the runbooks needs staging.

## Before staging can start

From PLAN section 9 and 12:

1. Server details for both hosts (IP, SSH, OS, GPU and VRAM). **Blocking.**
2. Approval of deviations D2 to D5 and decisions 3 to 10 in `DECISIONS.md`.
3. Domain and TLS choice (Q2).
4. SIP trunk test account and the carrier's IP ranges (Q4).
5. A reserved block of test caller numbers (Q13).

## Staging stages

Run them in this order; each one depends on the one before.

| Stage | What | Runbook | Pass |
|---|---|---|---|
| 0 | Read-only server inspection, firewall in `--dry-run` | PLAN section 11, `infra/firewall/README.md` | Output saved in `docs/inspection/` |
| 1 | Inference host: models downloaded, stack up offline, benchmark | `infra/inference/README.md` | Phase 1 acceptance (offline cold start, keys enforced, memory measured, p95 latency targets) |
| 2 | App host: UI built, stack up, first admin, sign-up closed | `infra/app/README.md` | Phase 2 acceptance table (no egress, no third-party requests in the browser, recordings protected) |
| 3 | Connect them: `tools/dograh/configure_providers.py`, then `--check` | `tools/dograh/` | No drift; "Dograh" connection archived; `buzz-local` is default |
| 4 | Front-desk agent imported; Test Audio in the browser | `agents/front-desk/README.md` | Agent answers from the knowledge block; lint clean after export |
| 5 | Asterisk and SIP trunk: 14 test calls | `infra/asterisk/README.md` | Phase 4 acceptance, including disclosure heard first on 10 calls and no denied call reaching the carrier |
| 6 | Compliance end to end: opt-out by voice, then redial blocked; redaction in the HubSpot record | `integrations/README.md`, `integrations/n8n/README.md` | Number on the list within one call; no PII in HubSpot |
| 7 | Egress packet capture on both hosts during stages 4 to 6 | `tests/egress/` | Analyser reports no destination outside the allow-list |
| 8 | Monitoring, alerts and backup restore drill | `infra/monitoring/README.md`, `infra/backup/README.md` | Dashboard populated; a test alert delivered; `restore drill PASSED` |
| 9 | Scenario suite (18 now, 50 for go-live) and a 10-call load test | `tests/scenarios/RUNNER.md` | All must-pass scenarios pass; latency holds at 10 concurrent calls |

Record each stage's result, with date and any measured figures, in `docs/CHANGELOG.md`.

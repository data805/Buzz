# Integrations

| Folder | What | Runs where |
|---|---|---|
| [`suppression-service/`](suppression-service) | FastAPI service: Zambian number rules, the opt-out list, `/v1/redact`. The compliance gate Asterisk calls before every outbound dial. | app host, container, `127.0.0.1:8090` |
| [`redaction/`](redaction) | `buzz_redaction`: regex redaction of NRC numbers, payment cards (Luhn), PINs, passport-like IDs and optionally phone numbers. Bundled into the suppression image. | library |
| [`webhook-schemas/`](webhook-schemas) | The Dograh webhook payload template, JSON Schemas for the Dograh body and for our cleaned post-call record, examples, tests. | contracts |
| [`n8n/`](n8n) | The three n8n flows: outbound trigger with consent check, post-call redaction to HubSpot and Sheets, and opt-out sync. Exports go here. | n8n |

## Data flow

```
HubSpot (consent) ─▶ n8n trigger ─▶ /v1/check ─▶ Dograh API ─▶ Asterisk buzz-outbound ─▶ /v1/dialplan/check ─▶ trunk
                                                       │
                         caller says "stop calling" ──▶ opt_out tool ─▶ /v1/opt-out
                                                       │
                     end of call ─▶ Dograh webhook ─▶ n8n ─▶ transcript_url ─▶ /v1/redact ─▶ HubSpot / Sheets
                                                                   └──────────▶ /v1/check (opt-out status)
suppression list ◀──────────────── opt-out sync ──────────────────▶ HubSpot consent property
```

## Tests

You need Python 3.13 or 3.14. One virtual environment covers all three test suites:

```sh
python3 -m venv .venv && .venv/bin/pip install -r suppression-service/requirements-dev.txt
(cd suppression-service && ../.venv/bin/python -m pytest -q)
(cd redaction          && ../.venv/bin/python -m pytest -q)
(cd webhook-schemas    && ../.venv/bin/python -m pytest -q)
```

Each folder has a `pytest.ini` that sets the import paths. Nothing needs to be installed as a package.

## Build

```sh
docker build -f integrations/suppression-service/Dockerfile -t buzz/suppression:1.0.0 integrations
```

The build context is `integrations/`, so the image can include `redaction/`. `.dockerignore` limits the context to the two packages and `requirements.txt`.

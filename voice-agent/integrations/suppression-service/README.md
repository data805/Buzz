# Suppression service

The compliance gate for every outbound dial (PLAN sections 6 and 7). It normalises numbers to Zambian E.164, refuses premium, short-code and international numbers, and keeps the opt-out list. The API is the contract in [`docs/INTERFACES.md`](../../docs/INTERFACES.md#suppression-service-api-integrationssuppression-service), plus `POST /v1/redact` (below), which is not in that table yet.

| | |
|---|---|
| Code | `suppression/` (`numbers.py` number rules, `db.py` SQLite, `app.py` API) |
| Image | `Dockerfile`, build context `integrations/` |
| Port | 8080 in the container; published as `127.0.0.1:8090` on the app host |
| State | one SQLite file (WAL) on a volume at `/var/lib/suppression` |
| Tests | `pytest` in this folder (see [`../README.md`](../README.md)) |

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `SUPPRESSION_API_KEY` | (required) | `X-API-Key` value. At least 24 characters. The service exits with status 2 if it is unset or too short. Compared with `hmac.compare_digest`. |
| `SUPPRESSION_DB_PATH` | `/var/lib/suppression/suppression.db` | SQLite file. Must be on a local disk (WAL does not work on network filesystems). |
| `SUPPRESSION_LOG_HASH_KEY` | derived from the API key | Key for the HMAC of numbers in the decision log. Set it separately if you want hashes to stay comparable across API-key rotations. |
| `SUPPRESSION_LOG_RETENTION_DAYS` | `90` | Decision-log rows older than this are deleted (at start-up and every 500 writes). |
| `LOG_LEVEL` | `INFO` | `DEBUG` also logs `/health` and `/metrics` requests. |

## Endpoints

All need `X-API-Key` except `/health` and `/metrics`. A missing or wrong key gives `401` (JSON body, so never `ALLOW …`).

| Endpoint | Behaviour |
|---|---|
| `GET /v1/dialplan/check?number=<raw>` | `200 text/plain`, exactly `ALLOW +260971234567` or `DENY <reason>`, **no trailing newline**. Reasons: `invalid`, `international`, `premium`, `short_code`, `suppressed`. A database error gives `503` with body `ERROR`. |
| `GET /v1/check?number=<raw>` | `{"allow": true, "e164": "+260…", "reason": "ok"}` or `allow: false` with one of the reasons above. `e164` is set whenever it can be derived (also for `international`, `premium` and `suppressed`), otherwise `null`. A database error gives `503 {"detail": "unavailable"}`; callers must treat any non-200 as deny. |
| `POST /v1/opt-out` | Body `{"phone_number", "source", "call_id"?}`. Always `201 {"e164", "suppressed": true}`, whether new or already listed (the first record's `source`, `call_id` and `created_at` are kept). Accepts any number that has an E.164 form, including international callers. Short codes and unparseable input give `422 {"detail": {"reason": …}}`. |
| `GET /v1/suppressions?since=<ISO date or time>` | JSON list of `{e164, source, call_id, created_at}`, oldest first. `created_at` is UTC, `…Z`. |
| `POST /v1/suppressions/import?source=import&dry_run=false` | Raw CSV body (`Content-Type: text/csv`, UTF-8, BOM allowed, at most 5 MB) with a `phone_number` column (case-insensitive). Optional `source` and `call_id` columns override the query parameter per row. One transaction. Returns counts and a validation report (see below). |
| `POST /v1/redact` | Body `{"value": <string, list or object>, "phones": false}`, at most 2 MB. Returns `{"value": <redacted>, "counts": {"pin": 1, …}, "total": n}`. Uses [`../redaction`](../redaction). For n8n. |
| `GET /health` | `200 ok`, or `503 unavailable` if the database cannot be read. |
| `GET /metrics` | Prometheus text. |

Example import report:

```json
{"dry_run": false, "rows": 7, "added": 2, "already_suppressed": 1, "duplicate_in_file": 1,
 "invalid": 3, "blank": 1, "errors": [{"line": 4, "reason": "invalid"}], "errors_truncated": false}
```

Errors give the CSV line number and reason only, never the number. Use `dry_run=true` first.

```sh
curl -sS -H "X-API-Key: $SUPPRESSION_API_KEY" -H 'Content-Type: text/csv' \
     --data-binary @optouts.csv 'http://127.0.0.1:8090/v1/suppressions/import?source=hubspot_export&dry_run=true'
```

## Number rules

Implemented in `suppression/numbers.py`; the sources are cited at the top of that file. The authority is the ITU-published ZICTA national numbering plan of 24 December 2024; libphonenumber's ZM metadata is the cross-check.

- **Accepted input:** `0971234567`, `971234567`, `+260971234567`, `260971234567`, `00260971234567`, with spaces, dashes, dots, slashes or brackets, and `+260 (0)97…`. The NSN is always 9 digits.
- **Callable (`ALLOW`):**
  - mobile: 95 and 75 (Zamtel), 96 and 76 (MTN), 97, 77 and 57 (Airtel), 98 (ZedMobile, formerly Beeline);
  - geographic: 211–218;
  - VoIP: 63 (Liquid). These are ordinary subscriber numbers, but the brief only names mobile and landline, so drop `VOIP_NDCS` if you don't want them.
- **`premium`:** anything starting 8 (free-phone and local-rate special services, e.g. 800) or 90 (premium rate).
- **`short_code`:** 3–5 digits (e.g. 112, 991, 993, 999), and USSD or service codes containing `*` or `#`.
- **`international`:** a `+` or `00` prefix followed by any country code other than 260.
- **`invalid`:** everything else. This includes:
  - reserved or unassigned ranges (55, 56, 58, 78, 79, 99, 219 and others);
  - 6-digit local fixed numbers with no area code;
  - foreign numbers written without `+` or `00`;
  - non-ASCII digits;
  - input longer than 64 characters.

The rules **fail closed**: when ZICTA assigns a new range (55 for Zamtel and 56 for MTN are reserved now), calls to it are denied until someone adds it to `numbers.py` with a source and updates the tests.

## Fail-closed design

- **Dialplan:** it dials only if the body starts with exactly `ALLOW +260`. Each of these gives a body that does not:
  - a wrong key, a database error (`ERROR`) or a crash (`500`);
  - a timeout or refused connection (empty result);
  - a `DENY` decision.
- **Errors:** a database error while looking up the list is reported as an error, never as `ALLOW`. A failure to write the decision log is logged and does not change the decision.
- **Start-up:** the service refuses to start without the key, or if SQLite cannot switch to WAL.
- **Durability:** writes use `synchronous=FULL`, so an acknowledged opt-out survives a power cut.
- **Deletion:** opt-outs cannot be deleted through the API. A manual `DELETE` is copied automatically to `suppression_removals` by a trigger, and `e164` cannot be updated in place.

## Privacy (DPIA notes)

- **Access logging:** uvicorn's access log is off, because it prints query strings and those contain numbers. The service logs `METHOD /route/template STATUS ms` only.
- **Request bodies** (opt-outs, CSVs, transcripts sent for redaction) are never logged.
- **Decision log:** `decision_log` keeps the time, endpoint, decision, reason and a 16-hex-character HMAC-SHA-256 of the number, so you can see that one number was denied three times without storing the number. A plain hash would be reversible, because there are only about 10⁹ Zambian numbers. Rows are deleted after `SUPPRESSION_LOG_RETENTION_DAYS`.
- **Metrics** carry counts only, labelled by endpoint, decision and reason.
- **Plain-text numbers** are stored only in `suppressions` (they must be, to match) and `suppression_removals` (the audit of manual removals).

## Metrics

| Metric | Labels |
|---|---|
| `suppression_checks_total` | `endpoint` (`check`, `dialplan`), `decision` (`allow`, `deny`), `reason` (`ok` or a deny reason) |
| `suppression_check_errors_total` | `endpoint`. Alert on any increase. |
| `suppression_opt_outs_total` | `result` (`new`, `duplicate`) |
| `suppression_import_rows_total` | `result` (`added`, `already_suppressed`, `invalid`) |
| `suppression_redactions_total` | `kind` (`nrc`, `card`, `pin`, `id`, `phone`) |
| `suppression_listed_numbers` | Gauge: size of the list |
| `suppression_request_seconds` | Histogram by `route` |

## Asterisk usage (suggestion for `infra/asterisk`)

Asterisk 22 `func_curl.c` trims trailing whitespace from the body (`ast_str_trim_blanks`) and returns the body whatever the HTTP status, unless `CURLOPT(failurecodes)` is set. We send no newline anyway, so the result is the same either way.

**Encode the number:** use `${URIENCODE()}`. An unencoded `+` turns into a space. That is harmless for `+260` numbers, but it turns a foreign number into `invalid` instead of `international`.

```
[buzz-outbound]
exten => _[+0-9].,1,Set(CURLOPT(httpheader)=X-API-Key: ${ENV(SUPPRESSION_API_KEY)})
 same => n,Set(CURLOPT(conntimeout)=2)
 same => n,Set(CURLOPT(httptimeout)=3)
 same => n,Set(VERDICT=${CURL(http://127.0.0.1:8090/v1/dialplan/check?number=${URIENCODE(${EXTEN})})})
 same => n,GotoIf($["${VERDICT:0:10}" != "ALLOW +260"]?deny,1)
 same => n,Set(E164=${VERDICT:6})
 same => n,GotoIf($[${REGEX("^\+260[0-9]{9}$" ${E164})}]?:deny,1)
 ; ... GotoIfTime, GROUP_COUNT, Dial(PJSIP/${E164}@zm-trunk)
exten => deny,1,NoOp(suppression: ${VERDICT})
 same => n,Hangup(21)
```

The exact syntax of this snippet still needs to be tested on the Asterisk build (**verify**).

## Compose (suggestion for `infra/app`)

```yaml
suppression:
  image: buzz/suppression:1.0.0
  build: { context: ../../integrations, dockerfile: suppression-service/Dockerfile }
  environment:
    SUPPRESSION_API_KEY: ${SUPPRESSION_API_KEY}
  volumes: [suppression-data:/var/lib/suppression]
  ports: ["127.0.0.1:8090:8080"]
  networks: [buzz-app]
  read_only: true
  cap_drop: [ALL]
  security_opt: ["no-new-privileges:true"]
  restart: unless-stopped
```

Containers on `buzz-app` (the Dograh `opt_out` tool, n8n if it is self-hosted there, and Prometheus if it runs in a container) reach the service at `http://suppression:8080`, not at `127.0.0.1:8090`. Loopback inside a container is the container itself.

## Manual removal (audited)

Only with a written request from the data subject, recorded in the DPIA log:

```sh
docker compose exec suppression python -c "import sqlite3; c=sqlite3.connect('/var/lib/suppression/suppression.db'); c.execute(\"DELETE FROM suppressions WHERE e164='+260…'\"); c.commit()"
```

The trigger writes the removed row to `suppression_removals` with `removed_at`.

## Backups

Back up with `sqlite3 … ".backup"`, or copy the file only while the service is stopped. A plain copy of a live WAL database can be inconsistent. Add the `suppression-data` volume to `infra/backup`.

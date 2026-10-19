# Backup, restore and retention (app host)

| Script | Does | Schedule |
|---|---|---|
| [`backup.sh`](backup.sh) | encrypted backup of Dograh Postgres, the suppression SQLite database, Asterisk config and spool, and the MinIO bucket | `buzz-backup.timer`, 01:30 daily |
| [`restore.sh`](restore.sh) | `--verify` restore drill, `--extract`, `--restore-postgres` | manual (the drill is a Phase acceptance test) |
| [`retention.sh`](retention.sh) | PLAN section 7: expire recordings and transcripts after 90 days and anonymise call data. **Dry run by default.** | `buzz-retention.timer`, 03:30 daily, with `--apply` |

The scripts assume Ubuntu with GNU coreutils, Docker, `age`, `python3` and `rsync` (only for the off-site copy). They run as root from `/opt/buzz/voice-agent/infra/backup` **(verify the install path)**.

## What is backed up

| Artefact | How | File in each backup |
|---|---|---|
| Dograh Postgres (`postgres` DB) | `docker exec <postgres> pg_dump -Fc` plus `pg_dumpall --globals-only` | `postgres.dump.age`, `postgres-globals.sql.age`, `alembic_version.txt` |
| Suppression list (SQLite, WAL) | Python `sqlite3` online backup API inside the container, then `PRAGMA integrity_check` | `suppression.sqlite.age` |
| Asterisk | `tar` inside the container of `ASTERISK_PATHS` (config, voicemail, monitor/recording, custom sounds) | `asterisk.tar.gz.age` |
| MinIO bucket `voice-audio` | `mc mirror` to a staging directory, then `tar`. Full on Sundays (`WEEKLY_DAY`); on other days, objects newer than `MINIO_INCREMENTAL_WINDOW` (2 days, so one missed night is still covered). | `minio-full.tar.age` or `minio-incremental.tar.age` |
| Integrity | `sha256sum` of every `.age` file | `SHA256SUMS`, `manifest.txt` |

**Layout:**

```
/srv/buzz-backups/
  daily/2026-10-19T233000Z/…   newest 14 runs (KEEP_DAILY)
  weekly/2026-W42/…            Sunday runs, hard-linked, newest 8 (KEEP_WEEKLY)
  .last_success  .lock  .staging/
```

To restore MinIO, you need the newest `minio-full` that is not later than the target date, plus every `minio-incremental` after it.

**Sizing.** A full MinIO archive is about the size of 90 days of recordings. Eight weekly fulls plus the dailies need roughly 9× the bucket size. Put `BACKUP_ROOT` on its own disk, and set `OFFSITE_RSYNC_TARGET` for a copy off the host. A backup on the same disk as the data does not survive the loss of that disk.

**mc image.** MinIO has archived the community `mc`: Docker Hub images are gone and dl.min.io returns `410 Gone`. `dograhai/minio` does not include `mc` (`Dockerfile.dograh`). Build the final tag once:

```bash
docker build -t buzz/mc:RELEASE.2025-08-13T08-35-41Z infra/backup/mc
```

`mc` reaches MinIO through `MC_HOST_buzz` on the `buzz-app` network. `MINIO_ROOT_PASSWORD` must therefore be URL-safe; generate it with `openssl rand -hex 24`.

## Encryption: age

The scripts encrypt every artefact while it streams, with [age](https://age-encryption.org) (v1.3.2) and **public-key recipients**. The server can encrypt but cannot decrypt.

**Key handling:**

1. On an admin laptop (not the server), generate two identities, one for each custodian. Protect each with a passphrase:
   `age-keygen | age -p > buzz-backup-<name>.age` (prints the public key).
2. Store each encrypted identity, plus its passphrase, separately in the BongoHive password manager. Also print each on paper and keep it in the office safe. Two custodians means losing one key loses nothing.
3. On the server, put **only the public keys** (`age1…`, one per line) in `/etc/buzz/backup/age-recipients.txt` (root, 0644). Each manifest records the SHA-256 of this file, so you can tell which recipients a backup was made for.
4. For a drill or a restore, copy one decrypted identity to `/root/age-identity.txt` (mode 0600). Run with `AGE_IDENTITY_FILE=/root/age-identity.txt`, then run `shred -u` on the file. Never leave it on the server.
5. Key rotation: add the new public key, run a backup, and check that it decrypts with the new key. Then remove the old key. Old backups still need the old key until they rotate out (8 weeks).

Install age from the Ubuntu archive (`apt install age`) or from the v1.3.2 release, checking the checksum.

## Install

```bash
sudo install -d -m 0755 /etc/buzz/backup /var/lib/node_exporter/textfile_collector
sudo install -d -m 0700 /srv/buzz-backups
sudo cp infra/backup/.env.example infra/backup/.env && sudo chmod 600 infra/backup/.env   # edit
sudo editor /etc/buzz/backup/age-recipients.txt
docker build -t buzz/mc:RELEASE.2025-08-13T08-35-41Z infra/backup/mc
sudo cp infra/backup/systemd/*.service infra/backup/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start buzz-backup.service && journalctl -u buzz-backup.service -n 50
sudo /opt/buzz/voice-agent/infra/backup/retention.sh          # dry run: read the output
sudo systemctl enable --now buzz-backup.timer buzz-retention.timer
```

node-exporter (from `infra/monitoring`) reads `/var/lib/node_exporter/textfile_collector`. Prometheus alerts if `buzz_backup_last_success_timestamp_seconds` is more than 26 hours old (`BackupTooOld`).

## Runbook

- **`BackupTooOld` / `BackupLastRunFailed`:** run `journalctl -u buzz-backup.service -n 200`. A failed run leaves `.inprogress-<stamp>.failed` (deleted after 7 days). Common causes: a container is not running (`no running container for service …`, so set `<SERVICE>_CONTAINER`), the disk is full, the recipients file is missing, or the `mc` image has not been built. Fix the cause, then run `sudo systemctl start buzz-backup.service`.
- **One component only:** `sudo ./backup.sh --only postgres`. This does not update the success metric. Add `--full` to force a full MinIO mirror.
- **Disk filling:** lower `KEEP_WEEKLY` or move `BACKUP_ROOT`. Never delete the newest `minio-full`.

## Restore drill (Phase acceptance test)

Run this after first install, after every Dograh upgrade (the schema changes), and quarterly. Record the result in `docs/CHANGELOG.md`.

```bash
sudo AGE_IDENTITY_FILE=/root/age-identity.txt ./restore.sh --verify            # newest daily
sudo AGE_IDENTITY_FILE=/root/age-identity.txt ./restore.sh --verify /srv/buzz-backups/weekly/2026-W42
sudo shred -u /root/age-identity.txt
```

**Pass criteria.** The script exits 0 and prints `restore drill PASSED`. That means:

1. `SHA256SUMS` matches.
2. The dump restores with `pg_restore --exit-on-error` into a throwaway `VERIFY_PG_IMAGE` container with `--network none`.
3. `alembic_version` is present and matches the manifest, and the `vector` extension exists.
4. `organizations` and `users` are not empty. The script also prints counts of workflows, runs and recordings, and the latest run time; compare these with the live system.
5. The suppression copy passes `integrity_check` and shows its row counts. Compare with `GET /v1/suppressions`.
6. The Asterisk and MinIO archives decrypt and list.

The drill also writes `buzz_restore_verify_last_success_timestamp_seconds`.

For the **full** acceptance test, add a timed restore of a staging app host from the backup. Use the order below and check that a test call and a recording playback work.

## Full restore (disaster recovery)

1. Rebuild the host, then bring up `infra/app` with the same `.env` (secrets come from the password manager). Stop `api`, `ui` and the workers; keep `postgres` and `minio` running.
2. `restore.sh --restore-postgres <dir> --i-understand-this-overwrites-live-data`. If roles were customised, also apply `postgres-globals.sql` from `--extract`.
3. MinIO:
   - Run `restore.sh --extract <dir> /root/restore`.
   - Unpack `minio-full.tar`, then each later `minio-incremental.tar`, in date order, into one directory.
   - Push the directory back to the bucket. Run this on the app host:

     ```bash
     docker run --rm --network buzz-app \
       -v /root/restore/m:/m \
       -e MC_HOST_buzz=http://USER:PASS@minio:9000 \
       buzz/mc:RELEASE.2025-08-13T08-35-41Z \
       mirror /m/voice-audio buzz/voice-audio
     ```
4. Suppression: stop the service, copy `suppression.sqlite` over its database file, then remove any `-wal` and `-shm` files and start it again.
5. Asterisk: unpack `asterisk.tar.gz` into the container's `/` (or the bind-mounted paths), then run `core reload`.
6. Run `retention.sh` (dry run) **before** starting Dograh. A restore brings back data that the retention job had already purged after the backup was taken. Then run it with `--apply`.
7. `shred -u` the plaintext under `/root/restore`.

## Retention (PLAN section 7)

### Where Dograh v1.49.0 keeps call data

The source was read at tag `dograh-v1.49.0`.

| Data | Location | Source |
|---|---|---|
| Mixed call recording | MinIO `recordings/{run_id}.wav`; `workflow_runs.recording_url` | `api/services/workflow_run_artifacts.py` |
| Per-side recordings | `recordings/{run_id}/user.wav`, `…/bot.wav`; `workflow_runs.extra->recordings->{mixed,user,bot}` (string or `{storage_key}`) | same; `api/utils/recording_artifacts.py` |
| Transcript file | `transcripts/{run_id}.txt`; `workflow_runs.transcript_url` | same |
| Transcript turns (DB) | `workflow_runs.logs->realtime_feedback_events` | `api/services/pipecat/event_handlers.py`, `transcript_log_coordinator.py`, `qa/analysis.py` |
| Extracted variables, call status | `workflow_runs.gathered_context` | `api/db/models.py` `WorkflowRunModel`; `campaign_client.py`, `reports_client.py` |
| Caller number and contact fields | `workflow_runs.initial_context` (e.g. `phone_number`) | `reports_client.py` line 76 |
| QA output | `workflow_runs.annotations` | models |
| Text-chat sessions | `workflow_run_text_sessions.session_data` (cascade on run) | models |
| Webhook payloads (n8n) | `webhook_deliveries.payload` | models |
| Widget client IP | `embed_sessions.client_ip` | models |
| Campaign contacts | `queued_runs.context_variables`; uploaded CSVs under MinIO `campaigns/{org_id}/…` | models; `api/routes/s3_signed_url.py` |
| Voicemail-detection audio | `voicemail_detections/…` (read path only in 1.49.0) | `s3_signed_url.py` |
| **Prompt audio, not call data** | `recordings/{org_id}/{recording_id}/{file}`; table `workflow_recordings` | `api/routes/workflow_recording.py` |

The last row is the trap. The disclosure and other pre-recorded prompts use the **same `recordings/` prefix** as call recordings. A plain `mc ilm rule add --prefix recordings/ --expire-days 90` would delete the disclosure audio after 90 days, so `retention.sh` does not do that.

### What `retention.sh --apply` does

1. **MinIO lifecycle.** It imports the following rules, with fixed IDs, using `mc ilm rule import buzz/voice-audio < lifecycle.json`. Running it again is harmless. The equivalent `mc ilm` commands are:

   ```bash
   mc ilm rule add --prefix "transcripts/"          --expire-days 90 buzz/voice-audio
   mc ilm rule add --prefix "voicemail_detections/" --expire-days 90 buzz/voice-audio
   mc ilm rule add --tags "buzz-class=call-recording" --expire-days 90 buzz/voice-audio
   ```

   `rule import` replaces the bucket's whole lifecycle configuration. Dograh sets none.
2. **Tagging.** Each night it tags the call recordings of the last 3 days (`TAG_WINDOW_DAYS`) with `buzz-class=call-recording`. Keys come from the database and must match the call-artefact patterns. Keys in `workflow_recordings.storage_key` are excluded. Run it once with `--backfill-tags` at install time.
3. **Sweep.** For runs older than 90 days that are not yet marked, it does the following, in up to 2,000 runs per night:
   - It deletes their objects explicitly. If any delete fails, it stops before touching the database.
   - It then anonymises the database, in **one transaction**:
     - In `workflow_runs`:
       - `logs`, `annotations` and `initial_context` become `{}`.
       - `gathered_context` is reduced to `call_id`, `call_disposition`, `call_status`, `call_tags` and `call_initiation_uncertain`, so campaign reports still work.
       - `recording_url`, `transcript_url` and `public_access_token` become NULL.
       - `extra.recordings` is removed, and `extra.buzz_retention.anonymised_at` is set.
     - `workflow_run_text_sessions` rows are deleted.
     - `webhook_deliveries.payload` and `custom_headers` are cleared.
     - `embed_sessions.client_ip` is cleared.
     - `queued_runs.context_variables` is cleared for processed or failed rows older than 90 days.

   Without `--apply`, the same SQL runs and is **rolled back**, and the row counts are printed.
4. **Asterisk.** It deletes files under `monitor/`, `recording/` and `voicemail/` older than 90 days.
5. **Metric.** It writes `buzz_retention_last_success_timestamp_seconds`.

The SQL was tested against a minimal copy of these tables on PostgreSQL 16. Run it as a dry run on staging with real Dograh data before enabling the timer.

### For the DPIA

- **Backups extend retention.** A recording deleted on day 90 can survive in the newest weekly full for up to 8 more weeks, so up to about 146 days in total. Either record this in the DPIA or shorten `KEEP_WEEKLY`.
- **Langfuse traces** contain full transcripts and are outside this job. Give Langfuse its own retention setting.
- **Anonymise or delete?** This job anonymises, because it keeps run rows for statistics. Whether that is enough, or whether rows must be deleted, is a DPIA decision. Deleting is not implemented.
- **Campaign CSVs** under `campaigns/` hold contact lists and are not expired yet. Decide how long they are kept.

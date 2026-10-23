# Local test stack (one Mac)

Runs Buzz on a laptop for testing: browser calls on localhost, no telephony.
Tuned for a 16 GB Mac. Nothing leaves the machine once the models are downloaded.

## Start

```sh
brew install ollama colima docker docker-compose   # once
colima start --cpu 4 --memory 3.5 --disk 60
./scripts/up.sh
```

`up.sh` is idempotent. It writes `.env` (git-ignored) with fresh secrets, pulls
Qwen2.5 3B into Ollama, starts the containers, fetches the speech models,
warms them up, connects Dograh to the local models and imports the front-desk agent.

For a 32 GB Mac: `BASE_MODEL=qwen2.5:7b-instruct ./scripts/up.sh`.

## Use

1. Open <http://localhost:3010> and sign in as `admin@bongohive.co.zm`; the password is `DOGRAH_ADMIN_PASSWORD` in `.env`.
2. Open **Buzz front desk (inbound)** and use **Test Audio**.
3. Allow the microphone. Calls relay through coturn via `turn.localhost`.

"Create agent" in the UI needs Dograh's cloud and fails by design. Add agents with
`python3 ../../tools/dograh/import_workflow.py <workflow.json> --env-file .env`.

## Ports (all on 127.0.0.1)

| Port | Service |
| --- | --- |
| 3010 | Dograh UI |
| 8000 | Dograh API |
| 8001 | Speaches (speech to text, text to speech) |
| 8090 | Suppression service |
| 11434 | Ollama (runs on the host, not in Docker) |

## Stop and reset

```sh
docker compose down        # stop, keep data
docker compose down -v     # wipe data
```

## Troubleshooting

- `docker is not running`: run `colima start --cpu 4 --memory 3.5`.
- A container restarting: `docker compose ps`, then `docker compose logs <service>`.
- `TTS context ... completed with no audio`: Dograh waited only 3 s for Piper. `patches/service_factory.py` (mounted into the api container) raises that to 30 s, so a slow sentence now arrives late instead of being dropped. Slow sentences themselves come from the next point.
- Laggy or choppy voice: the Mac is swapping (`sysctl vm.swapusage`; over about 2.5 GB used is bad). Quit Chrome and other heavy apps, or reboot, then retest. The Docker VM is set to 3.5 GB (`colima start --cpu 4 --memory 3.5`) to leave room for Ollama.
- Slow first reply: the model is loading; `up.sh` keeps it resident, but a restart of Ollama clears that.

# Buzz prompts for Asterisk

Asterisk plays these files itself, without the LLM or TTS. That keeps the disclosure fixed and non-interruptible (PLAN.md section 7) and lets transfers work while staff are screened.

The files go in `infra/asterisk/sounds/buzz/`. Compose mounts that folder read-only at `/var/lib/asterisk/sounds/buzz`. The folder is git-ignored. Keep approved recordings in the shared drive, with the approval date, and copy them onto the server.

## Format

- **8 kHz, mono, G.711 μ-law, headerless, extension `.ulaw`.** This is the format the whole call path uses, so Asterisk does not transcode.
- An 8 kHz mono 16-bit PCM `.wav` also works (`format_wav`). Asterisk prefers whichever file costs least to play.
- Don't add the extension in the dialplan: `Playback(/var/lib/asterisk/sounds/buzz/disclosure-inbound)`.
- Peak level about −3 dBFS. Leave no more than 200 ms of silence at the start and end.
- Convert a studio recording like this:
  ```
  ffmpeg -i in.wav -ar 8000 -ac 1 -f mulaw disclosure-inbound.ulaw
  # or
  sox in.wav -r 8000 -c 1 -t ul disclosure-inbound.ulaw
  ```

## Files

| File | Heard by | When | Required | Notes |
|---|---|---|---|---|
| `disclosure-inbound` | caller | after answer, before the agent (`buzz-inbound`) | **yes** | en-GB, recorded, approved wording. Asterisk refuses to start without it, and a call is dropped (cause 41) if it fails to play. |
| `disclosure-outbound` | called person | after they answer, before the agent (`buzz-sub-callee-disclosure`) | **yes** | **8 seconds or less.** Dograh's ARI originate times out at 30 s, and ringing (`OUTBOUND_RING_TIMEOUT`, 18 s) plus this prompt must fit inside that. |
| `service-unavailable` | caller | Dograh did not pick up an inbound call | no | |
| `transfer-intro` | staff | staff answer a transfer | no | e.g. "BongoHive AI desk. I have a caller for you." |
| `transfer-topic-<topic>` | staff | after the intro, if the transfer destination has a topic | no | `<topic>` is `[a-z0-9_]`, up to 32 characters. It must match the topic token in the Dograh transfer destination (`Local/1001*events@buzz-transfer/n`). Suggested topics: `programmes`, `events`, `membership`, `partnerships`, `general`. |
| `transfer-press-1` | staff | screening menu, played up to twice | no | "Press 1 to take the call. Press 2 and they can leave a message." |
| `transfer-declined-staff` | staff | after declining | no | |
| `transfer-leave-message` | caller | decline or no answer | no | Asterisk plays its standard beep after this (from the core sounds). |
| `transfer-message-thanks` | caller | after recording | no | |

Only the two disclosures are mandatory. If an optional prompt is missing, Asterisk logs a warning and carries on.

## Disclosure wording (draft, needs approval)

- **Inbound:** "Hello, you've reached BongoHive. You're speaking with an AI assistant, and this call may be recorded."
- **Outbound:** "Hello, this is BongoHive's AI assistant calling. This call may be recorded."

The final wording comes from the DPIA. It should say what the opt-out is ("say 'stop calling me' at any time"), if the DPO wants that in the disclosure itself rather than left to the agent.

## Placeholders for staging

`generate_placeholders.sh` asks Speaches (Kokoro, voice `bf_emma`) to speak every prompt above, then converts each one to `.ulaw`:

```
cd infra/asterisk/sounds
SPEACHES_URL=http://<inference-host>:8001 \
SPEACHES_API_KEY=<key> \
TTS_VOICE=bf_emma \
./generate_placeholders.sh          # FORCE=1 to overwrite existing files
```

The script needs `curl` and `ffmpeg` or `sox`, and must run from a machine that can reach the inference host on the LAN. It requests `response_format: "wav"` from `/v1/audio/speech` (**verify** that Speaches 0.9.0-rc.3 returns WAV for Kokoro). Placeholders are for staging only. Production uses a human recording of the approved wording.

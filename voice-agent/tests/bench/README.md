# Inference benchmark

Async latency benchmark for the inference host (PLAN section 3). It runs against vLLM and Speaches at 1, 5 and 10 concurrent requests and reports p50 and p95 for:

| Target | Metric | What is timed |
|---|---|---|
| `llm` | `ttft` | Request sent → first generated token in the streamed chat completion. The role-only first chunk does not count. `total` is the full response. |
| `stt` | `latency` | Multipart upload → complete JSON transcript from `/v1/audio/transcriptions`. |
| `tts` | `ttfb` | Request sent → first audio byte from `/v1/audio/speech` (`response_format: pcm`). `total` is the full audio. |

Each level runs `concurrency × rounds` requests (default `rounds` is 5) after one unmeasured warm-up request. The results are printed as a table and written to `results/bench-<UTC time>.json`, which also stores every raw latency, the first errors and sample outputs.

## STT audio matches what Dograh sends

Traced in Dograh `v1.49.0` and its pipecat submodule (`dograh-hq/pipecat` at `93f62ca9`):

1. Asterisk `chan_websocket` sends G.711 μ-law at 8 kHz (`pipecat/serializers/asterisk.py`).
2. The serializer decodes it with `audioop.ulaw2lin` and resamples to the pipeline rate (`pipecat/audio/utils.py`, `ulaw_to_pcm`). For ARI the pipeline rate is also 8000: `transport_sample_rate=8000` in `api/services/telephony/providers/ari/__init__.py`, used for every rate by `create_audio_config` in `api/services/pipecat/audio_config.py`. So there is **no resampling**.
3. `SegmentedSTTService` appends 0.5 s of silence (`trailing_silence_secs`) and wraps the segment with `pcm_to_wav(pcm, 8000)` (`pipecat/services/stt_service.py`).
4. `OpenAISTTService._transcribe` posts multipart `file=("audio.wav", …, "audio/wav")`, `model` and `language` (`pipecat/services/openai/stt.py`, via `pipecat/services/speaches/stt.py`). Dograh builds it in `api/services/pipecat/service_factory.py` with `sample_rate=audio_config.transport_in_sample_rate`.

`buzzbench/audio.py` repeats steps 2 and 3 on 8 kHz μ-law fixtures, and `bench.py` posts the same multipart fields. TTS uses the same body as pipecat's `SpeachesTTSService.run_tts`: `model`, `input`, `voice`, `response_format: "pcm"` and `speed`.

## Setup

```bash
cd tests/bench
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q          # unit tests; no servers needed
```

## Fixtures

The STT fixtures are mono μ-law WAVs at 8 kHz in `fixtures/`. They are generated, not committed. No third-party audio is used.

```bash
export INFERENCE_HOST=10.0.0.20 SPEACHES_API_KEY=...   # LAN IP of the inference host (example)

python make_fixtures.py                  # Kokoro TTS of six caller-style sentences, put through an 8 kHz μ-law "trunk"
python make_fixtures.py --from-wav DIR   # your own recordings, any rate (optional DIR/<name>.txt holds the reference text)
python make_fixtures.py --synthetic      # tone bursts, no server needed; only checks the plumbing
```

TTS fixtures are clean speech, so they flatter Whisper. For the Phase 1 WER and latency sign-off, also use consented recordings of real BongoHive calls (`--from-wav`). Keep those out of git: `.gitignore` already excludes `fixtures/*.wav`.

## Run

Run from the app host or another machine on the LAN, so that the network path is real.

```bash
export INFERENCE_HOST=10.0.0.20 VLLM_API_KEY=... SPEACHES_API_KEY=...
python bench.py --label "L4 24GB, preset 24gb"
python bench.py --targets llm --concurrency 1,5,10,16 --system-prompt-file ../../agents/<agent>/global_prompt.md
```

Options: `--targets`, `--concurrency`, `--rounds`, `--llm-max-tokens`, `--tts-voice`, `--timeout`, `--out`, and the URL, key and model overrides (`--help`). The defaults come from the same variable names as `infra/inference/.env`.

The default system prompt is short. Use `--system-prompt-file` with the real Global node prompt (about 3k tokens with the inline knowledge base) for numbers that reflect production prefill. Prefix caching makes repeat requests cheaper, which matches production.

The exit code is 1 if any measured request failed.

## Layout

| Path | Contents |
|---|---|
| `bench.py` | CLI and request drivers |
| `make_fixtures.py` | Fixture generator |
| `buzzbench/audio.py` | G.711 μ-law codec (checked bit for bit against `audioop`), band-limited resampler, WAV read/write, the Dograh STT payload |
| `buzzbench/stats.py` | Percentiles (numpy "linear" definition) and the table |
| `buzzbench/streaming.py` | SSE parsing and the first-token test |
| `tests/` | Unit tests for the above |

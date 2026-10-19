#!/usr/bin/env python3
"""Latency benchmark for the Buzz inference host (PLAN section 3).

Measures, at each concurrency level (default 1, 5, 10):
  llm  time to first token of a streamed chat completion   (vLLM)
  stt  full response time of /v1/audio/transcriptions       (Speaches)
       on 8 kHz mu-law audio converted the way Dograh posts it
  tts  time to first audio byte of /v1/audio/speech         (Speaches)

Prints a p50/p95 table and writes JSON. See README.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

from buzzbench.audio import DOGRAH_TELEPHONY_RATE, dograh_stt_wav, read_wav, to_telephone_ulaw
from buzzbench.stats import format_table, summarise
from buzzbench.streaming import chunk_has_output, sse_data

HERE = Path(__file__).resolve().parent

DEFAULT_SYSTEM_PROMPT = (
    "You are Buzz, the phone receptionist for BongoHive, an innovation and technology hub "
    "in Lusaka, Zambia. Answer in one or two short spoken sentences. Be warm and plain. "
    "If you do not know, offer to take a message. Never ask for PINs, card numbers or "
    "national ID numbers."
)

LLM_QUESTIONS = [
    "What time do you open on Saturdays?",
    "How do I apply for the next startup programme?",
    "Can I book a meeting room for tomorrow afternoon?",
    "Is there an event for women in tech this month?",
    "I'd like to speak to someone about membership plans.",
    "Where exactly are you located?",
    "Do you offer coding classes for young people?",
    "How much is a hot desk per month?",
]

TTS_SENTENCES = [
    "Good morning, thank you for calling BongoHive. How can I help you today?",
    "Our offices are open from eight in the morning until five in the afternoon.",
    "I can take a message and ask a member of the team to call you back.",
    "The next programme opens for applications at the start of next month.",
]


@dataclass
class LevelResult:
    latencies: list[float] = field(default_factory=list)
    totals: list[float] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    samples: list[str] = field(default_factory=list)


class Bench:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.llm_headers = {"Authorization": f"Bearer {args.vllm_api_key}"}
        self.sp_headers = {"Authorization": f"Bearer {args.speaches_api_key}"}
        self.system_prompt = (
            Path(args.system_prompt_file).read_text(encoding="utf-8")
            if args.system_prompt_file
            else DEFAULT_SYSTEM_PROMPT
        )
        self.stt_payloads: list[tuple[str, bytes]] = []

    # --- payloads -------------------------------------------------------------

    def load_stt_fixtures(self) -> None:
        files = sorted(Path(self.args.fixtures).glob("*.wav"))
        if not files:
            raise SystemExit(
                f"no WAV fixtures in {self.args.fixtures}; run make_fixtures.py first"
            )
        for f in files:
            wav = read_wav(f)
            if wav.is_ulaw and wav.sample_rate == DOGRAH_TELEPHONY_RATE and wav.channels == 1:
                ulaw = wav.data
            else:
                # Not yet telephone audio: put it through the simulated trunk.
                ulaw = to_telephone_ulaw(wav.to_mono_pcm16(), wav.sample_rate)
            self.stt_payloads.append((f.name, dograh_stt_wav(ulaw)))

    # --- one request per target ------------------------------------------------

    async def llm_once(self, client: httpx.AsyncClient, i: int) -> tuple[float, float, str]:
        body = {
            "model": self.args.llm_model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": LLM_QUESTIONS[i % len(LLM_QUESTIONS)]},
            ],
            "max_tokens": self.args.llm_max_tokens,
            "temperature": 0.3,
            "stream": True,
        }
        t0 = time.perf_counter()
        ttft = None
        async with client.stream(
            "POST", f"{self.args.llm_url}/chat/completions", json=body, headers=self.llm_headers
        ) as r:
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}: {(await r.aread())[:200]!r}")
            async for line in r.aiter_lines():
                payload = sse_data(line)
                if payload is None:
                    continue
                if ttft is None and chunk_has_output(payload):
                    ttft = time.perf_counter() - t0
                if payload == "[DONE]":
                    break
        total = time.perf_counter() - t0
        if ttft is None:
            raise RuntimeError("stream ended without any token")
        return ttft, total, ""

    async def stt_once(self, client: httpx.AsyncClient, i: int) -> tuple[float, float, str]:
        name, wav = self.stt_payloads[i % len(self.stt_payloads)]
        # Same multipart fields as pipecat OpenAISTTService._transcribe.
        files = {"file": ("audio.wav", wav, "audio/wav")}
        data = {"model": self.args.stt_model, "language": self.args.language}
        t0 = time.perf_counter()
        r = await client.post(
            f"{self.args.speaches_url}/audio/transcriptions",
            files=files,
            data=data,
            headers=self.sp_headers,
        )
        dt = time.perf_counter() - t0
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        text = r.json().get("text", "")
        return dt, dt, f"{name}: {text.strip()}"

    async def tts_once(self, client: httpx.AsyncClient, i: int) -> tuple[float, float, str]:
        # Same body as pipecat SpeachesTTSService.run_tts.
        body = {
            "model": self.args.tts_model,
            "input": TTS_SENTENCES[i % len(TTS_SENTENCES)],
            "voice": self.args.tts_voice,
            "response_format": "pcm",
            "speed": 1.0,
        }
        t0 = time.perf_counter()
        ttfb = None
        n = 0
        async with client.stream(
            "POST", f"{self.args.speaches_url}/audio/speech", json=body, headers=self.sp_headers
        ) as r:
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}: {(await r.aread())[:200]!r}")
            async for chunk in r.aiter_bytes():
                if chunk:
                    if ttfb is None:
                        ttfb = time.perf_counter() - t0
                    n += len(chunk)
        total = time.perf_counter() - t0
        if ttfb is None:
            raise RuntimeError("empty audio")
        return ttfb, total, f"{n} bytes"

    # --- driving ---------------------------------------------------------------

    async def run_level(self, target: str, concurrency: int) -> LevelResult:
        fn = {"llm": self.llm_once, "stt": self.stt_once, "tts": self.tts_once}[target]
        total_requests = concurrency * self.args.rounds
        res = LevelResult()
        counter = iter(range(total_requests))
        limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
        timeout = httpx.Timeout(self.args.timeout)
        async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
            # Warm-up request, not measured.
            try:
                await fn(client, 0)
            except Exception as e:  # noqa: BLE001
                res.errors.append(f"warm-up: {e}")

            async def worker() -> None:
                for i in counter:
                    try:
                        first, total, sample = await fn(client, i)
                        res.latencies.append(first)
                        res.totals.append(total)
                        if sample and len(res.samples) < 3:
                            res.samples.append(sample)
                    except Exception as e:  # noqa: BLE001
                        res.errors.append(f"{type(e).__name__}: {e}")

            await asyncio.gather(*(worker() for _ in range(concurrency)))
        return res


METRIC = {"llm": "ttft", "stt": "latency", "tts": "ttfb"}


async def main_async(args: argparse.Namespace) -> int:
    bench = Bench(args)
    targets = [t.strip() for t in args.targets.split(",") if t.strip()]
    if "stt" in targets:
        bench.load_stt_fixtures()
    levels = [int(c) for c in args.concurrency.split(",")]

    rows: list[dict] = []
    detail: list[dict] = []
    for target in targets:
        for c in levels:
            print(f"... {target} at concurrency {c}", file=sys.stderr, flush=True)
            t0 = time.perf_counter()
            res = await bench.run_level(target, c)
            wall = time.perf_counter() - t0
            measured_errors = [e for e in res.errors if not e.startswith("warm-up")]
            row = {"target": target, "metric": METRIC[target], "concurrency": c}
            row.update(summarise(res.latencies, len(measured_errors)))
            rows.append(row)
            if target in ("llm", "tts"):
                total = {"target": target, "metric": "total", "concurrency": c}
                total.update(summarise(res.totals, len(measured_errors)))
                rows.append(total)
            detail.append(
                {
                    "target": target,
                    "concurrency": c,
                    "wall_s": round(wall, 2),
                    "latencies_ms": [round(v * 1000, 1) for v in res.latencies],
                    "totals_ms": [round(v * 1000, 1) for v in res.totals],
                    "errors": res.errors[:20],
                    "samples": res.samples,
                }
            )

    table = format_table(rows)
    print(table)

    out = Path(args.out) if args.out else (
        HERE / "results" / f"bench-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "meta": {
            "time_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "client_host": platform.node(),
            "llm_url": args.llm_url,
            "speaches_url": args.speaches_url,
            "llm_model": args.llm_model,
            "stt_model": args.stt_model,
            "tts_model": args.tts_model,
            "tts_voice": args.tts_voice,
            "rounds": args.rounds,
            "llm_max_tokens": args.llm_max_tokens,
            "stt_audio": "G.711 mu-law 8 kHz -> PCM16 8 kHz + 0.5 s silence, WAV (as Dograh/ARI)",
            "fixtures": [n for n, _ in bench.stt_payloads],
            "label": args.label,
        },
        "results": rows,
        "detail": detail,
    }
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nwrote {out}", file=sys.stderr)
    return 1 if any(r["errors"] for r in rows) else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    host = os.environ.get("INFERENCE_HOST", "127.0.0.1")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--llm-url", default=os.environ.get("LLM_URL", f"http://{host}:8000/v1"))
    p.add_argument("--speaches-url", default=os.environ.get("SPEACHES_URL", f"http://{host}:8001/v1"))
    p.add_argument("--vllm-api-key", default=os.environ.get("VLLM_API_KEY", ""))
    p.add_argument("--speaches-api-key", default=os.environ.get("SPEACHES_API_KEY", ""))
    p.add_argument("--llm-model", default=os.environ.get("LLM_MODEL_NAME", "buzz-llm"))
    p.add_argument("--stt-model", default=os.environ.get("STT_MODEL", "deepdml/faster-whisper-large-v3-turbo-ct2"))
    p.add_argument("--tts-model", default=os.environ.get("TTS_MODEL", "speaches-ai/Kokoro-82M-v1.0-ONNX"))
    p.add_argument("--tts-voice", default=os.environ.get("TTS_VOICE", "bf_emma"))
    p.add_argument("--language", default="en")
    p.add_argument("--targets", default="llm,stt,tts", help="comma list of llm, stt, tts")
    p.add_argument("--concurrency", default="1,5,10", help="comma list of levels")
    p.add_argument("--rounds", type=int, default=5, help="requests per worker at each level")
    p.add_argument("--llm-max-tokens", type=int, default=64)
    p.add_argument("--system-prompt-file", help="use the real Global node prompt instead of the short default")
    p.add_argument("--fixtures", default=str(HERE / "fixtures"))
    p.add_argument("--timeout", type=float, default=60.0)
    p.add_argument("--out", help="JSON path (default results/bench-<UTC time>.json)")
    p.add_argument("--label", default="", help="free text stored in the JSON, e.g. GPU and preset")
    args = p.parse_args(argv)
    for name in ("llm_url", "speaches_url"):
        setattr(args, name, getattr(args, name).rstrip("/"))
    return args


def main() -> int:
    return asyncio.run(main_async(parse_args()))


if __name__ == "__main__":
    sys.exit(main())

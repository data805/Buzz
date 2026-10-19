#!/usr/bin/env python3
"""Create the STT benchmark fixtures: mono G.711 mu-law WAVs at 8 kHz.

No third-party or copyrighted audio is used. Three sources:

  --from-tts         (default) synthesise caller-style sentences with the
                     Speaches TTS (Kokoro, Apache-2.0), then band-limit to
                     8 kHz and mu-law encode them like a phone trunk.
  --from-wav DIR     convert your own WAV recordings (PCM16 or mu-law, any
                     rate), e.g. consented BongoHive test calls. Keep those
                     out of git.
  --synthetic        speech-shaped tone bursts; needs no server. Only for
                     checking the plumbing: Whisper will return little or no
                     text for it.

Writes fixtures/*.wav and fixtures/manifest.json (reference text, if known).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import struct
import sys
from pathlib import Path

from buzzbench.audio import (
    DOGRAH_TELEPHONY_RATE,
    read_wav,
    to_telephone_ulaw,
    ulaw_wav_bytes,
)

HERE = Path(__file__).resolve().parent

# Kokoro's native output rate (speaches executors/kokoro.py SAMPLE_RATE).
KOKORO_RATE = 24000

CALLER_LINES = [
    "Hello, I'm calling to ask about your opening hours this weekend.",
    "Hi, my name is Chanda. I'd like to book a meeting room for Thursday.",
    "Good afternoon. How do I apply for the startup incubation programme?",
    "Can you tell me how much a hot desk costs per month, please?",
    "I want to speak to someone about the coding class for my daughter.",
    "Yes, please take a message. My number is the one I'm calling from.",
]


def synth_tts(args: argparse.Namespace) -> list[tuple[str, bytes, str]]:
    import httpx

    key = args.speaches_api_key or os.environ.get("SPEACHES_API_KEY", "")
    headers = {"Authorization": f"Bearer {key}"}
    out = []
    with httpx.Client(timeout=60) as client:
        for i, text in enumerate(CALLER_LINES, 1):
            r = client.post(
                f"{args.speaches_url.rstrip('/')}/audio/speech",
                headers=headers,
                json={
                    "model": args.tts_model,
                    "input": text,
                    "voice": args.voice,
                    "response_format": "pcm",
                },
            )
            r.raise_for_status()
            out.append((f"tts-{i:02d}.wav", to_telephone_ulaw(r.content, KOKORO_RATE), text))
            print(f"  tts-{i:02d}.wav  {text}", file=sys.stderr)
    return out


def convert_dir(src: Path) -> list[tuple[str, bytes, str]]:
    out = []
    for f in sorted(src.glob("*.wav")):
        wav = read_wav(f)
        if wav.is_ulaw and wav.sample_rate == DOGRAH_TELEPHONY_RATE and wav.channels == 1:
            ulaw = wav.data
        else:
            ulaw = to_telephone_ulaw(wav.to_mono_pcm16(), wav.sample_rate)
        ref = f.with_suffix(".txt")
        out.append((f"rec-{f.stem}.wav", ulaw, ref.read_text().strip() if ref.exists() else ""))
        print(f"  rec-{f.stem}.wav", file=sys.stderr)
    return out


def synthetic_pcm(seconds: float = 2.0, rate: int = 16000) -> bytes:
    """Deterministic voiced-sounding bursts: 140 Hz harmonics with a syllable envelope."""
    n = int(seconds * rate)
    samples = []
    for i in range(n):
        t = i / rate
        env = max(0.0, math.sin(2 * math.pi * 3.0 * t)) ** 2  # ~3 syllables per second
        f0 = 140 + 20 * math.sin(2 * math.pi * 0.7 * t)
        v = sum(math.sin(2 * math.pi * f0 * h * t) / h for h in range(1, 12))
        samples.append(int(max(-1.0, min(1.0, 0.25 * env * v)) * 32767))
    return struct.pack(f"<{n}h", *samples)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--from-tts", action="store_true", help="default")
    mode.add_argument("--from-wav", type=Path, metavar="DIR")
    mode.add_argument("--synthetic", action="store_true")
    host = os.environ.get("INFERENCE_HOST", "127.0.0.1")
    p.add_argument("--speaches-url", default=os.environ.get("SPEACHES_URL", f"http://{host}:8001/v1"))
    p.add_argument("--speaches-api-key", default="")
    p.add_argument("--tts-model", default=os.environ.get("TTS_MODEL", "speaches-ai/Kokoro-82M-v1.0-ONNX"))
    p.add_argument("--voice", default="bf_isabella", help="a different voice from the agent's own")
    p.add_argument("--out", type=Path, default=HERE / "fixtures")
    args = p.parse_args()

    if args.from_wav:
        items = convert_dir(args.from_wav)
    elif args.synthetic:
        items = [("synthetic-01.wav", to_telephone_ulaw(synthetic_pcm(), 16000), "")]
    else:
        items = synth_tts(args)
    if not items:
        print("nothing to write", file=sys.stderr)
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for name, ulaw, text in items:
        (args.out / name).write_bytes(ulaw_wav_bytes(ulaw))
        manifest[name] = {"text": text, "seconds": round(len(ulaw) / DOGRAH_TELEPHONY_RATE, 2)}
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"wrote {len(items)} fixture(s) to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Audio helpers: G.711 mu-law, resampling and WAV files.

Reproduces what Dograh v1.49.0 posts to Speaches for an Asterisk (ARI) call,
traced through the source:

1. Asterisk chan_websocket sends raw G.711 mu-law at 8 kHz
   (pipecat fork 93f62ca9, ``src/pipecat/serializers/asterisk.py``).
2. The serializer decodes it with ``audioop.ulaw2lin(..., 2)`` and resamples
   from ``asterisk_sample_rate`` to the pipeline rate
   (``src/pipecat/audio/utils.py``, ``ulaw_to_pcm``). For ARI both are 8000:
   ``transport_sample_rate=8000`` in
   ``api/services/telephony/providers/ari/__init__.py``, and
   ``create_audio_config`` in ``api/services/pipecat/audio_config.py`` sets
   transport, VAD and pipeline rates all to that value. So there is no
   resampling: 16-bit PCM, mono, 8 kHz.
3. ``SegmentedSTTService`` buffers the utterance, appends
   ``trailing_silence_secs`` (default 0.5 s) of zeros and wraps it with
   ``pcm_to_wav(pcm, sample_rate)`` (``src/pipecat/services/stt_service.py``).
4. ``OpenAISTTService._transcribe`` posts it as multipart
   ``file=("audio.wav", <wav>, "audio/wav")`` plus ``model`` and ``language``
   to ``{base_url}/audio/transcriptions``
   (``src/pipecat/services/openai/stt.py``; Speaches subclass in
   ``src/pipecat/services/speaches/stt.py``; built in Dograh
   ``api/services/pipecat/service_factory.py`` with
   ``sample_rate=audio_config.transport_in_sample_rate``).

Pure Python (no numpy, no audioop) so the unit tests run anywhere. The mu-law
codec is the classic Sun/CCITT G.711 one and is checked bit for bit against
``audioop`` in the tests.
"""

from __future__ import annotations

import io
import math
import struct
import sys
import wave
from array import array
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

DOGRAH_TELEPHONY_RATE = 8000
DOGRAH_TRAILING_SILENCE_SECS = 0.5

WAVE_FORMAT_PCM = 1
WAVE_FORMAT_MULAW = 7

_BIAS = 0x84
_CLIP14 = 8159  # clip for the 14-bit magnitude
_SEG_UEND = (0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF)


def _linear_to_ulaw(sample: int) -> int:
    """Encode one signed 16-bit sample (G.711 mu-law).

    Same algorithm as CPython audioop ``st_14linear2ulaw(sample >> 2)``, which
    is what ``audioop.lin2ulaw(..., 2)`` does.
    """
    pcm = sample >> 2
    if pcm < 0:
        pcm = -pcm
        mask = 0x7F
    else:
        mask = 0xFF
    pcm = min(pcm, _CLIP14) + (_BIAS >> 2)
    seg = 0
    while seg < 8 and pcm > _SEG_UEND[seg]:
        seg += 1
    if seg >= 8:
        return 0x7F ^ mask
    return ((seg << 4) | ((pcm >> (seg + 1)) & 0x0F)) ^ mask


def _ulaw_to_linear(code: int) -> int:
    """Decode one mu-law byte to a signed 16-bit sample (as audioop.ulaw2lin)."""
    u = ~code & 0xFF
    t = ((u & 0x0F) << 3) + _BIAS
    t <<= (u & 0x70) >> 4
    return (_BIAS - t) if (u & 0x80) else (t - _BIAS)


_DECODE_TABLE = array("h", (_ulaw_to_linear(c) for c in range(256)))
_ENCODE_TABLE = bytes(_linear_to_ulaw(s) for s in range(-32768, 32768))


def _samples(pcm: bytes) -> array:
    a = array("h")
    a.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if sys.byteorder == "big":
        a.byteswap()
    return a


def _to_bytes(a: array) -> bytes:
    if sys.byteorder == "big":
        a = array("h", a)
        a.byteswap()
    return a.tobytes()


def lin2ulaw(pcm: bytes) -> bytes:
    """16-bit little-endian PCM to mu-law bytes."""
    return bytes(_ENCODE_TABLE[s + 32768] for s in _samples(pcm))


def ulaw2lin(data: bytes) -> bytes:
    """Mu-law bytes to 16-bit little-endian PCM (what Dograh's serializer does)."""
    return _to_bytes(array("h", (_DECODE_TABLE[b] for b in data)))


# --- Resampling -----------------------------------------------------------------

_TAPS_HALF = 16  # zero crossings each side at the output rate


def _kernel(phase: float, cutoff: float, half: int) -> list[float]:
    """Windowed-sinc (Blackman) taps for offsets -half+1..half around `phase`."""
    taps = []
    for k in range(-half + 1, half + 1):
        x = k - phase
        if x == 0:
            s = 1.0
        else:
            s = math.sin(math.pi * cutoff * x) / (math.pi * cutoff * x)
        w_pos = (x + half) / (2 * half)
        if 0.0 <= w_pos <= 1.0:
            w = 0.42 - 0.5 * math.cos(2 * math.pi * w_pos) + 0.08 * math.cos(4 * math.pi * w_pos)
        else:
            w = 0.0
        taps.append(cutoff * s * w)
    return taps


def resample(pcm: bytes, in_rate: int, out_rate: int) -> bytes:
    """Band-limited resampling of mono 16-bit PCM between any two rates.

    Low-pass at the lower Nyquist frequency, so downsampling to 8 kHz removes
    content above 4 kHz the way a telephone channel would.
    """
    if in_rate <= 0 or out_rate <= 0:
        raise ValueError("sample rates must be positive")
    x = _samples(pcm)
    if in_rate == out_rate or not x:
        return _to_bytes(x)
    step = Fraction(in_rate, out_rate)
    cutoff = min(1.0, out_rate / in_rate)
    half = max(_TAPS_HALF, int(math.ceil(_TAPS_HALF / cutoff)))
    n_out = (len(x) * out_rate) // in_rate
    n_in = len(x)
    cache: dict[Fraction, list[float]] = {}
    out = array("h")
    for n in range(n_out):
        t = n * step
        base = int(t)
        frac = t - base
        taps = cache.get(frac)
        if taps is None:
            taps = cache[frac] = _kernel(float(frac), cutoff, half)
        acc = 0.0
        start = base - half + 1
        for i, c in enumerate(taps):
            j = start + i
            if 0 <= j < n_in:
                acc += x[j] * c
        out.append(max(-32768, min(32767, int(round(acc)))))
    return _to_bytes(out)


# --- WAV ------------------------------------------------------------------------


def pcm_to_wav(pcm: bytes, sample_rate: int, num_channels: int = 1) -> bytes:
    """Wrap 16-bit PCM in a WAV container, as pipecat's ``pcm_to_wav`` does."""
    block_align = 2 * num_channels
    pcm = pcm[: len(pcm) - (len(pcm) % block_align)]
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(num_channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def ulaw_wav_bytes(data: bytes, sample_rate: int = DOGRAH_TELEPHONY_RATE) -> bytes:
    """A mono G.711 mu-law WAV (format tag 7) with a fact chunk."""
    fmt = struct.pack("<HHIIHHH", WAVE_FORMAT_MULAW, 1, sample_rate, sample_rate, 1, 8, 0)
    fact = struct.pack("<I", len(data))
    pad = b"\x00" if len(data) % 2 else b""
    body = (
        b"WAVE"
        + b"fmt " + struct.pack("<I", len(fmt)) + fmt
        + b"fact" + struct.pack("<I", len(fact)) + fact
        + b"data" + struct.pack("<I", len(data)) + data + pad
    )
    return b"RIFF" + struct.pack("<I", len(body)) + body


@dataclass
class WavAudio:
    format_tag: int
    sample_rate: int
    channels: int
    bits: int
    data: bytes

    @property
    def is_ulaw(self) -> bool:
        return self.format_tag == WAVE_FORMAT_MULAW

    def to_mono_pcm16(self) -> bytes:
        """Decode to mono 16-bit PCM at the file's own rate."""
        if self.is_ulaw:
            pcm = ulaw2lin(self.data)
        elif self.format_tag == WAVE_FORMAT_PCM and self.bits == 16:
            pcm = self.data
        else:
            raise ValueError(f"unsupported WAV: format {self.format_tag}, {self.bits} bits")
        if self.channels == 1:
            return pcm
        s = _samples(pcm)
        c = self.channels
        mono = array("h", (int(sum(s[i : i + c]) / c) for i in range(0, len(s) - c + 1, c)))
        return _to_bytes(mono)


def parse_wav(raw: bytes) -> WavAudio:
    """Parse a RIFF/WAVE file with PCM16 or mu-law data.

    Tolerates a data chunk whose declared size runs past the end of the file
    (streamed WAVs).
    """
    if len(raw) < 12 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE file")
    pos = 12
    fmt = None
    while pos + 8 <= len(raw):
        cid = raw[pos : pos + 4]
        size = struct.unpack("<I", raw[pos + 4 : pos + 8])[0]
        body = raw[pos + 8 : pos + 8 + size]
        if cid == b"fmt ":
            tag, ch, rate, _, _, bits = struct.unpack("<HHIIHH", body[:16])
            if tag == 0xFFFE and len(body) >= 26:  # WAVE_FORMAT_EXTENSIBLE
                tag = struct.unpack("<H", body[24:26])[0]
            fmt = (tag, ch, rate, bits)
        elif cid == b"data":
            if fmt is None:
                raise ValueError("data chunk before fmt chunk")
            tag, ch, rate, bits = fmt
            return WavAudio(tag, rate, ch, bits, body)
        pos += 8 + size + (size % 2)
    raise ValueError("no data chunk")


def read_wav(path: str | Path) -> WavAudio:
    return parse_wav(Path(path).read_bytes())


# --- The Dograh path --------------------------------------------------------------


def to_telephone_ulaw(pcm: bytes, in_rate: int) -> bytes:
    """Simulate the trunk: band-limit to 8 kHz and encode as G.711 mu-law."""
    return lin2ulaw(resample(pcm, in_rate, DOGRAH_TELEPHONY_RATE))


def dograh_stt_wav(
    ulaw: bytes,
    trailing_silence_secs: float = DOGRAH_TRAILING_SILENCE_SECS,
    sample_rate: int = DOGRAH_TELEPHONY_RATE,
) -> bytes:
    """The WAV body Dograh posts to /v1/audio/transcriptions for an ARI call.

    mu-law 8 kHz -> PCM16 8 kHz (no resampling for ARI) -> + trailing silence
    -> WAV 8 kHz mono 16-bit.
    """
    pcm = ulaw2lin(ulaw)
    pcm += bytes(int(sample_rate * trailing_silence_secs) * 2)
    return pcm_to_wav(pcm, sample_rate)

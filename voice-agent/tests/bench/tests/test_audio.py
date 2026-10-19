import io
import math
import struct
import wave

import pytest

from buzzbench import audio as A


def sine(freq, rate, seconds, amp=0.5):
    n = int(rate * seconds)
    return struct.pack(f"<{n}h", *(int(amp * 32767 * math.sin(2 * math.pi * freq * i / rate)) for i in range(n)))


def samples(pcm):
    return struct.unpack(f"<{len(pcm) // 2}h", pcm)


def rms(xs):
    return math.sqrt(sum(x * x for x in xs) / len(xs))


def zero_crossings(xs):
    return sum(1 for a, b in zip(xs, xs[1:]) if (a < 0) != (b < 0))


# --- mu-law -------------------------------------------------------------------


def test_ulaw_reference_points():
    # G.711: 0xFF and 0x7F are +0 and -0; 0x00 / 0x80 are the extremes.
    assert samples(A.ulaw2lin(b"\xff\x7f\x00\x80")) == (0, 0, -32124, 32124)
    assert A.lin2ulaw(struct.pack("<h", 0)) == b"\xff"
    assert A.lin2ulaw(struct.pack("<2h", 32767, -32768)) == b"\x80\x00"


def test_ulaw_decode_encode_is_stable():
    for code in range(256):
        if code == 0x7F:  # -0 encodes back as +0
            continue
        assert A.lin2ulaw(A.ulaw2lin(bytes([code]))) == bytes([code])


def test_ulaw_round_trip_error_is_small():
    pcm = sine(440, 8000, 0.1, amp=0.8)
    back = samples(A.ulaw2lin(A.lin2ulaw(pcm)))
    orig = samples(pcm)
    err = rms([a - b for a, b in zip(orig, back)])
    assert err / rms(orig) < 0.03  # mu-law SNR is about 38 dB


def test_ulaw_matches_audioop_bit_for_bit():
    # pipecat decodes with audioop (audioop-lts on Python >= 3.13).
    audioop = pytest.importorskip("audioop")
    every_sample = struct.pack("<65536h", *range(-32768, 32768))
    assert A.lin2ulaw(every_sample) == audioop.lin2ulaw(every_sample, 2)
    every_code = bytes(range(256))
    assert A.ulaw2lin(every_code) == audioop.ulaw2lin(every_code, 2)


# --- resampling ---------------------------------------------------------------


def test_resample_same_rate_is_identity():
    pcm = sine(300, 8000, 0.05)
    assert A.resample(pcm, 8000, 8000) == pcm


@pytest.mark.parametrize("in_rate", [16000, 24000, 44100])
def test_resample_to_8k_keeps_length_and_pitch(in_rate):
    pcm = sine(440, in_rate, 0.5)
    out = samples(A.resample(pcm, in_rate, 8000))
    assert abs(len(out) - 4000) <= 1
    # Ignore the filter edges at both ends.
    # Two zero crossings per cycle.
    expected = 2 * 440 * (len(out) - 400) / 8000
    assert abs(zero_crossings(out[200:-200]) - expected) <= 4
    assert rms(out[200:-200]) == pytest.approx(rms(samples(pcm)), rel=0.05)


def test_resample_removes_content_above_4khz():
    pcm = sine(6000, 24000, 0.25)
    out = samples(A.resample(pcm, 24000, 8000))
    assert rms(out[100:-100]) < 0.02 * rms(samples(pcm))


def test_resample_rejects_bad_rate():
    with pytest.raises(ValueError):
        A.resample(b"\x00\x00", 0, 8000)


# --- WAV ------------------------------------------------------------------------


def test_pcm_to_wav_layout_matches_pipecat():
    pcm = sine(300, 8000, 0.01) + b"\x01"  # odd trailing byte is dropped
    wav = A.pcm_to_wav(pcm, 8000)
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE" and wav[36:40] == b"data"
    tag, ch, rate, byte_rate, align, bits = struct.unpack("<HHIIHH", wav[20:36])
    assert (tag, ch, rate, byte_rate, align, bits) == (1, 1, 8000, 16000, 2, 16)
    assert len(wav) == 44 + len(pcm) - 1
    with wave.open(io.BytesIO(wav)) as w:
        assert w.getframerate() == 8000 and w.getnframes() == 80


def test_ulaw_wav_round_trip():
    data = bytes(range(256)) * 3 + b"\x10"  # odd length exercises the pad byte
    parsed = A.parse_wav(A.ulaw_wav_bytes(data))
    assert parsed.is_ulaw and parsed.sample_rate == 8000 and parsed.channels == 1
    assert parsed.data == data
    assert parsed.to_mono_pcm16() == A.ulaw2lin(data)


def test_parse_wav_reads_stereo_pcm_as_mono():
    frames = struct.pack("<4h", 100, 300, -200, -400)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(frames)
    parsed = A.parse_wav(buf.getvalue())
    assert samples(parsed.to_mono_pcm16()) == (200, -300)


def test_parse_wav_rejects_non_wav():
    with pytest.raises(ValueError):
        A.parse_wav(b"OggS" + bytes(40))


# --- the Dograh path --------------------------------------------------------------


def test_dograh_stt_wav_is_8k_pcm16_with_half_second_silence():
    ulaw = A.lin2ulaw(sine(300, 8000, 1.0))
    wav = A.dograh_stt_wav(ulaw)
    with wave.open(io.BytesIO(wav)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 8000)
        assert w.getnframes() == 8000 + 4000
        frames = w.readframes(w.getnframes())
    assert frames[:16000] == A.ulaw2lin(ulaw)
    assert frames[16000:] == bytes(8000)


def test_to_telephone_ulaw_is_one_byte_per_8k_sample():
    assert len(A.to_telephone_ulaw(sine(300, 16000, 0.25), 16000)) == 2000

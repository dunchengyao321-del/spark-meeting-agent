"""Audio helpers: PCM resampling, level metering, WAV packaging."""

import array
import io
import wave

PCM16_SAMPLE_WIDTH = 2


def pcm_to_samples(pcm: bytes) -> array.array:
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % PCM16_SAMPLE_WIDTH])
    if samples.byteswap is None:  # pragma: no cover
        pass
    return samples


def resample(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Linear-interpolation resample for 16-bit mono PCM."""
    if src_rate == dst_rate or not pcm:
        return pcm
    src = pcm_to_samples(pcm)
    if not src:
        return b""
    ratio = src_rate / dst_rate
    out_len = int(len(src) / ratio)
    out = array.array("h")
    append = out.append
    last = len(src) - 1
    for i in range(out_len):
        pos = i * ratio
        idx = int(pos)
        frac = pos - idx
        if idx >= last:
            value = src[last]
        else:
            value = src[idx] * (1.0 - frac) + src[idx + 1] * frac
        append(int(max(-32768, min(32767, value))))
    return out.tobytes()


def rms_level(pcm: bytes) -> float:
    """RMS amplitude (0..32768 scale) of a PCM16 chunk."""
    if not pcm:
        return 0.0
    samples = pcm_to_samples(pcm)
    if not samples:
        return 0.0
    acc = 0
    for value in samples:
        acc += value * value
    return (acc / len(samples)) ** 0.5


def build_wav(pcm: bytes, rate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(PCM16_SAMPLE_WIDTH)
        wav_file.setframerate(rate)
        wav_file.writeframes(pcm)
    return buffer.getvalue()

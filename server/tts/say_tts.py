"""macOS `say` TTS adapter: zero-config local voice for development.

Replace with a streaming cloud TTS (e.g. Volcano) for production latency;
the interface stays the same.
"""

import asyncio
import subprocess
import tempfile
import uuid
import wave
from pathlib import Path

from server.tts.base import TTSBase, TTS_RATE


class SayTTS(TTSBase):
    name = "say"

    def __init__(self, config: dict):
        self.default_voice = str(config.get("tts_voice", "Tingting"))

    def _run(self, text: str, voice: str) -> bytes:
        with tempfile.TemporaryDirectory(prefix="spark_tts_") as tmp_dir:
            aiff = Path(tmp_dir) / f"{uuid.uuid4().hex}.aiff"
            wav = Path(tmp_dir) / f"{uuid.uuid4().hex}.wav"
            subprocess.run(["say", "-v", voice, "-o", str(aiff), text],
                           check=True, capture_output=True, timeout=30)
            subprocess.run(["afconvert", "-f", "WAVE", "-d", f"LEI16@{TTS_RATE}",
                            "-c", "1", str(aiff), str(wav)],
                           check=True, capture_output=True, timeout=30)
            with wave.open(str(wav), "rb") as wav_file:
                pcm = wav_file.readframes(wav_file.getnframes())
        if not pcm:
            raise RuntimeError(
                "say 未产出音频（后台/无头进程常见），请在设置中改用 TTS 引擎=openai")
        return pcm

    async def synthesize(self, text: str, voice: str | None = None) -> bytes:
        use_voice = voice or self.default_voice
        return await asyncio.to_thread(self._run, text, use_voice)

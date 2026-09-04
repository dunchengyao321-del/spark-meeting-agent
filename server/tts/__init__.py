"""TTS factory: macOS say（离线、零配置）或火山豆包语音（真人级音色）。

`tts_engine` = say（默认）| volcano。选 volcano 但未配 Key 时保留适配器，
让首次发声在控制台报出明确的「未配置火山语音 Key」错误，而不是静默降级。
"""

from server.tts.base import TTSBase, TTS_RATE
from server.tts.say_tts import SayTTS
from server.tts.volcano_tts import VolcanoTTS

__all__ = ["TTSBase", "TTS_RATE", "SayTTS", "VolcanoTTS", "build_tts"]


def build_tts(config: dict) -> TTSBase:
    engine = str(config.get("tts_engine", "say")).split("#", 1)[0].strip().lower()
    if engine == "volcano":
        return VolcanoTTS(config)
    return SayTTS(config)

"""ASR factory: 豆包语音识别大模型（默认）或 Apple 本地识别（离线兜底）。"""

from server.asr.apple_asr import AppleASR
from server.asr.base import ASRBase, ASRResult
from server.asr.volcano_asr import VolcanoASR

__all__ = ["ASRBase", "ASRResult", "AppleASR", "VolcanoASR", "build_asr"]


def build_asr(config: dict) -> ASRBase:
    engine = str(config.get("asr_engine", "")).split("#", 1)[0].strip().lower()
    if engine == "apple":
        return AppleASR(config)
    if engine == "volcano":
        return VolcanoASR(config)
    # 未显式指定：有火山语音 Key 走大模型识别，否则退回本地离线识别
    return VolcanoASR(config) if VolcanoASR(config).configured() else AppleASR(config)

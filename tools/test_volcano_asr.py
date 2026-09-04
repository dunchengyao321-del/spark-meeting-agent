"""火山 seed-ASR 连通性/准确率验证：say 合成中文语音 → 16k PCM → 转写。"""

import asyncio
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.asr.volcano_asr import VolcanoASR  # noqa: E402
from server.config_store import load_config  # noqa: E402

SENTENCES = [
    "星火，帮我总结一下今天的会议内容",
    "履约系统的履约单状态有哪些",
    "查一下 dmall coupon 的券核销逻辑",
]


def synth(text: str) -> bytes:
    aiff = Path("/tmp/spark_asr_test.aiff")
    pcm_path = Path("/tmp/spark_asr_test.pcm")
    subprocess.run(["say", "-v", "Tingting", "-r", "185", "-o", str(aiff), text],
                   check=True, timeout=30)
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(aiff), "-ar", "16000", "-ac", "1",
                    "-f", "s16le", str(pcm_path)], check=True, timeout=30)
    return pcm_path.read_bytes()


async def main() -> int:
    config = load_config()
    asr = VolcanoASR(config)
    if not asr.configured():
        print("FAIL: 未配置火山语音 Key")
        return 1
    asr.set_hotwords(["星火", "履约", "履约单", "dmall", "coupon", "券核销"])
    print(f"resource_id={asr.resource_id} two_pass={asr.two_pass}")
    fails = 0
    for text in SENTENCES:
        pcm = synth(text)
        t0 = time.time()
        try:
            result = await asr.transcribe(pcm, 16000)
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL: {text!r} -> {exc}")
            fails += 1
            continue
        total = (time.time() - t0) * 1000
        ok = "OK " if result.text else "EMPTY"
        print(f"{ok} | 音频 {len(pcm) / 2 / 16000:.1f}s | 耗时 {total:.0f}ms"
              f"（内部 {result.duration_ms}ms）\n     期望: {text}\n     识别: {result.text}")
        if not result.text:
            fails += 1
    print("PASS" if fails == 0 else f"FAIL: {fails} 句失败")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

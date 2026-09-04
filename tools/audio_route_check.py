#!/usr/bin/env python3
"""音频路由自检（P4.0）：飞书桥接发声链路体检。

用法：
  ./.venv/bin/python tools/audio_route_check.py            # 只检查，不出声
  ./.venv/bin/python tools/audio_route_check.py --play     # 合成一句话播放到 BlackHole
  ./.venv/bin/python tools/audio_route_check.py --play --text "链路测试"

检查项：ffmpeg / SwitchAudioSource / BlackHole 设备 / 火山 TTS 配置 / （--play）真实出声。
全部通过退出码 0，任一失败退出码 1。
"""

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from integrations.feishu.bridge import (find_blackhole_device, pcm_to_wav,  # noqa: E402
                                        play_wav_to_blackhole)


def check(label: str, ok: bool, detail: str = "") -> bool:
    mark = "✅" if ok else "❌"
    line = f"{mark} {label}"
    if detail:
        line += f"：{detail}"
    print(line, flush=True)
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="飞书桥接音频路由自检")
    parser.add_argument("--play", action="store_true", help="合成一句话并播放到 BlackHole")
    parser.add_argument("--text", default="大家好，这是星火的音频链路测试。", help="试音文本")
    args = parser.parse_args()

    all_ok = True

    ffmpeg = shutil.which("ffmpeg")
    all_ok &= check("ffmpeg", bool(ffmpeg), ffmpeg or "未安装：brew install ffmpeg")

    switch = shutil.which("SwitchAudioSource")
    all_ok &= check("SwitchAudioSource", bool(switch),
                    switch or "未安装：brew install switchaudio-osx")

    device = find_blackhole_device()
    all_ok &= check("BlackHole 输出设备", bool(device),
                    device or "未检测到：brew install blackhole-2ch 后重启")

    from server.config_store import load_config
    from server.tts import build_tts
    config = load_config()
    tts = build_tts(config)
    all_ok &= check(f"TTS 引擎（{tts.name}）配置", tts.configured() if hasattr(tts, "configured") else True,
                    f"tts_engine={config.get('tts_engine', 'say')}，"
                    f"speaker={config.get('volcano_tts_speaker', '')}")

    if not all_ok:
        print("\n自检未通过：先修复上面的 ❌ 项。")
        return 1

    if not args.play:
        print("\n基础检查通过。加 --play 可真实试音（会播放到 BlackHole → 飞书麦克风）。")
        return 0

    import asyncio

    async def synth() -> bytes:
        warm = getattr(tts, "warm", None)
        if warm:
            await warm()
        pcm = await tts.synthesize(args.text)
        drop = getattr(tts, "_drop", None)
        if drop:
            await drop()
        return pcm

    pcm = asyncio.run(synth())
    if not pcm:
        print("❌ TTS 未产出音频")
        return 1
    wav = str(Path(tempfile.gettempdir()) / "spark_route_check.wav")
    pcm_to_wav(pcm, wav, getattr(tts, "rate", 24000))
    print(f"🔊 播放到 {device}（{len(pcm) // 2 // 24000}s 音频）……")
    try:
        play_wav_to_blackhole(wav)
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 播放失败：{exc}")
        return 1
    finally:
        Path(wav).unlink(missing_ok=True)
    print("✅ 试音完成：如果飞书会议的麦克风选的是 BlackHole 2ch，会中应能听到这句话。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

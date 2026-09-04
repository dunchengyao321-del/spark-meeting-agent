#!/usr/bin/env python3
"""端到端验收：浏览器同款 WS 链路，注入真人合成语音 → 听清 → 唤醒 → 知识库回答 → 语音回复。

用法：
  ./.venv/bin/python tools/e2e_ws_test.py                 # 默认问题（差旅报销）
  ./.venv/bin/python tools/e2e_ws_test.py --text "星火，项目验收标准是什么？"

流程与页面完全一致：/ws/meeting?engine=pipeline，音频帧 = 1 字节声道 + 16k PCM16。
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import websockets

for _var in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy",
             "HTTPS_PROXY", "https_proxy"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
SERVICE = "http://127.0.0.1:8765"
WS_URI = "ws://127.0.0.1:8765/ws/meeting?engine=pipeline"
QUESTION_DEFAULT = "星火，差旅报销多久到账？"
FRAME_BYTES = 640  # 20ms @ 16kHz mono PCM16


def check_service() -> dict:
    with urllib.request.urlopen(f"{SERVICE}/api/status", timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def synth_question(text: str) -> bytes:
    """say → AIFF → 16k mono PCM16（模拟真人提问录音）。"""
    aiff = Path("/tmp/spark_e2e_q.aiff")
    pcm_path = Path("/tmp/spark_e2e_q.pcm")
    subprocess.run(["say", "-v", "Tingting", "-r", "185", "-o", str(aiff), text],
                   check=True, timeout=30)
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(aiff), "-ar", "16000", "-ac", "1",
                    "-f", "s16le", str(pcm_path)], check=True, timeout=30)
    return pcm_path.read_bytes()


async def run(text: str) -> int:
    status = check_service()
    print(f"服务：kb={status['kb']['chunks']} 片段 | "
          + " | ".join(f"mcp {s['name']}={s['status']}" for s in status["mcp"]))

    pcm = synth_question(text)
    print(f"提问音频：{len(pcm) / 2 / 16000:.1f}s（say 合成，模拟真人发声）")

    events: list[dict] = []
    audio_bytes = 0
    first_audio_at: float | None = None
    t_end_speech = 0.0
    agent_text = ""
    asr_text = ""
    metrics: dict = {}

    async with websockets.connect(WS_URI, max_size=None, proxy=None) as ws:
        # 等会话就绪
        while True:
            event = json.loads(await asyncio.wait_for(ws.recv(), 10))
            events.append(event)
            if event.get("type") == "session.state" and event.get("status") == "connected":
                break

        # 推流：1 字节声道(0=我) + PCM，实时节奏
        for i in range(0, len(pcm), FRAME_BYTES):
            await ws.send(b"\x00" + pcm[i:i + FRAME_BYTES])
            await asyncio.sleep(0.02)
        # 尾部静音，触发 VAD 断句
        for _ in range(75):
            await ws.send(b"\x00" + b"\x00" * FRAME_BYTES)
            await asyncio.sleep(0.02)
        t_end_speech = time.time()

        # 收回复，直到拿到智能体语音或超时
        deadline = time.time() + 45
        while time.time() < deadline:
            try:
                message = await asyncio.wait_for(ws.recv(), timeout=deadline - time.time())
            except asyncio.TimeoutError:
                break
            if isinstance(message, (bytes, bytearray)):
                audio_bytes += len(message)
                if first_audio_at is None:
                    first_audio_at = time.time()
                continue
            event = json.loads(message)
            events.append(event)
            etype = event.get("type")
            if etype == "transcript.final":
                speaker = event.get("speaker")
                if speaker == "agent":
                    agent_text = event.get("text", "")
                else:
                    asr_text = event.get("text", "")
                    print(f"识别：{asr_text}")
            elif etype == "metrics.turn":
                metrics = event
            elif etype == "session.error":
                print(f"服务错误：{event.get('error')}")
            if agent_text and audio_bytes > 0 and first_audio_at:
                await asyncio.sleep(0.5)  # 让指标事件跟上
                while True:
                    try:
                        message = await asyncio.wait_for(ws.recv(), 0.8)
                    except asyncio.TimeoutError:
                        break
                    if isinstance(message, (bytes, bytearray)):
                        audio_bytes += len(message)
                        continue
                    event = json.loads(message)
                    if event.get("type") == "metrics.turn":
                        metrics = event
                break
        try:
            await ws.send(json.dumps({"type": "session.stop"}))
        except Exception:  # noqa: BLE001
            pass

    print(f"回答：{agent_text or '（无）'}")
    if metrics:
        keys = ["asr_ms", "retrieval_ms", "llm_ttft_ms", "tts_ttfa_ms", "channel"]
        print("指标：" + " | ".join(f"{k}={metrics[k]}" for k in keys if k in metrics))
    if first_audio_at:
        print(f"说完→首音：{(first_audio_at - t_end_speech) * 1000:.0f}ms | "
              f"回复音频 {audio_bytes / 2 / 24000:.1f}s")

    ok = bool(agent_text) and audio_bytes > 0
    print("结果：", "PASS" if ok else "FAIL")
    if not ok:
        print("事件流：", [e.get("type") for e in events])
    return 0 if ok else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", default=QUESTION_DEFAULT)
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args.text)))

#!/usr/bin/env python3
"""延迟基准：测量会议管线各环节在本机的真实耗时。

用法：
  ./.venv/bin/python tools/latency_check.py          # 本地离线链路（apple ASR + Ollama + say）
  ./.venv/bin/python tools/latency_check.py --cloud  # config.json 的云端链路（需代理/密钥可用）

环节：
  ASR     voice_samples/*.webm → 16k PCM → 识别（经 8765 服务的 /api/asr/bench，TCC 会话内）
  检索     docs/kb 知识库检索（含预热命中路径）
  LLM     流式首字延迟（本地 Ollama 可达时优先）
  TTS     单句合成出首帧音频
  合计     语音到语音估算 = ASR + 检索 + LLM 首字 + TTS 首帧
"""

import argparse
import asyncio
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.config_store import load_config  # noqa: E402
from server.llm.openai_llm import OpenAIChatLLM  # noqa: E402
from server.rag.store import KnowledgeStore  # noqa: E402
from server.tts import build_tts  # noqa: E402

SERVICE = "http://127.0.0.1:8765"
QUESTION = "差旅报销多久到账？"
LLM_PROMPT = "请用一句不超过20字的话回答：项目延期最大的风险是什么？"
TTS_TEXT = "结论是风险可控，建议先灰度上线。"


def http_json(url: str, payload: dict | None = None, timeout: int = 60) -> dict:
    data = json.dumps(payload or {}).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def bench_asr() -> dict:
    try:
        result = http_json(f"{SERVICE}/api/asr/bench", {})
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"无法连接 {SERVICE}（{type(exc).__name__}）"}
    return result


def bench_kb(config: dict) -> dict:
    store = KnowledgeStore(ROOT, config.get("kb_dir", "docs/kb"))
    store.ensure_loaded()
    started = time.time()
    hits = store.search(QUESTION, k=4)
    search_ms = (time.time() - started) * 1000
    warm_t0 = time.time()
    store.warm(QUESTION)
    warm_ms = (time.time() - warm_t0) * 1000
    hit_t0 = time.time()
    taken = store.take_warm("差旅报销多久能到账？")
    take_ms = (time.time() - hit_t0) * 1000
    return {"ok": True, "chunks": len(store.chunks), "hits": len(hits),
            "search_ms": round(search_ms, 1), "warm_ms": round(warm_ms, 1),
            "warm_hit_ms": round(take_ms, 3), "warm_hit": taken is not None}


def ollama_reachable() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/v1/models", timeout=2) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001
        return False


async def bench_llm(config: dict, local: bool) -> dict:
    if local and ollama_reachable():
        config = {**config, "llm_base_url": "http://127.0.0.1:11434/v1",
                  "llm_model": "qwen2.5:7b"}
        provider = "ollama qwen2.5:7b"
    else:
        provider = config.get("llm_model") or "cloud"
    llm = OpenAIChatLLM(config)
    if not llm.configured():
        return {"ok": False, "error": "未配置 LLM API Key"}
    messages = [{"role": "user", "content": LLM_PROMPT}]
    started = time.time()
    ttft_ms = None
    parts: list[str] = []
    try:
        async for item in llm.stream_chat(messages):
            if item["type"] == "delta":
                if ttft_ms is None:
                    ttft_ms = (time.time() - started) * 1000
                parts.append(item["text"])
            elif item["type"] == "error":
                return {"ok": False, "error": item["error"][:160]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:160]}
    total_ms = (time.time() - started) * 1000
    return {"ok": True, "provider": provider, "ttft_ms": round(ttft_ms or total_ms),
            "total_ms": round(total_ms), "chars": len("".join(parts))}


async def bench_tts(config: dict, local: bool) -> dict:
    if local:
        config = {**config, "tts_engine": "say"}
    tts = build_tts(config)
    started = time.time()
    try:
        pcm = await tts.synthesize(TTS_TEXT)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:160]}
    ttfa_ms = (time.time() - started) * 1000
    return {"ok": True, "engine": tts.name if hasattr(tts, "name") else type(tts).__name__,
            "ttfa_ms": round(ttfa_ms), "audio_ms": round(len(pcm) / 2 / 24000 * 1000)}


def line(name: str, data: dict) -> None:
    if not data.get("ok"):
        print(f"{name:<6} ✗ {data.get('error', '失败')}")
        return
    detail = " · ".join(f"{k}={v}" for k, v in data.items() if k != "ok")
    print(f"{name:<6} ✓ {detail}")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cloud", action="store_true",
                        help="用 config.json 的云端链路（默认本地离线链路）")
    args = parser.parse_args()
    local = not args.cloud
    config = load_config()
    print(f"链路：{'本地离线（apple/Ollama/say）' if local else '云端（config.json）'}")
    print("-" * 64)

    asr = bench_asr()
    if asr.get("ok"):
        line("ASR", {"provider": asr.get("provider"), "asr_ms": asr.get("asr_ms"),
                     "audio_ms": asr.get("audio_ms"), "rtf": asr.get("rtf"),
                     "识别": (asr.get("text") or "")[:24] + "…"})
    else:
        line("ASR", asr)

    kb = bench_kb(config)
    line("检索", {"chunks": kb["chunks"], "hits": kb["hits"],
                  "search_ms": kb["search_ms"], "warm_hit_ms": kb["warm_hit_ms"]})

    llm = await bench_llm(config, local)
    line("LLM", {"provider": llm.get("provider", ""), "ttft_ms": llm.get("ttft_ms"),
                 "total_ms": llm.get("total_ms"), "chars": llm.get("chars")})

    tts = await bench_tts(config, local)
    line("TTS", {"engine": tts.get("engine", ""), "ttfa_ms": tts.get("ttfa_ms"),
                 "audio_ms": tts.get("audio_ms")})

    print("-" * 64)
    stages = [asr.get("asr_ms", 0) if asr.get("ok") else 0,
              kb["search_ms"], llm.get("ttft_ms", 0) if llm.get("ok") else 0,
              tts.get("ttfa_ms", 0) if tts.get("ok") else 0]
    if all(stages):
        print(f"语音→语音估算：{round(sum(stages))}ms（预热命中时检索≈0）")
    else:
        print("部分环节失败，未生成合计。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

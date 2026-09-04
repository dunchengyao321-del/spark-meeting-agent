#!/usr/bin/env python3
"""星火 AI 发言生成 - 用 LLM 生成口语化发言

配置在 config.json:
  llm_base_url  OpenAI 兼容接口（如火山方舟 https://ark.cn-beijing.volces.com/api/v3）
  llm_api_key   API Key
  llm_model     模型名（如 doubao-seed-1.6）
  persona       可选，你的说话风格描述
"""
import json
import sys
import urllib.request
from pathlib import Path

CONFIG_FILE = Path(__file__).parent / "config.json"

DEFAULT_SYSTEM = (
    "你是用户在飞书会议中的语音分身，代替用户本人发言。"
    "根据给出的场景或指令，生成用户要说的话。要求："
    "口语化、自然、第一人称；1-3 句话，不超过 80 字；"
    "直接输出要说的话，不要任何前缀、解释或引号。"
)


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def generate_reply(instruction: str, context: str = "") -> str:
    cfg = load_config()
    base_url = cfg.get("llm_base_url", "").rstrip("/")
    api_key = cfg.get("llm_api_key", "")
    model = cfg.get("llm_model", "")
    if not base_url or not api_key or not model:
        raise RuntimeError(
            "未配置 LLM：请在 config.json 填写 llm_base_url / llm_api_key / llm_model"
        )

    system = DEFAULT_SYSTEM
    persona = cfg.get("persona", "").strip()
    if persona:
        system += f"\n用户的说话风格：{persona}"

    user = instruction
    if context.strip():
        user = f"会议上下文：\n{context.strip()}\n\n指令：{instruction}"

    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.7,
    }).encode("utf-8")

    req = urllib.request.Request(
        base_url + "/chat/completions",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    text = result["choices"][0]["message"]["content"].strip()
    return text.strip('"').strip()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('用法: python3 ai_speech.py "指令" ["会议上下文"]')
        sys.exit(1)
    instruction = sys.argv[1]
    context = sys.argv[2] if len(sys.argv) > 2 else ""
    print(generate_reply(instruction, context))

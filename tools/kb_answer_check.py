#!/usr/bin/env python3
"""知识库问答验收：问题 → 知识库检索 → 云端/本地 LLM 基于片段作答。

用法：
  ./.venv/bin/python tools/kb_answer_check.py "某客户OS上线切换的关键风险是什么？"
  ./.venv/bin/python tools/kb_answer_check.py            # 用内置示例问题

用于验证「智能体学会了公司知识库」：检索命中是否相关、回答是否有据。
"""

import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.config_store import load_config  # noqa: E402
from server.llm.openai_llm import OpenAIChatLLM  # noqa: E402
from server.rag.store import KnowledgeStore  # noqa: E402

DEFAULT_QUESTION = "某客户OS项目的上线切换方案里，关键步骤和风险点是什么？"


def build_context(hits: list[dict]) -> str:
    blocks = []
    for i, hit in enumerate(hits, 1):
        blocks.append(f"[{i}] 来源：{hit['source']}（{hit.get('heading') or hit.get('title')}）\n{hit['text']}")
    return "\n\n".join(blocks)


async def main() -> int:
    question = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_QUESTION
    config = load_config()
    store = KnowledgeStore(ROOT, config.get("kb_dir", "docs/kb"))

    t0 = time.time()
    hits = await asyncio.to_thread(store.search, question, 4)
    search_ms = (time.time() - t0) * 1000
    print(f"问题：{question}")
    print(f"检索：{search_ms:.0f}ms，命中 {len(hits)} 条")
    for hit in hits:
        print(f"  - {hit['source']} | {hit.get('heading') or hit.get('title')} | score={hit['score']}")
    if not hits:
        print("未命中知识库，无法作答。")
        return 1

    llm = OpenAIChatLLM(config)
    if not llm.configured():
        print("未配置 LLM API Key，只做检索展示。")
        return 0

    persona = str(config.get("persona", "")).strip() or "专业、简洁，先说结论"
    messages = [{
        "role": "system",
        "content": ("你是会议中的智能体「星火」。只依据下面的知识库片段回答；"
                    f"片段不足时直接说明。风格：{persona}。回答控制在 120 字以内。\n\n"
                    f"知识库片段：\n{build_context(hits)}"),
    }, {"role": "user", "content": question}]

    started = time.time()
    ttft_ms = None
    parts: list[str] = []
    async for item in llm.stream_chat(messages):
        if item["type"] == "delta":
            if ttft_ms is None:
                ttft_ms = (time.time() - started) * 1000
            parts.append(item["text"])
        elif item["type"] == "error":
            print(f"LLM 错误：{item['error']}")
            return 1
    total_ms = (time.time() - started) * 1000
    answer = "".join(parts).strip()
    print("-" * 64)
    print(f"回答（{llm.model}，首字 {ttft_ms:.0f}ms / 共 {total_ms:.0f}ms）：")
    print(answer)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

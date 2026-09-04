"""LLM factory: 单模式——火山引擎 Ark（默认）或自建 OpenAI 兼容端点（custom）。

Volcano Engine Ark speaks the OpenAI-compatible chat-completions protocol, so
the same adapter serves both. Ark endpoints live in mainland China and are
reached directly (http_client bypasses the proxy for volces.com).
"""

import os

from server.llm.base import LLMBase
from server.llm.openai_llm import OpenAIChatLLM

__all__ = ["LLMBase", "OpenAIChatLLM", "build_llm",
           "VOLCANO_ARK_BASE_URL", "DEFAULT_VOLCANO_LLM_MODEL"]

VOLCANO_ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_VOLCANO_LLM_MODEL = "doubao-seed-2-1-pro-260628"


def build_llm(config: dict) -> LLMBase:
    """Pick the chat LLM.

    llm_provider = "custom" -> 自建/本地 OpenAI 兼容端点
    （llm_base_url / llm_model / llm_api_key，如 Ollama 离线演示）；
    其他（默认 "volcano"）-> 火山 Ark
    （volcano_api_key / volcano_llm_model / volcano_llm_base_url）。
    """
    provider = str(config.get("llm_provider", "volcano")).split("#", 1)[0].strip().lower()
    if provider == "custom":
        return OpenAIChatLLM(config, name="custom")
    base = (str(config.get("volcano_llm_base_url", "")).strip()
            or VOLCANO_ARK_BASE_URL)
    model = (str(config.get("volcano_llm_model", "")).strip()
             or DEFAULT_VOLCANO_LLM_MODEL)
    key = (os.environ.get("VOLCANO_API_KEY", "").strip()
           or str(config.get("volcano_api_key", "")).strip())
    # Ark Seed 模型默认开深度思考，语音会议等不起（首字 26~39s）；
    # 默认关闭，可用 volcano_thinking = enabled / auto 重新打开。
    thinking = str(config.get("volcano_thinking", "disabled")).split("#", 1)[0].strip().lower()
    if thinking not in ("disabled", "enabled", "auto"):
        thinking = "disabled"
    extra = {"thinking": {"type": thinking}}
    return OpenAIChatLLM(config, base_url=base, api_key=key, model=model,
                         name="volcano-ark", extra_payload=extra)

"""Configuration store shared by the meeting server.

Keeps compatibility with the legacy config.json layout used by the Feishu
tools and the old config_ui page. Secrets are never echoed back by the API.
"""

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "config.json"

# Fields the settings UI may read and write.
FIELDS = [
    "local_voice_system_prompt",
    "llm_provider", "llm_base_url", "llm_model", "persona",
    "volcano_llm_model", "volcano_llm_base_url", "volcano_thinking",
    "tts_engine", "tts_voice",
    "asr_engine", "apple_asr_locale",
    "meeting_engine", "meeting_wake_names", "meeting_wake_required", "meeting_silence_ms",
    "meeting_min_endpoint_ms", "kb_dir",
    "meeting_partial_asr", "meeting_answer_questions",
    "meeting_pending_question_ms", "meeting_tool_filler", "meeting_disambiguate",
    "b_agent_enabled", "b_agent_url",
    "proxy_bypass",
    "ov_enabled", "ov_url", "ov_timeout_ms", "ov_target_uri",
    "volcano_api_key", "volcano_tts_api_key",
    "volcano_tts_speaker", "volcano_tts_resource_id",
]

SECRET_FIELDS = ["llm_api_key", "app_secret", "volcano_api_key", "volcano_tts_api_key"]

DEFAULTS = {
    "meeting_engine": "pipeline",
    "meeting_wake_names": ["星火"],
    "meeting_silence_ms": 700,
    "meeting_min_endpoint_ms": 250,
    "llm_provider": "volcano",
    "meeting_partial_asr": True,
    "meeting_answer_questions": True,
    "meeting_pending_question_ms": 2000,
    "meeting_tool_filler": "我查一下，稍等。",
    "kb_dir": "docs/kb",
    "b_agent_enabled": False,
    "b_agent_url": "http://127.0.0.1:8766",
    "ov_enabled": False,
    "ov_url": "http://127.0.0.1:1933",
    "ov_timeout_ms": 800,
    "ov_target_uri": "viking://resources",
}


def load_config() -> dict:
    data = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    merged = dict(DEFAULTS)
    merged.update(data)
    return merged


def save_config(data: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def llm_api_key(config: dict) -> str:
    """Key for the custom OpenAI-compatible endpoint (llm_provider=custom)."""
    return (os.environ.get("LLM_API_KEY", "").strip()
            or str(config.get("llm_api_key", "")).strip())

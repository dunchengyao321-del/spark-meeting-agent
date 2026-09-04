"""LLM provider factory, proxy bypass and filler normalization (offline)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.http_client import _host_bypasses_proxy, proxy_bypass_suffixes  # noqa: E402
from server.llm import (DEFAULT_VOLCANO_LLM_MODEL, VOLCANO_ARK_BASE_URL,  # noqa: E402
                        build_llm)
from server.engines.pipeline import _filler_phrase, _truthy  # noqa: E402

FAILS = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}{(' — ' + str(detail)[:160]) if detail and not condition else ''}")
    if not condition:
        FAILS.append(name)


# ---- provider selection
v = build_llm({"llm_provider": "volcano", "volcano_api_key": "ark-key"})
check("F1 火山默认 Base URL", v.base_url == VOLCANO_ARK_BASE_URL, v.base_url)
check("F2 火山默认模型", v.model == DEFAULT_VOLCANO_LLM_MODEL, v.model)
check("F3 火山 Key 生效", v.configured() and v._api_key() == "ark-key")
check("F4 火山适配器命名", v.name == "volcano-ark", v.name)

v2 = build_llm({"llm_provider": "volcano", "volcano_api_key": "k",
                "volcano_llm_base_url": "https://ark.cn-beijing.volces.com/api/v3/",
                "volcano_llm_model": "doubao-seed-1-6-250615"})
check("F5 火山自定义模型", v2.model == "doubao-seed-1-6-250615", v2.model)
check("F6 Base URL 去尾斜杠", not v2.base_url.endswith("/"), v2.base_url)

o = build_llm({"llm_provider": "custom", "llm_base_url": "http://127.0.0.1:11434/v1",
               "llm_model": "qwen2.5:7b", "llm_api_key": "x"})
check("F7 自定义 Base URL 优先", o.base_url == "http://127.0.0.1:11434/v1", o.base_url)
check("F8 自定义模型", o.model == "qwen2.5:7b", o.model)

d = build_llm({})
check("F9 默认火山 Ark 端点", d.base_url == VOLCANO_ARK_BASE_URL, d.base_url)
check("F10 无 Key 未配置", not d.configured())

t = build_llm({"llm_provider": "volcano", "volcano_api_key": "k",
               "volcano_thinking": "disabled"})
check("F11 默认关闭深度思考", t.extra_payload == {"thinking": {"type": "disabled"}},
      t.extra_payload)

# ---- proxy bypass (火山等国内端点必须直连，不走 realtime_proxy)
check("P1 火山域名绕过代理", _host_bypasses_proxy("ark.cn-beijing.volces.com"))
check("P2 根域名也绕过", _host_bypasses_proxy("volces.com"))
check("P3 OpenAI 不绕过", not _host_bypasses_proxy("api.openai.com"))
check("P4 内网 IP 绕过", _host_bypasses_proxy("192.168.1.20"))
check("P5 localhost 绕过", _host_bypasses_proxy("localhost"))
check("P6 自定义绕过后缀",
      _host_bypasses_proxy("llm.corp.example.com",
                           {"proxy_bypass": ["example.com"]}))
check("P7 绕过清单可配", "example.com" in
      proxy_bypass_suffixes({"proxy_bypass": "example.com, .foo.cn"}))

# ---- behavior toggle normalization
check("T1 默认垫话", _filler_phrase(None) == "我查一下，稍等。")
check("T2 True 垫话", _filler_phrase(True) == "我查一下，稍等。")
check("T3 关闭垫话", _filler_phrase(False) == "" and _filler_phrase("off") == "")
check("T4 自定义垫话", _filler_phrase("稍等，我看一下数据。") == "稍等，我看一下数据。")
check("T5 开关解析", _truthy(None) and not _truthy("off") and not _truthy(False)
      and _truthy("true"))

print("llm factory:", "ALL PASS" if not FAILS else f"{len(FAILS)} FAILED")
sys.exit(1 if FAILS else 0)

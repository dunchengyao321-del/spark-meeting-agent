"""OpenAI-compatible streaming chat adapter (works with any compatible endpoint)."""

import asyncio
import json
import urllib.request

from server.config_store import llm_api_key
from server.http_client import build_opener
from server.llm.base import LLMBase


class OpenAIChatLLM(LLMBase):
    name = "openai-chat"

    def __init__(self, config: dict, *, base_url: str | None = None,
                 api_key: str | None = None, model: str | None = None,
                 name: str | None = None, extra_payload: dict | None = None):
        self.config = config
        if name:
            self.name = name
        base = base_url if base_url is not None else str(config.get("llm_base_url", "")).strip()
        self.base_url = base.rstrip("/") if base else "https://api.openai.com/v1"
        self._api_key_override = api_key
        self.model = model or str(config.get("llm_model", "")).strip() or "gpt-4o-mini"
        # Provider-specific request fields merged into every payload
        # (e.g. Ark Seed thinking control: {"thinking": {"type": "disabled"}}).
        self.extra_payload = dict(extra_payload or {})

    def _api_key(self) -> str:
        return self._api_key_override or llm_api_key(self.config)

    def configured(self) -> bool:
        return bool(self._api_key())

    def _open_stream(self, payload: dict, api_key: str):
        url = f"{self.base_url}/chat/completions"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        return build_opener(self.config, url).open(request, timeout=60)

    def _pump(self, payload: dict, api_key: str, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop):
        content_parts: list[str] = []
        tool_calls: dict[int, dict] = {}
        try:
            with self._open_stream(payload, api_key) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choice = (chunk.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        content_parts.append(delta["content"])
                        loop.call_soon_threadsafe(queue.put_nowait, {"type": "delta", "text": delta["content"]})
                    for tc in delta.get("tool_calls") or []:
                        slot = tool_calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "arguments": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] += fn["name"]
                        if fn.get("arguments"):
                            slot["arguments"] += fn["arguments"]
        except Exception as exc:  # noqa: BLE001 - surface to the async side
            loop.call_soon_threadsafe(queue.put_nowait, {"type": "error", "error": f"{type(exc).__name__}: {exc}"})
        finally:
            for slot in tool_calls.values():
                try:
                    arguments = json.loads(slot["arguments"]) if slot["arguments"] else {}
                except json.JSONDecodeError:
                    arguments = {"_raw": slot["arguments"]}
                loop.call_soon_threadsafe(queue.put_nowait, {
                    "type": "tool_call", "id": slot["id"], "name": slot["name"], "arguments": arguments,
                })
            loop.call_soon_threadsafe(queue.put_nowait, {"type": "done", "content": "".join(content_parts)})
            loop.call_soon_threadsafe(queue.put_nowait, None)

    async def stream_chat(self, messages: list, tools: list | None = None, model: str | None = None):
        api_key = self._api_key()
        if not api_key:
            raise RuntimeError(
                f"未配置 LLM API Key（提供方 {self.name}：请配置 volcano_api_key 或 llm_api_key）")
        payload = {
            "model": model or self.model,
            "messages": messages,
            "stream": True,
            "temperature": 0.4,
        }
        payload.update(self.extra_payload)
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        thread = asyncio.get_event_loop().run_in_executor(
            None, self._pump, payload, api_key, queue, loop)
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=45)
            except asyncio.TimeoutError:
                raise RuntimeError("LLM 流式响应超时（45s 无数据），请检查网络/代理") from None
            if item is None:
                break
            yield item
        await thread

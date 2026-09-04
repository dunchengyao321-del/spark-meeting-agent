"""LLM adapter interface (OpenAI-compatible chat completions)."""

from abc import ABC, abstractmethod


class LLMBase(ABC):
    name = "base"

    @abstractmethod
    async def stream_chat(self, messages: list, tools: list | None = None, model: str | None = None):
        """Async generator yielding events:

        {"type": "delta", "text": str}
        {"type": "tool_call", "id": str, "name": str, "arguments": dict}
        {"type": "done", "content": str}
        """
        ...

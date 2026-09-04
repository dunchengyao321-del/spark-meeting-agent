"""ASR adapter interface.

An adapter converts one finished utterance (PCM16 mono) into text. Streaming
partial results are a planned enhancement; the interface already returns an
optional confidence used by the semantic clarification step.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ASRResult:
    text: str
    confidence: float | None = None
    duration_ms: int = 0
    provider: str = ""


class ASRBase(ABC):
    name = "base"
    sample_rate = 16000

    @abstractmethod
    async def transcribe(self, pcm: bytes, rate: int) -> ASRResult:
        ...

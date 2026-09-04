"""TTS adapter interface. Returns 24kHz mono PCM16 (Realtime-native rate)."""

from abc import ABC, abstractmethod

TTS_RATE = 24000


class TTSBase(ABC):
    name = "base"
    rate = TTS_RATE
    streaming = False  # engines that emit audio while synthesizing set True

    @abstractmethod
    async def synthesize(self, text: str, voice: str | None = None) -> bytes:
        ...

    async def synthesize_stream(self, text: str, voice: str | None = None):
        """Yield PCM chunks as soon as available; default = whole sentence."""
        pcm = await self.synthesize(text, voice)
        if pcm:
            yield pcm

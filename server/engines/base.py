"""Engine interface shared by the meeting session."""

import asyncio
from abc import ABC, abstractmethod


class SessionIO:
    """Bridge between an engine and the browser WebSocket."""

    def __init__(self):
        self.frames: asyncio.Queue[tuple[int, bytes]] = asyncio.Queue(maxsize=512)
        self.controls: asyncio.Queue[dict] = asyncio.Queue()
        self.closed = asyncio.Event()

    async def send_event(self, event: dict) -> None:
        raise NotImplementedError

    async def send_audio(self, pcm24k: bytes) -> None:
        raise NotImplementedError


class EngineBase(ABC):
    kind = "base"

    @abstractmethod
    async def run(self, io: SessionIO) -> None:
        ...

    async def stop(self) -> None:
        ...

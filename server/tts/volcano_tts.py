"""Volcano (Doubao) bidirectional streaming TTS adapter.

Human-like neural voices via wss://openspeech.bytedance.com/api/v3/tts/bidirection.
Outputs 24kHz mono PCM16 — the pipeline-native format, no transcoding needed.

One persistent WebSocket per engine session: sentences reuse the connection
(StartSession/TaskRequest/FinishSession per sentence), so per-sentence overhead
is ~0 instead of a fresh TLS+WS handshake (~450ms) each time. Broken connections
are rebuilt transparently; barge-in cancellation drops the connection so the
next sentence starts from a clean protocol state.
"""

import asyncio
import copy
import json
import uuid

import websockets

from server.tts.base import TTSBase, TTS_RATE
from server.tts.volcano_protocols import (EventType, MsgType, finish_session,
                                          receive_message, start_connection,
                                          start_session, task_request,
                                          wait_for_event)

URL = "wss://openspeech.bytedance.com/api/v3/tts/bidirection"
DEFAULT_SPEAKER = "zh_male_wennuanahu_uranus_bigtts"  # 温暖阿虎 2.0
DEFAULT_RESOURCE_ID = "seed-tts-2.0"


class VolcanoTTS(TTSBase):
    name = "volcano"
    streaming = True

    def __init__(self, config: dict):
        self.api_key = str(config.get("volcano_tts_api_key", "")).strip()
        self.speaker = (str(config.get("volcano_tts_speaker", "")).strip()
                        or DEFAULT_SPEAKER)
        self.resource_id = (str(config.get("volcano_tts_resource_id", "")).strip()
                            or DEFAULT_RESOURCE_ID)
        self._ws = None
        self._lock = asyncio.Lock()

    def configured(self) -> bool:
        return bool(self.api_key)

    async def warm(self) -> None:
        """Pre-open the connection at session start (kills first-sentence latency)."""
        if not self.api_key:
            return
        async with self._lock:
            if self._ws is None:
                self._ws = await self._connect()

    async def _connect(self):
        headers = {
            "X-Api-Key": self.api_key,
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Connect-Id": str(uuid.uuid4()),
        }
        # 国内端点强制直连（proxy=None 忽略系统代理环境变量）
        ws = await websockets.connect(URL, additional_headers=headers,
                                      max_size=10 * 1024 * 1024, proxy=None)
        await start_connection(ws)
        await wait_for_event(ws, MsgType.FullServerResponse,
                             EventType.ConnectionStarted)
        return ws

    async def _drop(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass

    async def synthesize(self, text: str, voice: str | None = None) -> bytes:
        pcm = bytearray()
        async for chunk in self.synthesize_stream(text, voice):
            pcm.extend(chunk)
        return bytes(pcm)

    async def synthesize_stream(self, text: str, voice: str | None = None):
        if not self.api_key:
            raise RuntimeError("未配置火山 TTS API Key（设置面板：火山语音 Key）")
        if not text or not text.strip():
            return
        async with self._lock:
            for attempt in (1, 2):
                try:
                    if self._ws is None:
                        self._ws = await self._connect()
                    async for chunk in self._run_session(text, voice):
                        yield chunk
                    return
                except BaseException as exc:
                    # 取消/关闭（CancelledError、GeneratorExit 等非 Exception）
                    # 必须立刻传播，只有普通错误才重连重试一次。
                    await self._drop()
                    if not isinstance(exc, Exception) or attempt == 2:
                        raise

    async def _run_session(self, text: str, voice: str | None):
        session_id = str(uuid.uuid4())
        base = {"req_params": {
            "speaker": voice or self.speaker,
            "audio_params": {"format": "pcm", "sample_rate": TTS_RATE},
        }}
        req = copy.deepcopy(base)
        req["event"] = EventType.StartSession
        await start_session(self._ws, json.dumps(req).encode(), session_id)
        await wait_for_event(self._ws, MsgType.FullServerResponse,
                             EventType.SessionStarted)

        req = copy.deepcopy(base)
        req["event"] = EventType.TaskRequest
        req["req_params"]["text"] = text
        await task_request(self._ws, json.dumps(req).encode(), session_id)
        await finish_session(self._ws, session_id)

        while True:
            try:
                msg = await asyncio.wait_for(receive_message(self._ws), timeout=30)
            except asyncio.TimeoutError:
                raise RuntimeError("火山 TTS 响应超时（30s 无数据）") from None
            if msg.type == MsgType.AudioOnlyServer:
                if msg.payload:
                    yield msg.payload
            elif msg.type == MsgType.FullServerResponse:
                if msg.event == EventType.SessionFinished:
                    break
                if msg.event in (EventType.SessionFailed,
                                 EventType.SessionCanceled):
                    raise RuntimeError(f"火山 TTS 会话失败: {msg}")
            elif msg.type == MsgType.Error:
                raise RuntimeError(f"火山 TTS 错误: {msg}")

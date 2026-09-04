"""Volcano (Doubao) seed-ASR streaming adapter.

Endpoint: wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async — the
optimized bidirectional streaming recognizer with two-pass recognition
(enable_nonstream): streaming results for speed plus a non-streaming
re-recognition per VAD segment for final accuracy. Binary framing is shared
with the TTS protocol module (same 4-byte header layout).

The pipeline hands over one finished utterance (own VAD already applied), so
the whole buffer is uploaded, a negative packet terminates the stream, and we
wait for the final package. Auth is the speech-console API key (X-Api-Key),
the same key used by the TTS adapter.
"""

import asyncio
import json
import os
import time
import uuid

import websockets
from websockets.exceptions import ConnectionClosedOK

from server.asr.base import ASRBase, ASRResult
from server.tts.volcano_protocols import (Message, MsgType, MsgTypeFlagBits)

URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
DEFAULT_RESOURCE_ID = "volc.seedasr.sauc.duration"  # 豆包流式语音识别模型 2.0（小时版）
CHUNK_BYTES = 6400  # 200ms @ 16kHz 16bit mono（官方推荐分包大小）
HOTWORD_LIMIT = 60  # 热词+上下文合计上限 100 tokens，留足余量


class VolcanoASR(ASRBase):
    name = "volcano"
    sample_rate = 16000

    def __init__(self, config: dict):
        self.api_key = (os.environ.get("VOLCANO_SPEECH_KEY", "").strip()
                        or str(config.get("volcano_asr_api_key", "")).strip()
                        or str(config.get("volcano_tts_api_key", "")).strip())
        self.resource_id = (str(config.get("volcano_asr_resource_id", "")).strip()
                            or DEFAULT_RESOURCE_ID)
        self.two_pass = (str(config.get("volcano_asr_two_pass", "true"))
                         .strip().lower() not in ("0", "false", "off"))
        self._hotwords: list[str] = []

    def set_hotwords(self, words: list[str]) -> None:
        self._hotwords = [w for w in dict.fromkeys(words) if 1 < len(w) <= 20][:HOTWORD_LIMIT]

    def configured(self) -> bool:
        return bool(self.api_key)

    def _request_payload(self) -> bytes:
        request: dict = {
            "model_name": "bigmodel",
            "enable_itn": True,
            "enable_punc": True,
            "enable_ddc": True,
            "result_type": "full",
            "end_window_size": 800,
            "force_to_speech_time": 1000,
        }
        if self.two_pass:
            request["enable_nonstream"] = True
        if self._hotwords:
            context = json.dumps({"hotwords": [{"word": w} for w in self._hotwords]},
                                 ensure_ascii=False)
            request["corpus"] = {"context": context}
        payload = {
            "user": {"uid": "spark-meeting", "platform": "macOS"},
            "audio": {"format": "pcm", "codec": "raw", "rate": 16000,
                      "bits": 16, "channel": 1},
            "request": request,
        }
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    async def transcribe(self, pcm: bytes, rate: int) -> ASRResult:
        if not self.api_key:
            raise RuntimeError("未配置火山语音 Key（设置面板：火山语音 Key）")
        if not pcm:
            return ASRResult(text="", provider=self.name)
        started = time.time()
        headers = {
            "X-Api-Key": self.api_key,
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Request-Id": str(uuid.uuid4()),
            "X-Api-Sequence": "-1",
        }
        try:
            ws = await asyncio.wait_for(
                websockets.connect(URL, additional_headers=headers,
                                   max_size=10 * 1024 * 1024, proxy=None),
                timeout=10)
        except asyncio.TimeoutError:
            raise RuntimeError("火山 ASR 连接超时（10s），请检查网络") from None
        except Exception as exc:
            detail = str(exc)[:140]
            if "401" in detail or "403" in detail:
                raise RuntimeError(f"火山 ASR 鉴权失败（请检查语音控制台是否开通语音识别）: {detail}") from None
            raise RuntimeError(f"火山 ASR 连接失败: {detail}") from None
        try:
            req = Message(type=MsgType.FullClientRequest, flag=MsgTypeFlagBits.NoSeq)
            req.payload = self._request_payload()
            await ws.send(req.marshal())

            # 服务端把 full client request 计为序号 1，音频包从 2 开始
            seq = 1
            for i in range(0, len(pcm), CHUNK_BYTES):
                seq += 1
                msg = Message(type=MsgType.AudioOnlyClient,
                              flag=MsgTypeFlagBits.PositiveSeq)
                msg.sequence = seq
                msg.payload = pcm[i:i + CHUNK_BYTES]
                await ws.send(msg.marshal())
            last = Message(type=MsgType.AudioOnlyClient,
                           flag=MsgTypeFlagBits.NegativeSeq)
            last.sequence = -(seq + 1)
            last.payload = b""
            await ws.send(last.marshal())

            text = ""
            deadline = time.time() + 20
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise RuntimeError("火山 ASR 响应超时（20s 无最终结果）")
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    raise RuntimeError("火山 ASR 响应超时（20s 无最终结果）") from None
                except ConnectionClosedOK:
                    break  # 服务端发完最终结果后主动关闭
                msg = Message.from_bytes(raw)
                if msg.type == MsgType.Error:
                    raise RuntimeError(
                        f"火山 ASR 错误 {msg.error_code}: "
                        f"{msg.payload.decode('utf-8', 'replace')[:140]}")
                if msg.type != MsgType.FullServerResponse or not msg.payload:
                    continue
                data = json.loads(msg.payload.decode("utf-8", "replace"))
                code = data.get("code")
                if code is not None and int(code) != 0:
                    raise RuntimeError(f"火山 ASR 识别失败 code={code}: {str(data)[:160]}")
                result = data.get("result")
                if isinstance(result, list):  # 防御：个别版本返回列表
                    result = result[0] if result else None
                if isinstance(result, dict):
                    t = str(result.get("text") or "").strip()
                    if t:
                        text = t
                if data.get("is_last_package"):
                    break
            return ASRResult(text=text, confidence=None,
                             duration_ms=int((time.time() - started) * 1000),
                             provider=self.name)
        finally:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass

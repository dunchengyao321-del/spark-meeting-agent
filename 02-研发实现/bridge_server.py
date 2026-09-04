"""
bridge_server.py —— 飞书会议语音智能体 · 桥接服务器

职责：
  1) 作为 WebSocket 服务端（websockets 库）与浏览器页面里的 shim.js 通信；
  2) 下行通道 meeting_pcm：接收浏览器发来的会议室 PCM（Int16 -> Float32），喂给 ASR 回调；
  3) 上行通道 tts_pcm：把 TTS 产出的 PCM（Float32 -> Int16）发给浏览器注入合成麦克风；
  4) 背压与丢帧策略：上行发送队列满时丢最旧帧，保证实时性；
  5) 预留 ASR / LLM / TTS 对接的清晰接口（抽象基类 + 回调注册）。

协议约定（与 shim.js 对应）：
  单条 WebSocket 连接（默认 ws://127.0.0.1:8765/ws）上复用两个逻辑通道，
  二进制帧第 1 字节为通道号：
    0x01 下行 meeting_pcm：浏览器 -> 服务器，Int16 小端 PCM，48kHz 单声道，20ms/帧（960 采样）
    0x02 上行 tts_pcm    ：服务器 -> 浏览器，格式同上
  文本帧为 JSON 控制消息（hello / ping / pong）。

运行：python3 bridge_server.py [--host 127.0.0.1] [--port 8765] [--tone-test]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from abc import ABC, abstractmethod
from typing import AsyncIterator, Awaitable, Callable, List, Optional

import numpy as np
import websockets

# ---------------- 常量（与 shim.js 保持一致） ----------------
SAMPLE_RATE = 48000          # 统一 48kHz 单声道
FRAME_SAMPLES = 960          # 20ms 一帧
CH_DOWNLINK = 0x01           # 下行：会议室 -> 后端
CH_UPLINK = 0x02             # 上行：后端 -> 浏览器
UPLINK_QUEUE_MAX = 50        # 上行发送队列上限（50 帧 = 1s 音频），满则丢最旧

log = logging.getLogger("bridge")


# ==================== ASR / LLM / TTS 对接接口（抽象基类） ====================
class ASREngine(ABC):
    """语音识别引擎抽象基类：下行会议室 PCM 的消费者。"""

    @abstractmethod
    async def feed_pcm(self, pcm_f32: np.ndarray, sample_rate: int) -> None:
        """喂入一段 Float32 单声道 PCM（取值 -1.0 ~ 1.0）。实现方内部自行做流式识别。"""


class LLMEngine(ABC):
    """大模型引擎抽象基类：输入 ASR 文本，输出回复文本。"""

    @abstractmethod
    async def chat(self, text: str) -> str:
        """返回智能体的回复文本。"""


class TTSEngine(ABC):
    """语音合成引擎抽象基类：把文本合成为 Float32 PCM 块。"""

    @abstractmethod
    def synthesize(self, text: str) -> AsyncIterator[np.ndarray]:
        """异步生成器：按块产出 Float32 单声道 PCM（48kHz）。实现方负责流式产出。"""


class NullASR(ASREngine):
    """占位 ASR：不接真实识别，只周期性打印电平/流量统计，便于联调链路。"""

    def __init__(self, log_interval_s: float = 5.0) -> None:
        self._log_interval = log_interval_s
        self._last_log = time.monotonic()
        self._frames = 0
        self._sum_sq = 0.0
        self._count = 0

    async def feed_pcm(self, pcm_f32: np.ndarray, sample_rate: int) -> None:
        self._frames += 1
        self._sum_sq += float(np.square(pcm_f32, dtype=np.float64).sum())
        self._count += int(pcm_f32.size)
        now = time.monotonic()
        if now - self._last_log >= self._log_interval:
            rms = (self._sum_sq / self._count) ** 0.5 if self._count else 0.0
            log.info("[下行] 占位ASR 统计: %d 帧, RMS=%.4f（接真实 ASR 后替换本类）", self._frames, rms)
            self._last_log = now
            self._frames = 0
            self._sum_sq = 0.0
            self._count = 0


# ==================== 桥接服务器 ====================
# 下行回调类型：async def cb(pcm_f32: np.ndarray, sample_rate: int) -> None
DownlinkCallback = Callable[[np.ndarray, int], Awaitable[None]]
# 控制消息回调类型：async def cb(msg: dict) -> None（chat.message / screen.frame 等）
ControlCallback = Callable[[dict], Awaitable[None]]


class BridgeServer:
    """WebSocket 桥接服务器：承载下行 meeting_pcm 与上行 tts_pcm 两个逻辑通道。"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8765,
        asr: Optional[ASREngine] = None,
        tts: Optional[TTSEngine] = None,
    ) -> None:
        self.host = host
        self.port = port
        self._asr: ASREngine = asr or NullASR()     # 默认占位 ASR，联调用
        self._tts: Optional[TTSEngine] = tts        # TTS 可后接，speak_text 是入口
        self._clients: dict = {}                     # websocket -> asyncio.Queue[bytes]
        self._monitors: set = set()                  # 监控页 WebSocket 连接集合
        self._downlink_cbs: List[DownlinkCallback] = []
        self._control_cbs: List[ControlCallback] = []

    # ---------- 对外接口 ----------
    def on_downlink(self, callback: DownlinkCallback) -> None:
        """注册下行回调：每收到一帧会议室 PCM 都会被调用（在 ASR 之后）。"""
        self._downlink_cbs.append(callback)

    def on_control(self, callback: ControlCallback) -> None:
        """注册控制消息回调：页面发来的非内建控制消息（chat.message / screen.frame 等）。"""
        self._control_cbs.append(callback)

    def send_tts_pcm(self, pcm_f32: np.ndarray) -> None:
        """上行发送入口：Float32 PCM -> Int16，广播给所有已连接页面。
        背压策略：某客户端队列满时丢其最旧一帧（保实时性，不堆积延迟）。"""
        if pcm_f32.size == 0 or not self._clients:
            return
        clipped = np.clip(pcm_f32, -1.0, 1.0)
        payload = (clipped * 32767.0).astype("<i2").tobytes()
        frame = bytes([CH_UPLINK]) + payload
        for queue in self._clients.values():
            if queue.full():
                try:
                    queue.get_nowait()  # 丢最旧帧
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:
                pass  # 极端情况下仍满则直接丢本帧

    async def speak_text(self, text: str) -> None:
        """TTS 对接入口：文本 -> TTS 流式 PCM -> 上行发送。未接 TTS 时为空操作。"""
        if self._tts is None:
            log.warning("speak_text 被调用但未配置 TTSEngine，忽略: %r", text[:50])
            return
        async for chunk in self._tts.synthesize(text):
            self.send_tts_pcm(chunk)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def send_control(self, msg: dict) -> None:
        """向所有已连接页面广播一条 JSON 控制消息（如 chat.post 聊天回帖指令）。"""
        if not self._clients:
            return
        text = json.dumps(msg, ensure_ascii=False)
        for ws in self._clients:
            asyncio.create_task(ws.send(text))

    # ---------- 监控页（/ws/monitor） ----------
    def push_monitor_event(self, ev: dict) -> None:
        """把管线事件（转写/状态/投屏帧等）实时广播给所有监控页。"""
        if not self._monitors:
            return
        text = json.dumps(ev, ensure_ascii=False)
        for ws in list(self._monitors):
            asyncio.create_task(self._safe_send_monitor(ws, text))

    async def _safe_send_monitor(self, ws, text: str) -> None:
        try:
            await ws.send(text)
        except Exception:  # noqa: BLE001
            self._monitors.discard(ws)

    async def _monitor_handler(self, websocket) -> None:
        peer = getattr(websocket, "remote_address", "?")
        self._monitors.add(websocket)
        log.info("监控端已连接: %s（当前 %d 个）", peer, len(self._monitors))
        try:
            async for message in websocket:
                try:
                    msg = json.loads(message)
                except json.JSONDecodeError:
                    continue
                mtype = msg.get("type", "")
                if mtype == "agent.stop":
                    self.flush_uplink()
                    log.info("[监控] 紧急停止：已清空上行音频")
                else:
                    for cb in self._control_cbs:
                        try:
                            await cb(msg)
                        except Exception:  # noqa: BLE001
                            log.exception("监控控制消息回调异常")
        except websockets.ConnectionClosed:
            pass
        finally:
            self._monitors.discard(websocket)
            log.info("监控端已断开: %s（剩余 %d 个）", peer, len(self._monitors))

    def flush_uplink(self) -> None:
        """清空所有客户端上行队列，并广播 clear_audio 让页面清空抖动缓冲（真人打断时调用）。
        websockets 库支持多任务并发 send（内部已序列化），这里直接创建发送任务即可。"""
        drained = 0
        for queue in self._clients.values():
            while True:
                try:
                    queue.get_nowait()
                    drained += 1
                except asyncio.QueueEmpty:
                    break
        msg = json.dumps({"type": "clear_audio"})
        for ws in self._clients:
            asyncio.create_task(ws.send(msg))
        log.info("已清空上行队列（丢弃 %d 帧）并广播 clear_audio", drained)

    # ---------- 连接处理 ----------
    async def _handler(self, websocket) -> None:
        """每个浏览器页面一个连接。/monitor 路径走监控端，其余走 shim 音频通道。"""
        path = ""
        try:
            path = websocket.request.path or ""
        except Exception:  # noqa: BLE001
            pass
        if path.endswith("/monitor"):
            await self._monitor_handler(websocket)
            return
        peer = getattr(websocket, "remote_address", "?")
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=UPLINK_QUEUE_MAX)
        self._clients[websocket] = queue
        sender = asyncio.create_task(self._uplink_sender(websocket, queue))
        log.info("客户端已连接: %s（当前 %d 个）", peer, len(self._clients))
        try:
            async for message in websocket:
                if isinstance(message, (bytes, bytearray)):
                    self._route_binary(bytes(message))
                else:
                    await self._handle_control(websocket, message)
        except websockets.ConnectionClosed:
            pass
        finally:
            sender.cancel()
            self._clients.pop(websocket, None)
            log.info("客户端已断开: %s（剩余 %d 个）", peer, len(self._clients))

    def _route_binary(self, data: bytes) -> None:
        """按第 1 字节通道号分发二进制音频帧。"""
        if len(data) < 2:
            return
        channel, payload = data[0], data[1:]
        if channel == CH_DOWNLINK:
            # Int16 小端 -> Float32，交给 ASR 与注册的回调
            pcm_i16 = np.frombuffer(payload, dtype="<i2")
            pcm_f32 = pcm_i16.astype(np.float32) / 32768.0
            asyncio.create_task(self._dispatch_downlink(pcm_f32))
        elif channel == CH_UPLINK:
            log.warning("收到浏览器发来的上行帧？协议中上行仅 服务器->浏览器，已忽略")

    async def _dispatch_downlink(self, pcm_f32: np.ndarray) -> None:
        await self._asr.feed_pcm(pcm_f32, SAMPLE_RATE)
        for cb in self._downlink_cbs:
            try:
                await cb(pcm_f32, SAMPLE_RATE)
            except Exception:  # 单个回调异常不影响主链路
                log.exception("下行回调执行异常")

    async def _uplink_sender(self, websocket, queue: asyncio.Queue) -> None:
        """上行发送协程：从队列取帧发送；队列由 send_tts_pcm 生产（满则丢最旧）。"""
        try:
            while True:
                frame = await queue.get()
                await websocket.send(frame)
        except (websockets.ConnectionClosed, asyncio.CancelledError):
            pass

    async def _handle_control(self, websocket, message: str) -> None:
        """JSON 控制帧：hello / ping / pong。"""
        try:
            msg = json.loads(message)
        except json.JSONDecodeError:
            return
        mtype = msg.get("type")
        if mtype == "hello":
            log.info("收到页面握手: %s", msg)
            await websocket.send(json.dumps({"type": "hello-ack", "sampleRate": SAMPLE_RATE}))
        elif mtype == "ping":
            await websocket.send(json.dumps({"type": "pong", "t": msg.get("t")}))
        else:
            # 其余控制消息（chat.message / screen.frame 等）：
            # 1) 广播给监控页（监控台投屏画面/聊天流的唯一来源）
            # 2) 转发给注册的回调（adapter -> 管线视觉问答）
            if mtype in ("chat.message", "screen.frame"):
                self.push_monitor_event(msg)
            for cb in self._control_cbs:
                try:
                    await cb(msg)
                except Exception:  # 单个回调异常不影响主链路
                    log.exception("控制消息回调执行异常")

    # ---------- 联调辅助：上行 440Hz 测试音 ----------
    async def _tone_test_loop(self) -> None:
        """每 5 秒向浏览器发 0.5 秒 440Hz 提示音，用于不进会议验证上行链路。"""
        log.info("上行测试音已开启：每 5s 发送 0.5s 440Hz 提示音（--tone-test）")
        freq = 440.0
        duration_s = 0.5
        n = int(SAMPLE_RATE * duration_s)
        tone = (0.2 * np.sin(2 * np.pi * freq * np.arange(n) / SAMPLE_RATE)).astype(np.float32)
        while True:
            await asyncio.sleep(5)
            if not self._clients:
                continue
            for i in range(0, n, FRAME_SAMPLES):
                self.send_tts_pcm(tone[i:i + FRAME_SAMPLES])
                await asyncio.sleep(FRAME_SAMPLES / SAMPLE_RATE)  # 按 20ms 节奏发，模拟实时流

    # ---------- 启动 ----------
    async def run(self, tone_test: bool = False, adapter=None) -> None:
        log.info("桥接服务器启动: ws://%s:%d/ws（48kHz mono / Int16 20ms帧）", self.host, self.port)
        async with websockets.serve(self._handler, self.host, self.port):
            tasks = []
            if tone_test:
                tasks.append(asyncio.create_task(self._tone_test_loop()))
            if adapter is not None:
                tasks.append(asyncio.create_task(adapter.start()))
                log.info("星火管线适配器已启动: %s（下行增益 %.1fx）", adapter.url, adapter.downlink_gain)
            try:
                await asyncio.Future()  # 永久运行，直到被 Ctrl+C
            finally:
                for t in tasks:
                    t.cancel()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="飞书会议语音智能体 · 桥接服务器")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    parser.add_argument("--tone-test", action="store_true",
                        help="开启上行 440Hz 测试音（每 5s 一次），用于回环验证")
    parser.add_argument("--pipeline-url", default="",
                        help="星火管线地址（如 ws://127.0.0.1:8765/ws/meeting），传入后接全链路 ASR/LLM/TTS")
    parser.add_argument("--downlink-gain", type=float, default=2.0,
                        help="接管线时的会议下行增益（默认 2.0，助过管线噪声闸门）")
    parser.add_argument("--pipeline-channel", type=int, default=1, choices=[0, 1],
                        help="送入管线的声道：0=主人（免唤醒直答，演示用）1=会议（点名仲裁，默认）")
    parser.add_argument("--log-level", default="INFO", help="日志级别（默认 INFO）")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    server = BridgeServer(host=args.host, port=args.port)
    adapter = None
    if args.pipeline_url:
        from pipeline_adapter import MeetingPipelineAdapter
        adapter = MeetingPipelineAdapter(args.pipeline_url, server,
                                         downlink_gain=args.downlink_gain,
                                         channel=args.pipeline_channel)
        server.on_downlink(adapter.feed_downlink)
        server.on_control(adapter.feed_control)
        if args.tone_test:
            log.warning("已接管线，--tone-test 忽略（测试音会与 TTS 抢通道）")
    try:
        asyncio.run(server.run(tone_test=args.tone_test and adapter is None, adapter=adapter))
    except KeyboardInterrupt:
        log.info("收到 Ctrl+C，服务器退出")


if __name__ == "__main__":
    main()

"""
pipeline_adapter.py —— 对接「星火会议控制台」全链路管线（/ws/meeting）的适配器

把 bridge_server 的双向 48kHz PCM 流转接到星火服务器（默认 ws://127.0.0.1:8765/ws/meeting）：

  下行：shim(48kHz Int16) -> 本适配器（增益 + 重采样 48k->16k，声道字节 1=会议）-> /ws/meeting
  上行：/ws/meeting 的 TTS(24kHz Int16) -> 重采样 24k->48k -> bridge 上行队列 -> shim 注入会议
  事件：transcript.* / agent.state 打日志；clear_audio（管线打断）-> 清空桥接上行队列并通知页面

注意：feed_downlink 会被 bridge 以每帧一个 task 的方式并发调用，
重采样器状态（audioop.ratecv 的 state）必须串行访问，故全程加锁保序。
"""
from __future__ import annotations

import asyncio
import json
import logging
import audioop  # Python 3.10 内置；3.12+ 移除，届时可换 numpy 实现
from datetime import datetime
from pathlib import Path

import numpy as np
import websockets

log = logging.getLogger("pipeline-adapter")

MEETING_CHANNEL = 1      # 管线声道：0=我（控制台主人麦）1=会议
PIPE_IN_RATE = 16000     # 管线输入采样率（engines/pipeline.py BROWSER_RATE）
PIPE_OUT_RATE = 24000    # 管线 TTS 输出采样率（tts/base.py TTS_RATE）
BRIDGE_RATE = 48000      # 桥接统一采样率
FRAME_BYTES = 960 * 2    # 20ms @48k Int16
TRANSCRIPT_DIR = Path(__file__).resolve().parent / "transcripts"  # 会议实时记录（JSONL）


class MeetingPipelineAdapter:
    """桥接服务器 <-> 星火管线（/ws/meeting）的双向适配器（带断线重连）。"""

    def __init__(self, url: str, bridge, downlink_gain: float = 2.0, channel: int = MEETING_CHANNEL) -> None:
        self.url = url
        self.bridge = bridge                      # BridgeServer：send_tts_pcm / flush_uplink
        self.downlink_gain = float(downlink_gain)  # 会议下行增益（管线噪声闸门 peak>=1500 较严）
        self.channel = int(channel)                # 送入管线的声道：0=主人（免唤醒直答）1=会议（点名仲裁）
        self._ws = None
        self._recv_task: asyncio.Task | None = None
        self._stopped = False
        self._send_lock = asyncio.Lock()          # 重采样状态与发送保序
        self._rs_down = None                      # audioop 状态：48k -> 16k
        self._rs_up = None                        # audioop 状态：24k -> 48k
        self._up_tail = b""                       # 上行凑帧缓存（Int16 字节）
        self._up_buf = bytearray()                # 上行整流缓冲：TTS 突发到达，按 20ms 实时节奏匀速放出
        self._pace_task: asyncio.Task | None = None
        self._posted_replies: dict[str, float] = {}  # 已代发到会议聊天的回复文本 -> 时间戳（防自答循环）

    # ---------- 生命周期 ----------
    async def start(self) -> None:
        """永久重连循环：由 bridge.run() 以 task 形式启动。"""
        self._pace_task = asyncio.create_task(self._pace_loop())
        while not self._stopped:
            try:
                async with websockets.connect(self.url, ping_interval=None, max_queue=1000) as ws:
                    self._ws = ws
                    log.info("已接入星火管线: %s", self.url)
                    self._recv_task = asyncio.create_task(self._recv_loop(ws))
                    await self._recv_task
            except Exception as exc:  # noqa: BLE001 - 任何断连都重试
                log.warning("星火管线连接断开（5s 后重连）: %s", exc)
            finally:
                self._ws = None
                if self._recv_task:
                    self._recv_task.cancel()
                    self._recv_task = None
                # 断连时清掉未播完的上行音频，避免重连后播陈旧声音
                self._up_buf.clear()
                self._up_tail = b""
                self._rs_up = None
            await asyncio.sleep(5)

    async def stop(self) -> None:
        self._stopped = True
        if self._pace_task:
            self._pace_task.cancel()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass

    # ---------- 下行：会议室 48k PCM -> 管线（注册为 bridge.on_downlink 回调） ----------
    async def feed_downlink(self, pcm_f32: np.ndarray, sample_rate: int) -> None:
        ws = self._ws
        if ws is None:
            return
        async with self._send_lock:  # ratecv 状态必须串行
            boosted = np.clip(pcm_f32 * self.downlink_gain, -1.0, 1.0)
            i16 = (boosted * 32767.0).astype("<i2").tobytes()
            out, self._rs_down = audioop.ratecv(i16, 2, 1, sample_rate, PIPE_IN_RATE, self._rs_down)
            try:
                await ws.send(bytes([self.channel]) + out)
            except Exception:  # noqa: BLE001 - 发送失败交给重连循环
                pass

    # ---------- 页面控制消息 -> 管线（注册为 bridge.on_control 回调） ----------
    async def feed_control(self, msg: dict) -> None:
        """把页面/监控页侧的控制消息转发给管线。

        支持的类型：
        - chat.message（会议聊天）/ screen.frame（投屏截帧）：转发给管线消费
        - inject.say：让智能体说指定文本（控制台触发）
        - agent.force：举手接话（下一位说完后智能体主动接话）
        - session.stop：停止当前会话
        防自答循环：智能体代发到聊天框的回复文本，被扫描器重新采回时直接丢弃。
        """
        mtype = msg.get("type", "")
        if mtype not in ("chat.message", "screen.frame", "inject.say",
                         "agent.force", "session.stop"):
            return
        # 投屏帧落盘（B 智能体视觉问答的数据源；与 ws 状态无关，先落盘再转发）
        if mtype == "screen.frame":
            self._save_screen_frame(msg)
        if mtype == "chat.message":
            text = str(msg.get("text", "")).strip()
            if text and text in self._posted_replies:
                log.info("[防自答] 丢弃智能体自己发到聊天框的回复: %s", text[:40])
                return
        ws = self._ws
        if ws is None:
            return
        try:
            await ws.send(json.dumps(msg, ensure_ascii=False))
            log.info("[适配器] 控制消息已转发到管线: %s", mtype)
        except Exception:  # noqa: BLE001 - 发送失败交给重连循环
            pass

    def _save_screen_frame(self, msg: dict) -> None:
        """把最新投屏帧写到 latest_screen.jpg，供 B 智能体视觉问答读取。"""
        b64 = str(msg.get("image_b64", ""))
        if not b64:
            return
        try:
            import base64
            data = base64.b64decode(b64)
            fp = Path(__file__).resolve().parent / "latest_screen.jpg"
            fp.write_bytes(data)
            (Path(__file__).resolve().parent / "latest_screen.ts").write_text(
                str(msg.get("ts", "")), encoding="utf-8")
        except Exception:  # noqa: BLE001 - 写盘失败不影响主链路
            pass

    # ---------- 上行：管线 TTS 24k PCM -> 48k -> bridge ----------
    async def _recv_loop(self, ws) -> None:
        async for message in ws:
            if isinstance(message, (bytes, bytearray)):
                pcm48, self._rs_up = audioop.ratecv(bytes(message), 2, 1,
                                                    PIPE_OUT_RATE, BRIDGE_RATE, self._rs_up)
                self._feed_uplink(pcm48)
            else:
                await self._handle_event(message)

    def _feed_uplink(self, pcm48_i16: bytes) -> None:
        # TTS 是突发到达的（100ms 音频 ~20ms 一发），直接转发会撑爆下游缓冲造成丢帧断音；
        # 这里只入整流缓冲，由 _pace_loop 按实时节奏匀速送给 bridge。
        buf = self._up_tail + pcm48_i16
        whole = len(buf) // FRAME_BYTES * FRAME_BYTES
        self._up_buf.extend(buf[:whole])
        self._up_tail = buf[whole:]

    async def _pace_loop(self) -> None:
        """上行整流器：严格按 20ms/帧的实时节奏把 TTS 音频匀速喂给桥接，
        使桥接队列与页面抖动缓冲既不溢出（丢帧断音）也不欠载（静音卡顿）。"""
        while not self._stopped:
            if len(self._up_buf) >= FRAME_BYTES:
                frame = bytes(self._up_buf[:FRAME_BYTES])
                del self._up_buf[:FRAME_BYTES]
                f32 = np.frombuffer(frame, dtype="<i2").astype(np.float32) / 32768.0
                self.bridge.send_tts_pcm(f32)
                await asyncio.sleep(FRAME_BYTES / 2 / BRIDGE_RATE)  # 20ms
            else:
                await asyncio.sleep(0.004)

    # ---------- 会议实时记录（JSONL，唤醒回复/复盘的数据源） ----------
    def _current_meeting_id(self) -> str:
        """读取当前会议号（agent_config.json），写入记录用于多会议隔离。"""
        try:
            cfg = json.loads((Path(__file__).resolve().parent / "agent_config.json")
                             .read_text(encoding="utf-8"))
            return str(cfg.get("meeting_id", ""))
        except Exception:  # noqa: BLE001
            return ""

    def _write_transcript(self, speaker: str, text: str) -> None:
        """会议记录落盘：按「日期-会议号」分文件，天然按会议隔离。

        文件命名 meeting-<日期>-<会议号>.jsonl：每场会议独立留存，
        绝不会出现多场会议记录混淆污染上下文的情况。
        """
        try:
            TRANSCRIPT_DIR.mkdir(exist_ok=True)
            day = datetime.now().strftime("%Y%m%d")
            meeting = self._current_meeting_id() or "unknown"
            line = json.dumps({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "speaker": speaker, "text": text,
                "meeting": meeting,
            }, ensure_ascii=False)
            with open(TRANSCRIPT_DIR / f"meeting-{day}-{meeting}.jsonl", "a",
                      encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:  # noqa: BLE001 - 记录失败不影响主链路
            pass

    # ---------- 管线事件处理 ----------
    async def _handle_event(self, text: str) -> None:
        try:
            ev = json.loads(text)
        except json.JSONDecodeError:
            return
        self.bridge.push_monitor_event(ev)   # 全量事件实时推给监控页
        etype = ev.get("type", "")
        if etype in ("transcript.final", "transcript.partial"):
            log.info("[管线] %s[%s] %s", etype.split(".")[1],
                     ev.get("speaker", ""), str(ev.get("text", ""))[:120])
            if etype == "transcript.final":
                # 实时记录：语音/聊天/智能体的所有最终文本落盘 JSONL
                self._write_transcript(str(ev.get("speaker", "")), str(ev.get("text", "")))
            # 智能体的最终答复：同步代发到会议聊天框（文字进 -> 语音答 + 文字回）
            if etype == "transcript.final" and ev.get("speaker") == "agent":
                answer = str(ev.get("text", "")).strip()
                if answer:
                    self._posted_replies[answer] = asyncio.get_event_loop().time()
                    # 清理 120s 前的旧记录，避免无限增长
                    now = asyncio.get_event_loop().time()
                    for k in [k for k, ts in self._posted_replies.items() if now - ts > 120]:
                        del self._posted_replies[k]
                    self.bridge.send_control({"type": "chat.post", "text": answer[:500]})
                    log.info("[管线] 已把答复代发到会议聊天框: %s", answer[:40])
        elif etype == "agent.state":
            log.info("[管线] 智能体状态: %s %s", ev.get("state", ""), ev.get("reason", ""))
        elif etype == "clear_audio":
            log.info("[管线] clear_audio（真人打断）: 清空上行整流缓冲/桥接队列/页面抖动缓冲")
            self._up_buf.clear()          # 先清整流缓冲，旧音频绝不再播
            self._up_tail = b""
            self._rs_up = None
            self.bridge.flush_uplink()
        elif etype == "session.error":
            log.warning("[管线] 错误: %s", str(ev.get("error", ""))[:200])
        elif etype == "session.state":
            log.info("[管线] 会话: %s %s", ev.get("status", ""), ev.get("llm_model", ""))
        elif etype == "metrics.turn":
            brief = {k: v for k, v in ev.items() if k != "type" and not isinstance(v, str)}
            log.info("[管线] 一轮应答指标: %s", brief)

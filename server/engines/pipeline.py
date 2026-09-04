"""Full streaming pipeline engine: VAD -> ASR -> turn arbiter -> LLM -> TTS.

This engine owns the meeting behaviour: per-channel speaker state, turn
arbitration, semantic clarification, KB speculative prefetch, interim ASR
partials (边说边转), unanswered-question pickup and MCP tool calls.
"""

import asyncio
import json
import re
import tempfile
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from server.asr import build_asr
from server.audio_utils import build_wav, rms_level
from server.config_store import load_config
from server.engines.base import EngineBase, SessionIO
from server.llm import build_llm
from server.meeting.clarify import clarification_for
from server.meeting.turns import TurnArbiter, TurnDecision
from server.tts import build_tts

BROWSER_RATE = 16000
CHANNEL_NAMES = {0: "我", 1: "会议"}
SENTENCE_ENDS = set("。！？!?；;\n")

# Interim ASR (partial transcripts while the speaker is still talking).
PARTIAL_MIN_SPEECH_S = 1.0   # first partial after this much voiced audio
PARTIAL_INTERVAL_S = 1.2     # cadence between partial passes
PARTIAL_MAX_PER_UTTERANCE = 3

DEFAULT_TOOL_FILLER = "我查一下，稍等。"

# 触发"看投屏"意图的关键词：命中且 30s 内有截帧时，LLM 请求附带会议画面截图
_SCREEN_HINTS = ("投屏", "屏幕", "共享", "ppt", "PPT", "文档", "页面", "演示",
                 "图表", "画面", "这张图", "这个图", "截图", "看一下", "看下", "显示")


def _wants_screen(text: str) -> bool:
    return any(h in text for h in _SCREEN_HINTS)


# 停止指令词：聊天或语音中命中时，立即中断智能体当前发声/生成（T14 静音控制）
_STOP_WORDS = ("别说了", "停下", "闭嘴", "安静", "停一停", "不要说了", "别出声", "先别说话")


def _is_stop_command(text: str) -> bool:
    t = re.sub(r"[，。！？、,.!?\s]", "", text)
    return any(w in t for w in _STOP_WORDS)


def friendly_api_error(exc: Exception, stage: str) -> str:
    """Map common network/proxy failures to an actionable hint."""
    detail = f"{type(exc).__name__}: {exc}"
    lowered = detail.lower()
    if "429" in lowered or "rate limit" in lowered or "too many" in lowered:
        return f"{stage}失败：请求被限流（429），请稍后重试，或检查 API 套餐额度与代理"
    if "ssl" in lowered or "unexpected_eof" in lowered:
        return f"{stage}失败：代理隧道异常（SSL 中断），请检查本机代理/VPN 是否已连通"
    if isinstance(exc, urllib.error.URLError) and "timed out" in lowered:
        return f"{stage}失败：请求超时，请检查网络或代理设置"
    if "connection refused" in lowered or "unreachable" in lowered:
        return f"{stage}失败：无法连接服务端点，请检查网络/代理"
    return f"{stage}失败：{detail}"[:300]


def _truthy(value, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "off", "no", "关")
    return bool(value)


def _filler_phrase(value) -> str:
    """Normalize meeting_tool_filler: True/None -> default, off -> "", str -> str."""
    if value is None or value is True:
        return DEFAULT_TOOL_FILLER
    if value is False:
        return ""
    text = str(value).strip()
    if text.lower() in ("", "0", "false", "off", "no", "关"):
        return ""
    return text


@dataclass
class _ChannelState:
    """Per-channel VAD/endpoint state — mic and meeting audio never mix."""

    speaking: bool = False
    utterance: list[bytes] = field(default_factory=list)
    speech_started_at: float = 0.0
    last_voice_at: float = 0.0
    partial_count: int = 0
    last_partial_at: float = 0.0
    barge_frames: int = 0

    def begin(self, preroll: deque, now: float) -> None:
        self.speaking = True
        self.utterance = list(preroll)
        self.speech_started_at = now
        self.last_voice_at = now
        self.partial_count = 0
        self.last_partial_at = now


_HOTWORD_ASCII_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9]{2,}")
_HOTWORD_CJK_RE = re.compile(r"[一-鿿]{2,8}")


def _extract_hotwords(kb_store, wake_names, limit: int = 250) -> list[str]:
    """从唤醒名 + 知识库文件名/标题提取领域热词，注入 ASR contextualStrings。

    显著提升「星火」「差旅报销」「会员积分」「coupon」「履约」这类词的命中率。
    """
    words: list[str] = []
    seen: set[str] = set()

    def add(w: str):
        w = w.strip().strip("#*📦📚 ")
        if 1 < len(w) <= 20 and w not in seen:
            seen.add(w)
            words.append(w)

    for name in wake_names or []:
        add(str(name))
    chunks = getattr(kb_store, "chunks", []) or []
    for chunk in chunks:
        stem = Path(str(chunk.get("source", ""))).stem
        for w in _HOTWORD_ASCII_RE.findall(stem):
            add(w.lower())
        for w in _HOTWORD_CJK_RE.findall(stem):
            add(w)
        for w in _HOTWORD_CJK_RE.findall(str(chunk.get("heading", ""))):
            add(w)
        if len(words) >= limit:
            break
    return words[:limit]


class PipelineEngine(EngineBase):
    kind = "pipeline"

    def __init__(self, kb_store, mcp_manager):
        self.kb = kb_store
        self.mcp = mcp_manager
        self._stop = asyncio.Event()
        self._speaking_task: asyncio.Task | None = None
        self._respond_tasks: set[asyncio.Task] = set()
        self._respond_lock = asyncio.Lock()
        self._pending_q_task: asyncio.Task | None = None
        self._agent_audible = False
        self._tool_filler = DEFAULT_TOOL_FILLER
        self.arbiter: TurnArbiter | None = None

    async def stop(self) -> None:
        self._stop.set()
        self._agent_audible = False
        if self._speaking_task:
            self._speaking_task.cancel()
        if self._pending_q_task:
            self._pending_q_task.cancel()
        for task in list(self._respond_tasks):
            task.cancel()

    def _responding(self) -> bool:
        """True while any response is being generated or spoken."""
        return any(not task.done() for task in self._respond_tasks)

    # --------------------------------------------------------------- inject
    async def inject_text(self, text: str) -> None:
        """Console trigger ("让星火说"): respond as if directly addressed."""
        io = getattr(self, "_io", None)
        if io is None or not text.strip():
            return
        config = getattr(self, "_config", None) or load_config()
        history = getattr(self, "_history", None)
        meeting_log = getattr(self, "_meeting_log", None)
        if history is None or meeting_log is None:
            return
        text = text.strip()
        await io.send_event({"type": "transcript.final", "speaker": "控制台",
                             "text": text})
        meeting_log.append({"speaker": "控制台", "text": text})
        kb_hits = await asyncio.to_thread(self.kb.search, text, 4)
        metrics = {"engine": self.kind, "channel": "控制台"}
        if self.arbiter:
            self.arbiter.force_next()
        await self._tracked_respond(io, self._llm, self._tts, config, text,
                                    "控制台", kb_hits, meeting_log, history,
                                    metrics, time.time(),
                                    config.get("meeting_wake_names") or ["星火"])

    # ------------------------------------------------------------------ run
    async def run(self, io: SessionIO) -> None:
        config = load_config()
        self.arbiter = TurnArbiter(config.get("meeting_wake_names"))
        asr = build_asr(config)
        llm = build_llm(config)
        tts = build_tts(config)
        tts_warm = getattr(tts, "warm", None)
        if tts_warm is not None:
            try:
                await tts_warm()  # 预建连接，消除首句握手延迟；失败不阻断（发声时自动重连）
            except Exception as exc:  # noqa: BLE001
                print(f"[pipeline] TTS 预热失败（首次发声时重试）: {exc}", flush=True)
        kb_prewarm = getattr(self.kb, "prewarm", None)
        if kb_prewarm is not None:
            try:
                # 预热 OV embedding 模型，消除服务重启后首次检索的冷启动尖峰
                await asyncio.to_thread(kb_prewarm)
            except Exception as exc:  # noqa: BLE001
                print(f"[pipeline] 知识库预热失败（不影响会议）: {exc}", flush=True)
        silence_ms = int(config.get("meeting_silence_ms", 700))
        min_endpoint_ms = int(config.get("meeting_min_endpoint_ms", 250))
        wake_names = config.get("meeting_wake_names") or ["星火"]
        set_hw = getattr(asr, "set_hotwords", None)
        if set_hw is not None:
            try:
                self.kb.ensure_loaded()
                hotwords = _extract_hotwords(self.kb, wake_names)
                set_hw(hotwords)
                print(f"[pipeline] ASR 热词注入 {len(hotwords)} 个", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[pipeline] ASR 热词提取失败（忽略）: {exc}", flush=True)
        partial_asr_on = _truthy(config.get("meeting_partial_asr"), True)
        answer_questions = _truthy(config.get("meeting_answer_questions"), True)
        pending_q_ms = int(config.get("meeting_pending_question_ms", 2000))
        self._tool_filler = _filler_phrase(config.get("meeting_tool_filler"))
        # 免唤醒模式（meeting_wake_required=false）：主人麦克风直接说话即答；
        # 会议声道仍按点名/移交/无人接话仲裁，避免见话就接。
        wake_required = _truthy(config.get("meeting_wake_required"), True)
        triggers = self.arbiter.describe_triggers()
        if not wake_required:
            triggers = ["免唤醒：开始对话后，您直接说话即可（仅主人麦克风）",
                        "会议声道仍按点名/移交触发", "控制台手动触发：让星火说"]
        await io.send_event({"type": "session.state", "engine": self.kind,
                             "status": "connected",
                             "triggers": triggers,
                             "llm": getattr(llm, "name", "llm"),
                             "llm_model": getattr(llm, "model", "")})
        await io.send_event({"type": "agent.state", "state": "listening"})
        if not asr.configured():
            await io.send_event({"type": "session.error",
                                 "error": "语音识别未配置：请在设置面板填写火山语音 Key（或切换 Apple 本地识别）"})

        noise_floor = 200.0
        channels = {0: _ChannelState(), 1: _ChannelState()}
        prerolls = {0: deque(maxlen=15), 1: deque(maxlen=15)}  # ~300ms each
        last_prefetch_at = 0.0
        meeting_log: deque[dict] = deque(maxlen=24)
        history: list[dict] = []
        utter_seq = 0
        self._io, self._llm, self._tts, self._config = io, llm, tts, config
        self._history, self._meeting_log = history, meeting_log

        # ------------------------------------------------------- nested flows
        async def pending_question_flow(channel: int, text: str, seq: int):
            """Direct question + a beat of silence: the agent picks it up."""
            try:
                await asyncio.sleep(pending_q_ms / 1000)
            except asyncio.CancelledError:
                return
            if (seq != utter_seq or self._responding() or self._agent_audible
                    or any(st.speaking for st in channels.values())):
                return
            speaker = CHANNEL_NAMES.get(channel, f"声道{channel}")
            await io.send_event({"type": "agent.state", "state": "thinking",
                                 "reason": "unanswered-question"})
            metrics = {"engine": self.kind, "channel": speaker,
                       "trigger": "unanswered-question"}
            kb_hits = self.kb.take_warm(text)
            if kb_hits is None:
                fetch_t0 = time.time()
                kb_hits = await asyncio.to_thread(self.kb.search, text, 4)
                metrics["retrieval_ms"] = int((time.time() - fetch_t0) * 1000)
            else:
                metrics["retrieval_ms"] = 0
            await self._tracked_respond(io, llm, tts, config, text, speaker,
                                        kb_hits, meeting_log, history, metrics,
                                        time.time(), wake_names)

        async def partial_pass(channel: int, pcm: bytes):
            """Interim ASR while the speaker is still talking (边说边转).

            Emits a replaceable partial transcript and, when the wake name or a
            handoff pattern is already visible, warms the KB ahead of the
            endpoint so the final reply starts with retrieval cost ~ 0.
            """
            speaker = CHANNEL_NAMES.get(channel, f"声道{channel}")
            try:
                result = await asr.transcribe(pcm, BROWSER_RATE)
            except Exception:  # noqa: BLE001 - partials are best-effort
                return
            text = result.text.strip()
            if not text:
                return
            await io.send_event({"type": "transcript.partial", "speaker": speaker,
                                 "text": text, "partial_asr": True})
            if self.arbiter and self.arbiter.peek(text):
                hits = await asyncio.to_thread(self.kb.warm, text)
                await io.send_event({"type": "kb.prefetch", "query": text[:60],
                                     "hits": len(hits), "early": True})

        async def handle_utterance(channel: int, pcm: bytes, endpoint_now: float):
            nonlocal utter_seq
            utter_seq += 1
            seq = utter_seq
            if self._pending_q_task and not self._pending_q_task.done():
                self._pending_q_task.cancel()
            await io.send_event({"type": "agent.state", "state": "thinking"})
            metrics = {"engine": self.kind, "channel": CHANNEL_NAMES.get(channel, "?")}
            # 噪声闸门：峰值不足、或有效发声时长不足都按环境杂音忽略——
            # 点击/磕碰声发声通常 <150ms，真实问句至少 150ms 以上连续发声。
            # （原 300ms 对断续/远场拾音过严：B 端笔记本链路语音易碎成片被误杀）
            peak = 0.0
            voiced_ms = 0
            for i in range(0, max(0, len(pcm) - 640), 640):  # 20ms 帧 @16k
                level = rms_level(pcm[i:i + 640])
                peak = max(peak, level)
                if level > 600.0:
                    voiced_ms += 20
            if peak < 1500.0 or voiced_ms < 150:
                print(f"[pipeline] 噪声忽略(峰值 {peak:.0f}, 发声 {voiced_ms}ms)", flush=True)
                await io.send_event({"type": "agent.state", "state": "listening"})
                return
            try:
                result = await asr.transcribe(pcm, BROWSER_RATE)
            except Exception as exc:  # noqa: BLE001
                duration_s = len(pcm) / 2 / BROWSER_RATE
                if "No speech detected" in str(exc):
                    # ASR 判定非语音 = 杂音触发的正常拒识，只记服务端日志，不打扰页面
                    print(f"[pipeline] ASR 拒识非语音(时长 {duration_s:.1f}s 峰值 {peak:.0f})",
                          flush=True)
                    await io.send_event({"type": "agent.state", "state": "listening"})
                    return
                # 其余失败：记录时长/峰值并落盘样本，便于区分「采到静音」与「有语音未识别」。
                dump = ""
                try:
                    dump_path = Path(tempfile.gettempdir()) / f"spark_asr_fail_{int(endpoint_now)}.wav"
                    dump_path.write_bytes(build_wav(pcm, BROWSER_RATE))
                    dump = f"，样本 {dump_path}"
                except Exception:  # noqa: BLE001
                    pass
                print(f"[pipeline] ASR 失败: {exc} | 时长 {duration_s:.1f}s 峰值 {peak:.0f}{dump}",
                      flush=True)
                await io.send_event({"type": "session.error",
                                     "error": (f"ASR 失败：{type(exc).__name__}: {exc}"
                                               f"（时长 {duration_s:.1f}s，峰值 {peak:.0f}{dump}）")[:300]})
                await io.send_event({"type": "agent.state", "state": "listening"})
                return
            metrics["asr_ms"] = result.duration_ms
            speaker = CHANNEL_NAMES.get(channel, f"声道{channel}")
            text = result.text.strip()
            await io.send_event({"type": "transcript.final", "speaker": speaker, "text": text})
            if not text:
                await io.send_event({"type": "agent.state", "state": "listening"})
                return
            meeting_log.append({"speaker": speaker, "text": text})

            # 停止指令（T14）：语音"别说了/停下"立即中断当前发声与生成
            if _is_stop_command(text):
                if self._speaking_task:
                    self._speaking_task.cancel()
                for task in list(self._respond_tasks):
                    task.cancel()
                await io.send_event({"type": "clear_audio"})
                await io.send_event({"type": "agent.state", "state": "listening",
                                     "reason": "stop-command"})
                print("[pipeline] 收到停止指令（语音），已静音", flush=True)
                return

            decision = self.arbiter.decide(speaker, text)
            # 免唤醒模式下，聊天文字与主人语音同权：直接作答（演示场景）
            if decision.action == "listen" and not wake_required:
                decision = TurnDecision("speak", "chat-no-wake")
            if decision.action == "listen" and not wake_required and channel == 0:
                decision = TurnDecision("speak", "owner-mic")
            remainder = text
            if decision.matched:
                remainder = remainder.replace(decision.matched, "", 1)
            remainder = re.sub(r"[，。！？、,.!?:：\s]+", "", remainder)

            if decision.action == "listen":
                # Unclear speech from the user's own mic is confirmed instead of
                # being silently dropped ("听不清按语义确认"); meeting-channel
                # chatter only warms the KB context.
                if speaker == CHANNEL_NAMES.get(0):
                    question = clarification_for(remainder, None, result.confidence)
                    if question:
                        await self._speak(io, tts, question, metrics, endpoint_now)
                        history.append({"role": "user", "content": text})
                        history.append({"role": "assistant", "content": question})
                        return
                # Speculative prefetch: warm context while others keep talking.
                hits = await asyncio.to_thread(self.kb.warm, text)
                await io.send_event({"type": "kb.prefetch", "query": text[:60],
                                     "hits": len(hits)})
                await io.send_event({"type": "agent.state", "state": "listening"})
                # A direct question nobody picks up: wait a beat, then answer.
                if answer_questions and self.arbiter.is_direct_question(text):
                    self._pending_q_task = asyncio.create_task(
                        pending_question_flow(channel, text, seq))
                return

            kb_hits = self.kb.take_warm(text)
            if kb_hits is None:
                fetch_t0 = time.time()
                kb_hits = await asyncio.to_thread(self.kb.search, text, 4)
                metrics["retrieval_ms"] = int((time.time() - fetch_t0) * 1000)
            else:
                metrics["retrieval_ms"] = 0
                await io.send_event({"type": "kb.prefetch", "query": text[:60],
                                     "hits": len(kb_hits), "warm": True})

            # 歧义澄清（meeting_disambiguate 开启时才用）：知识库扩容后 top1/top2
            # 分数经常天然接近，"你说的是X还是Y"的误澄清会陷入反问循环，
            # 默认关闭——直接走 B/本地应答，答错也比连环反问强。
            if _truthy(config.get("meeting_disambiguate"), False):
                question = clarification_for(remainder, kb_hits, result.confidence)
                if question and decision.reason not in ("manual",):
                    await self._speak(io, tts, question, metrics, endpoint_now)
                    history.append({"role": "user", "content": text})
                    history.append({"role": "assistant", "content": question})
                    return

            await self._tracked_respond(io, llm, tts, config, text, speaker,
                                        kb_hits, meeting_log, history, metrics,
                                        endpoint_now, wake_names)

        # ------------------------------------------------------------ main loop
        latest_screen = {"b64": None, "ts": 0.0}  # 页面侧投屏截帧（screen.frame 控制帧更新）
        self._latest_screen = latest_screen

        async def handle_chat_message(control: dict):
            """会议聊天文字：进会议日志（agent 上下文可见）；
            点名/直接问句与语音同权触发应答，保持与 ASR 声道一致的仲裁。"""
            nonlocal utter_seq
            text = str(control.get("text", "")).strip()
            speaker = str(control.get("speaker", "会议聊天")).strip()[:20] or "会议聊天"
            if not text:
                return
            meeting_log.append({"speaker": speaker, "text": text})
            await io.send_event({"type": "transcript.final", "speaker": speaker, "text": text})
            print(f"[pipeline] 会议聊天[{speaker}]: {text[:60]}", flush=True)
            # 停止指令（T14）：立即中断当前发声与生成
            if _is_stop_command(text):
                if self._speaking_task:
                    self._speaking_task.cancel()
                for task in list(self._respond_tasks):
                    task.cancel()
                await io.send_event({"type": "clear_audio"})
                await io.send_event({"type": "agent.state", "state": "listening",
                                     "reason": "stop-command"})
                print("[pipeline] 收到停止指令（聊天），已静音", flush=True)
                return
            decision = self.arbiter.decide(speaker, text)
            # 免唤醒模式下，聊天文字与主人语音同权：直接作答（演示场景）
            if decision.action == "listen" and not wake_required:
                decision = TurnDecision("speak", "chat-no-wake")
            if decision.action != "listen" and not self._responding() and not self._agent_audible:
                utter_seq += 1
                kb_hits = await asyncio.to_thread(self.kb.search, text, 4)
                metrics = {"engine": self.kind, "channel": speaker, "trigger": "chat"}
                await self._tracked_respond(io, llm, tts, config, text, speaker,
                                            kb_hits, meeting_log, history, metrics,
                                            time.time(), wake_names)
            elif answer_questions and self.arbiter.is_direct_question(text):
                utter_seq += 1
                self._pending_q_task = asyncio.create_task(
                    pending_question_flow(1, text, utter_seq))

        while not self._stop.is_set() and not io.closed.is_set():
            # 页面侧输入（会议聊天文字 / 投屏截帧）：每轮先排空控制队列
            while True:
                try:
                    control = io.controls.get_nowait()
                except asyncio.QueueEmpty:
                    break
                ctype = control.get("type", "")
                if ctype == "chat.message":
                    await handle_chat_message(control)
                elif ctype == "screen.frame":
                    latest_screen["b64"] = str(control.get("image_b64", "")) or None
                    latest_screen["ts"] = time.time()
            try:
                channel, pcm = await asyncio.wait_for(io.frames.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            now = time.time()
            level = rms_level(pcm)
            state = channels.get(channel)
            if state is None:
                channel, state = 0, channels[0]
            preroll = prerolls[channel]

            # Barge-in. While the agent's voice is audible, speech on either
            # channel cuts in; while it is still thinking, only the owner's
            # mic cancels — meeting chatter must not kill an in-flight LLM
            # stream that has not said anything yet.
            barge_level = max(500.0, noise_floor * 3.2)
            speaking_barge = self._agent_audible and level > barge_level
            thinking_barge = (channel == 0 and not self._agent_audible
                              and self._responding() and level > barge_level)
            if speaking_barge or thinking_barge:
                state.barge_frames += 1
            else:
                state.barge_frames = 0
            # 持续 ~120ms 的高声即认定真人插话（原 240ms，打断提速）：
            # 点击/磕碰等瞬时噪声仍被过滤，但真人插话的响应延迟减半。
            if state.barge_frames >= 6:
                state.barge_frames = 0
                if self._speaking_task:
                    self._speaking_task.cancel()
                for task in list(self._respond_tasks):
                    task.cancel()
                await io.send_event({"type": "clear_audio"})
                await io.send_event({"type": "agent.state", "state": "listening",
                                     "reason": "barge-in"})
                # Keep the interrupting speech: if the channel was already
                # mid-utterance keep accumulating, else start a fresh one.
                if not state.speaking:
                    state.begin(preroll, now)
                state.last_voice_at = now
                state.utterance.append(pcm)
                preroll.append(pcm)
                continue

            threshold = max(420.0, noise_floor * 2.6)
            if level > threshold:
                if not state.speaking:
                    state.begin(preroll, now)
                state.last_voice_at = now
                state.utterance.append(pcm)
                if (partial_asr_on and asr.configured()
                        and now - state.speech_started_at >= PARTIAL_MIN_SPEECH_S
                        and now - state.last_partial_at >= PARTIAL_INTERVAL_S
                        and state.partial_count < PARTIAL_MAX_PER_UTTERANCE):
                    state.partial_count += 1
                    state.last_partial_at = now
                    asyncio.create_task(partial_pass(channel, b"".join(state.utterance)))
            else:
                noise_floor = noise_floor * 0.97 + level * 0.03
                if state.speaking:
                    state.utterance.append(pcm)
                    silence_ms_now = (now - state.last_voice_at) * 1000
                    utter_ms = (now - state.speech_started_at) * 1000
                    if silence_ms_now >= silence_ms and utter_ms >= min_endpoint_ms:
                        state.speaking = False
                        pcm_blob = b"".join(state.utterance)
                        state.utterance = []
                        asyncio.create_task(handle_utterance(channel, pcm_blob, now))
            preroll.append(pcm)

            # Periodic prefetch from the live meeting log even without endpoint.
            if (not any(st.speaking for st in channels.values())
                    and now - last_prefetch_at > 8 and meeting_log):
                last_prefetch_at = now
                latest = meeting_log[-1]["text"]
                asyncio.create_task(asyncio.to_thread(self.kb.warm, latest))

        await io.send_event({"type": "session.state", "engine": self.kind,
                             "status": "stopped"})

    # -------------------------------------------------------------- respond
    async def _tracked_respond(self, io, llm, tts, config, text, speaker,
                               kb_hits, meeting_log, history, metrics,
                               endpoint_now, wake_names):
        """Serialize responses (one voice at a time) and make them cancellable."""
        current = asyncio.current_task()
        self._respond_tasks.add(current)
        print(f"[pipeline] 应答开始: {text[:40]}", flush=True)
        t0 = time.time()
        try:
            async with self._respond_lock:
                await self._respond(io, llm, tts, config, text, speaker, kb_hits,
                                    meeting_log, history, metrics, endpoint_now,
                                    wake_names)
            print(f"[pipeline] 应答完成: {text[:40]} | {time.time() - t0:.1f}s", flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"[pipeline] 应答异常: {text[:40]} | {type(exc).__name__}: {exc}",
                  flush=True)
            await io.send_event({"type": "session.error",
                                 "error": friendly_api_error(exc, "应答")})
        finally:
            self._respond_tasks.discard(current)

    async def _ask_b_agent(self, config, text: str, speaker: str,
                           metrics: dict, endpoint_now: float) -> str:
        """把唤醒后的问题转发给 B 智能体（理解中枢：长上下文+知识库+MCP）。

        成功返回 B 的答案文本；B 未启动/超时/报错返回 ""（调用方回退本地流程）。
        """
        base = str(config.get("b_agent_url", "http://127.0.0.1:8766")).rstrip("/")

        def _post() -> dict:
            body = json.dumps({"question": text, "speaker": speaker},
                              ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                base + "/ask", data=body,
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))

        try:
            result = await asyncio.to_thread(_post)
        except Exception as exc:  # noqa: BLE001
            print(f"[pipeline] B 智能体不可达，回退本地回答: {type(exc).__name__}: {exc}",
                  flush=True)
            return ""
        if not result.get("ok") or not str(result.get("answer", "")).strip():
            print(f"[pipeline] B 智能体无有效答案，回退本地: {result.get('error', '')[:100]}",
                  flush=True)
            return ""
        metrics["b_agent_ms"] = result.get("latency_ms", 0)
        metrics["b_agent_ctx"] = result.get("context_lines", 0)
        print(f"[pipeline] B 智能体答案（上下文 {metrics['b_agent_ctx']} 条，"
              f"{metrics['b_agent_ms']}ms）: {str(result['answer'])[:60]}", flush=True)
        return str(result["answer"]).strip()

    async def _respond(self, io, llm, tts, config, text, speaker, kb_hits,
                       meeting_log, history, metrics, endpoint_now, wake_names):
        # B 智能体优先：唤醒后的问题交给理解中枢（长会议上下文+知识库+MCP 深度回答）；
        # B 不可用时静默回退本地快速回答，保证会议链路永不失声。
        if _truthy(config.get("b_agent_enabled"), False):
            b_answer = await self._ask_b_agent(config, text, speaker, metrics, endpoint_now)
            if b_answer:
                history.append({"role": "user", "content": text})
                history.append({"role": "assistant", "content": b_answer})
                await self._speak(io, tts, b_answer, metrics, endpoint_now)
                return
        persona = str(config.get("persona", "")).strip()
        base_prompt = str(config.get(
            "local_voice_system_prompt",
            "你是会议中的AI语音助手，先说结论，语气自然简洁，不抢话，不编造事实。"))
        kb_block = ""
        if kb_hits:
            # 提示词瘦身（B 方案降延迟）：KB 片段 4→2 条，单条 260→220 字
            lines = [f"[{i + 1}] ({h['source']} / {h.get('heading', '')}) {h['text'][:220]}"
                     for i, h in enumerate(kb_hits[:2])]
            kb_block = ("以下是与当前话题相关的知识库片段，回答时优先依据它们；"
                        "回答中不要出现 [1]、[2] 这类引用编号或出处标记；没有依据就明说：\n"
                        + "\n".join(lines))
        context_lines = [f"{item['speaker']}：{item['text']}" for item in list(meeting_log)[-5:]]
        meeting_block = "最近的会议发言：\n" + "\n".join(context_lines) if context_lines else ""
        system = "\n\n".join(part for part in [
            base_prompt,
            f"你正在一场多人会议中，你的唤醒名是「{'、'.join(wake_names)}」。当前对你说话的是「{speaker}」。",
            persona and f"人设：{persona}",
            meeting_block,
            kb_block,
            "回答控制在一到三句话，口语化，先结论后要点。",
        ] if part)

        messages = [{"role": "system", "content": system}] + history[-8:] + [
            {"role": "user", "content": text}]
        # 投屏画面：问题涉及屏幕/文档/演示且 30s 内有截帧时，附图走视觉模型（OpenAI 多模态格式）
        screen = getattr(self, "_latest_screen", None)
        if screen and screen.get("b64") and _wants_screen(text) \
                and time.time() - screen.get("ts", 0) < 30:
            messages[-1] = {"role": "user", "content": [
                {"type": "text",
                 "text": text + "\n（附图：当前会议投屏/视频画面，请结合画面内容回答）"},
                {"type": "image_url",
                 "image_url": {"url": "data:image/jpeg;base64," + screen["b64"]}},
            ]}
        tools = self.mcp.as_llm_tools()
        history.append({"role": "user", "content": text})

        llm_t0 = time.time()
        answer = await self._run_llm(io, llm, messages, tools, metrics, llm_t0,
                                     endpoint_now, tts)
        history.append({"role": "assistant", "content": answer})

    async def _run_llm(self, io, llm, messages, tools, metrics, llm_t0,
                       endpoint_now, tts) -> str:
        content_parts: list[str] = []
        sentence_buffer: list[str] = []
        sentence_queue: asyncio.Queue[str | None] = asyncio.Queue()
        speak_task: asyncio.Task | None = None

        async def queue_sentence(text: str):
            nonlocal speak_task
            # 语音里念引用编号毫无意义：模型偶尔模仿知识库的 [1] 标记，播报前剥掉
            text = re.sub(r"[\[【]\s*\d+\s*[\]】]", "", text).strip()
            if not text:
                return
            await sentence_queue.put(text)
            if speak_task is None:
                # Speak while the model keeps generating: playback of sentence N
                # overlaps generation of sentence N+1 instead of waiting for the
                # whole answer (the single biggest latency win).
                speak_task = asyncio.create_task(
                    self._speak_sentences(io, tts, sentence_queue, metrics,
                                          endpoint_now))

        first_token = True
        rounds = 0
        try:
            while rounds < 3:
                rounds += 1
                pending_tool_calls: list[dict] = []
                try:
                    async for item in llm.stream_chat(messages, tools=tools or None):
                        if item["type"] == "delta":
                            if first_token:
                                metrics["llm_ttft_ms"] = int((time.time() - llm_t0) * 1000)
                                first_token = False
                                await io.send_event({"type": "agent.state", "state": "thinking"})
                            content_parts.append(item["text"])
                            sentence_buffer.append(item["text"])
                            await io.send_event({"type": "transcript.partial",
                                                 "speaker": "agent", "text": item["text"]})
                            joined = "".join(sentence_buffer)
                            while True:
                                cut = next((i for i, ch in enumerate(joined)
                                            if ch in SENTENCE_ENDS), -1)
                                if cut == -1 and len(joined) < 64:
                                    break
                                if cut == -1:
                                    cut = len(joined) - 1
                                sentence = joined[:cut + 1].strip()
                                joined = joined[cut + 1:]
                                sentence_buffer = [joined]
                                await queue_sentence(sentence)
                        elif item["type"] == "tool_call":
                            pending_tool_calls.append(item)
                        elif item["type"] == "error":
                            await io.send_event({"type": "session.error",
                                                 "error": item["error"][:300]})
                        elif item["type"] == "done":
                            pass
                except Exception as exc:  # noqa: BLE001
                    print(f"[pipeline] LLM 异常: {type(exc).__name__}: {exc}", flush=True)
                    await io.send_event({"type": "session.error",
                                         "error": friendly_api_error(exc, "LLM 调用")})
                    break
                if not pending_tool_calls:
                    break
                # Execute tool calls; what was already generated is being spoken.
                tail = "".join(sentence_buffer).strip()
                sentence_buffer = []
                await queue_sentence(tail)
                if self._tool_filler:
                    # Never go silent while tools run: say a short filler first.
                    await queue_sentence(self._tool_filler)
                await io.send_event({"type": "agent.state", "state": "thinking",
                                     "reason": "tool"})
                assistant_tc = [{
                    "id": tc["id"], "type": "function",
                    "function": {"name": tc["name"],
                                 "arguments": json.dumps(tc["arguments"], ensure_ascii=False)},
                } for tc in pending_tool_calls]
                messages = messages + [{"role": "assistant", "content": "".join(content_parts) or None,
                                        "tool_calls": assistant_tc}]
                for tc in pending_tool_calls:
                    await io.send_event({"type": "mcp.call", "name": tc["name"],
                                         "arguments": tc["arguments"]})
                    try:
                        output = await asyncio.wait_for(
                            self.mcp.call(tc["name"], tc["arguments"]), timeout=30)
                    except Exception as exc:  # noqa: BLE001
                        output = f"工具调用失败：{exc}"
                    await io.send_event({"type": "mcp.result", "name": tc["name"],
                                         "output": str(output)[:400]})
                    messages.append({"role": "tool", "tool_call_id": tc["id"],
                                     "content": str(output)[:2000]})
                content_parts = []
                first_token = True

            tail = "".join(sentence_buffer).strip()
            await queue_sentence(tail)
            answer = re.sub(r"[\[【]\s*\d+\s*[\]】]", "",
                            "".join(content_parts)).strip() or "（没有生成内容）"
            await io.send_event({"type": "transcript.final", "speaker": "agent",
                                 "text": answer})
            sentence_queue.put_nowait(None)
            if speak_task is not None:
                try:
                    await speak_task
                except asyncio.CancelledError:
                    pass
            return answer
        except asyncio.CancelledError:
            # Barge-in during generation: stop the voice too, then propagate.
            if speak_task is not None:
                speak_task.cancel()
                try:
                    await speak_task
                except asyncio.CancelledError:
                    pass
            await io.send_event({"type": "agent.state", "state": "listening",
                                 "reason": "interrupted"})
            raise

    # ---------------------------------------------------------------- speak
    async def _speak(self, io, tts, text, metrics, endpoint_now):
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        queue.put_nowait(text)
        queue.put_nowait(None)
        await io.send_event({"type": "transcript.final", "speaker": "agent", "text": text})
        await self._speak_sentences(io, tts, queue, metrics, endpoint_now)

    async def _speak_sentences(self, io, tts, queue, metrics, endpoint_now):
        async def speak_worker():
            first_audio = True

            async def emit(pcm_chunk: bytes):
                nonlocal first_audio
                if not pcm_chunk:
                    return
                if first_audio:
                    self._agent_audible = True
                    metrics["tts_ttfa_ms"] = int((time.time() - (endpoint_now or time.time())) * 1000)
                    metrics["total_ms"] = metrics.get("tts_ttfa_ms")
                    await io.send_event({"type": "metrics.turn", **metrics})
                    first_audio = False
                await io.send_event({"type": "agent.state", "state": "speaking"})
                for i in range(0, len(pcm_chunk), 4800):  # 100ms @24k
                    await io.send_audio(pcm_chunk[i:i + 4800])
                    await asyncio.sleep(0.02)

            while True:
                sentence = await queue.get()
                if sentence is None:
                    break
                stream = getattr(tts, "synthesize_stream", None)
                try:
                    if stream is not None:
                        async for chunk in stream(sentence):
                            await emit(chunk)
                    else:
                        await emit(await tts.synthesize(sentence))
                except Exception as exc:  # noqa: BLE001
                    print(f"[pipeline] TTS 异常: {type(exc).__name__}: {exc}", flush=True)
                    await io.send_event({"type": "session.error",
                                         "error": friendly_api_error(exc, "TTS")})
                    continue
            await io.send_event({"type": "agent.state", "state": "listening"})

        self._speaking_task = asyncio.current_task()
        try:
            await speak_worker()
        except asyncio.CancelledError:
            await io.send_event({"type": "agent.state", "state": "listening",
                                 "reason": "interrupted"})
            raise
        finally:
            self._speaking_task = None
            self._agent_audible = False

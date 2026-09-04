"""In-process smoke test for the pipeline engine (no sockets, no API calls).

Feeds synthetic PCM frames through VAD -> (mock) ASR -> turn arbitration ->
(mock) LLM -> (mock) TTS and asserts the meeting behaviour:
  A. wake-name utterance triggers an agent reply with metrics
  B. non-addressed utterance only prefetches KB context
  C. unclear short utterance triggers a semantic clarification question
  D. console inject (让星火说) triggers a reply without ASR
  E. mic and meeting channels endpoint independently (no mixing)
  F. direct question nobody answers -> agent picks it up after a beat
  G. direct question superseded by new speech -> agent stays quiet
  H. interim ASR partials stream while speaking + early wake prefetch
  I. tool call speaks the filler line before executing
"""

import asyncio
import math
import array
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import server.engines.pipeline as pipeline_mod  # noqa: E402
from server.asr.base import ASRResult  # noqa: E402
from server.engines.base import SessionIO  # noqa: E402
from server.engines.pipeline import PipelineEngine  # noqa: E402
from server.rag.store import KnowledgeStore  # noqa: E402

ANSWER = "结论：风险可控，建议先灰度。"

TEST_CONFIG = {
    "meeting_wake_names": ["星火"],
    "meeting_silence_ms": 700,
    "meeting_min_endpoint_ms": 250,
    "meeting_partial_asr": False,
    "meeting_answer_questions": True,
    "meeting_pending_question_ms": 500,
    "meeting_tool_filler": "我查一下，稍等。",
}


class FakeASR:
    text = "星火，这个方案的风险是什么？"

    def __init__(self, config):
        pass

    def configured(self):
        return True

    async def transcribe(self, pcm, rate):
        return ASRResult(text=self.text, duration_ms=12)


class FakeLLM:
    calls = 0
    tool_mode = False  # first call asks for a tool, second answers

    def __init__(self, config):
        pass

    async def stream_chat(self, messages, tools=None, model=None):
        FakeLLM.calls += 1
        if FakeLLM.tool_mode and FakeLLM.calls == 1:
            yield {"type": "tool_call", "id": "t1", "name": "demo__ping",
                   "arguments": {}}
            yield {"type": "done", "content": ""}
            return
        for i in range(0, len(ANSWER), 4):
            yield {"type": "delta", "text": ANSWER[i:i + 4]}
        yield {"type": "done", "content": ANSWER}


class FakeTTS:
    spoken: list[str] = []

    def __init__(self, config):
        pass

    async def synthesize(self, text, voice=None):
        FakeTTS.spoken.append(text)
        return b"\x01\x00" * 1200  # 50ms silence-ish pcm

    async def synthesize_stream(self, text, voice=None):
        FakeTTS.spoken.append(text)
        yield b"\x01\x00" * 600
        yield b"\x01\x00" * 600


pipeline_mod.build_asr = lambda config: FakeASR(config)
pipeline_mod.build_llm = lambda config: FakeLLM(config)
pipeline_mod.build_tts = lambda config: FakeTTS(config)


class McpStub:
    def as_llm_tools(self):
        return []

    async def call(self, name, arguments):
        return "pong"


class FakeIO(SessionIO):
    def __init__(self):
        super().__init__()
        self.events = []
        self.audio_bytes = 0

    async def send_event(self, event):
        self.events.append(event)

    async def send_audio(self, pcm):
        self.audio_bytes += len(pcm)

    def finals(self, speaker=None):
        return [e for e in self.events
                if e["type"] == "transcript.final" and (speaker is None or e.get("speaker") == speaker)]

    def types(self):
        return [e["type"] for e in self.events]


def tone_frame(frame_idx, freq=440, amp=8000):
    buf = array.array("h")
    for i in range(320):
        t = (frame_idx * 320 + i) / 16000
        buf.append(int(amp * math.sin(2 * math.pi * freq * t)))
    return buf.tobytes()


async def feed_utterance(io, voice_frames=50, silence_frames=48, channel=0,
                         pace_voice=False):
    for i in range(voice_frames):
        io.frames.put_nowait((channel, tone_frame(i)))
        if pace_voice:
            await asyncio.sleep(0.03)  # stretch speech across wall-clock time
    for _ in range(silence_frames):
        io.frames.put_nowait((channel, b"\x00" * 640))
        await asyncio.sleep(0.025)  # real-time pacing so wall-clock VAD endpoint fires


async def feed_two_channels(io, voice_frames=40, silence_frames=48):
    """Interleaved mic + meeting speech: must endpoint as two utterances."""
    for i in range(voice_frames):
        io.frames.put_nowait((0, tone_frame(i, freq=440)))
        io.frames.put_nowait((1, tone_frame(i, freq=880)))
        await asyncio.sleep(0.01)
    for _ in range(silence_frames):
        io.frames.put_nowait((0, b"\x00" * 640))
        io.frames.put_nowait((1, b"\x00" * 640))
        await asyncio.sleep(0.025)


def isolated_kb() -> KnowledgeStore:
    kb = KnowledgeStore(ROOT, "docs/kb")
    # Never reuse the production index (it may point at the company kb_dir).
    kb.index_path = Path(tempfile.mkdtemp(prefix="spark_kb_test_")) / ".kb_index.json"
    return kb


async def run_scenario(asr_text, config_overrides=None, feeder=feed_utterance):
    config = dict(TEST_CONFIG)
    config.update(config_overrides or {})
    pipeline_mod.load_config = lambda: dict(config)
    FakeASR.text = asr_text
    FakeLLM.calls = 0
    FakeLLM.tool_mode = False
    FakeTTS.spoken = []
    kb = isolated_kb()
    engine = PipelineEngine(kb, McpStub())
    io = FakeIO()
    task = asyncio.create_task(engine.run(io))
    await asyncio.sleep(0.15)
    await feeder(io)
    await asyncio.sleep(2.2)
    io.closed.set()
    try:
        await asyncio.wait_for(task, timeout=6)
    except asyncio.TimeoutError:
        task.cancel()
    return io, engine


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}{(' — ' + detail) if detail and not condition else ''}")
    if not condition:
        sys.exit(1)


async def main():
    # A. wake-name triggers reply
    io, _ = await run_scenario("星火，这个方案的风险是什么？")
    check("A1 会话连接事件", any(e["type"] == "session.state" and e["status"] == "connected"
                               for e in io.events))
    check("A2 用户字幕定稿", any(e["text"].startswith("星火") for e in io.finals("我")))
    check("A3 智能体回答", any(ANSWER in e["text"] for e in io.finals("agent")),
          str([e["text"] for e in io.finals("agent")]))
    check("A4 音频下发", io.audio_bytes > 0, f"audio={io.audio_bytes}")
    check("A5 延迟指标", any(e["type"] == "metrics.turn" and e.get("llm_ttft_ms") is not None
                            for e in io.events), str(io.types()))
    check("A6 状态流转", "agent.state" in io.types() and
          any(e["state"] == "speaking" for e in io.events if e["type"] == "agent.state"))

    # B. non-addressed utterance -> prefetch only
    io, _ = await run_scenario("今天先把项目进度过一遍")
    check("B1 无智能体发言", not io.finals("agent"), str([e["text"] for e in io.finals("agent")]))
    check("B2 触发知识库预热", any(e["type"] == "kb.prefetch" for e in io.events), str(io.types()))

    # C. addressed but unclear -> clarification, no LLM call
    FakeLLM.calls = 0
    io, _ = await run_scenario("星火，嗯")
    clarified = [e["text"] for e in io.finals("agent")]
    check("C1 点名但不清楚→澄清", any("没听全" in t or "再说一遍" in t for t in clarified), str(clarified))
    check("C2 澄清不调用LLM", FakeLLM.calls == 0, f"calls={FakeLLM.calls}")

    # C3. non-addressed unclear utterance from the user's own mic -> clarification
    io, _ = await run_scenario("嗯")
    clarified = [e["text"] for e in io.finals("agent")]
    check("C3 未点名但不清楚→也确认", any("没听全" in t or "再说一遍" in t for t in clarified), str(clarified))

    # D. console inject
    pipeline_mod.load_config = lambda: dict(TEST_CONFIG)
    FakeLLM.calls = 0
    FakeLLM.tool_mode = False
    kb = isolated_kb()
    engine = PipelineEngine(kb, McpStub())
    io = FakeIO()
    task = asyncio.create_task(engine.run(io))
    await asyncio.sleep(0.15)
    await engine.inject_text("帮我用一句话介绍产品")
    await asyncio.sleep(1.5)
    io.closed.set()
    try:
        await asyncio.wait_for(task, timeout=6)
    except asyncio.TimeoutError:
        task.cancel()
    check("D1 注入触发回答", any(ANSWER in e["text"] for e in io.finals("agent")),
          str([e["text"] for e in io.finals("agent")]))

    # E. two channels endpoint independently (no utterance mixing)
    io, _ = await run_scenario("今天先把项目进度过一遍", feeder=feed_two_channels)
    check("E1 麦克风声道独立成句", len(io.finals("我")) == 1, str(io.finals("我")))
    check("E2 会议声道独立成句", len(io.finals("会议")) == 1, str(io.finals("会议")))

    # F. direct question nobody answers -> agent picks it up after a beat
    io, _ = await run_scenario("你觉得这个方案怎么样？")
    check("F1 无人接话→主动回答", any(ANSWER in e["text"] for e in io.finals("agent")),
          str([e["text"] for e in io.finals("agent")]))
    check("F2 标注接话原因", any(e.get("reason") == "unanswered-question"
                                for e in io.events if e["type"] == "agent.state"), str(io.types()))

    # G. direct question superseded by new speech -> agent stays quiet
    pipeline_mod.load_config = lambda: dict(TEST_CONFIG)
    FakeASR.text = "你觉得这个方案怎么样？"
    FakeLLM.calls = 0
    FakeLLM.tool_mode = False
    kb = isolated_kb()
    engine = PipelineEngine(kb, McpStub())
    io = FakeIO()
    task = asyncio.create_task(engine.run(io))
    await asyncio.sleep(0.15)
    # First utterance endpoints quickly (36 silence frames ≈ 900ms); the
    # follow-up starts inside the 500ms pickup window, so the channel is
    # still "speaking" when the pending question would fire.
    await feed_utterance(io, channel=1, silence_frames=36)
    await asyncio.sleep(0.05)
    FakeASR.text = "这个方案下周再议"
    await feed_utterance(io, channel=1)
    await asyncio.sleep(2.0)
    io.closed.set()
    try:
        await asyncio.wait_for(task, timeout=6)
    except asyncio.TimeoutError:
        task.cancel()
    check("G1 有人接话→不抢话", not io.finals("agent"), str([e["text"] for e in io.finals("agent")]))

    # H. interim ASR partials while speaking + early wake prefetch
    io, _ = await run_scenario("星火，这个方案的风险是什么？",
                               config_overrides={"meeting_partial_asr": True},
                               feeder=lambda io: feed_utterance(io, pace_voice=True))
    partials = [e for e in io.events
                if e["type"] == "transcript.partial" and e.get("partial_asr")]
    check("H1 说话中出实时草稿", len(partials) >= 1, str(io.types()))
    check("H2 命中唤醒提前预热", any(e["type"] == "kb.prefetch" and e.get("early")
                                   for e in io.events), str(io.types()))

    # I. tool call speaks the filler line before executing
    pipeline_mod.load_config = lambda: dict(TEST_CONFIG)
    FakeASR.text = "星火，这个方案的风险是什么？"
    FakeLLM.calls = 0
    FakeLLM.tool_mode = True
    FakeTTS.spoken = []
    kb = isolated_kb()
    engine = PipelineEngine(kb, McpStub())
    io = FakeIO()
    task = asyncio.create_task(engine.run(io))
    await asyncio.sleep(0.15)
    await feed_utterance(io)
    await asyncio.sleep(2.2)
    io.closed.set()
    try:
        await asyncio.wait_for(task, timeout=6)
    except asyncio.TimeoutError:
        task.cancel()
    check("I1 工具调用两轮LLM", FakeLLM.calls == 2, f"calls={FakeLLM.calls}")
    check("I2 工具调用前垫话", "我查一下，稍等。" in FakeTTS.spoken, str(FakeTTS.spoken))
    check("I3 工具结果回答", any(ANSWER in e["text"] for e in io.finals("agent")),
          str([e["text"] for e in io.finals("agent")]))

    print("pipeline smoke: ALL PASS")


if __name__ == "__main__":
    asyncio.run(main())

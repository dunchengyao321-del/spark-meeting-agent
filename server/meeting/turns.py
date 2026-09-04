"""Turn arbitration: when may the agent speak in a multi-party meeting.

Mirrors human meeting etiquette — never grab the floor; speak when addressed
by wake name, explicitly handed the floor, directly questioned and nobody
else answers, or manually triggered from the console.
"""

import re
from dataclasses import dataclass

HANDOFF_PATTERNS = [
    "你来说", "你说一下", "你说说", "帮我说", "帮我回", "帮忙回",
    "AI 说", "AI说", "智能体说", "你来回答", "你回答一下", "你查一下", "帮忙查",
]
QUESTION_TAIL_RE = re.compile(r"(吗|呢|嘛|么|？|\?)\s*$")
QUESTION_HEAD_RE = re.compile(
    r"^(为什么|怎么|怎样|如何|什么|哪个|哪些|谁|几|多少|能不能|可否|是否|有没有|请问)")
# Second-person asks addressed at the agent ("你觉得呢", "你怎么看") — these
# become speakable only after a beat of silence (nobody else picks up).
DIRECT_ASK_RE = re.compile(
    r"(你觉得|你怎么看|你认为|你说呢|你呢|你的看法|你的建议|你建议)")
AGENT_WORD_RE = re.compile(r"(AI|助手|智能体|机器人)", re.IGNORECASE)


@dataclass
class TurnDecision:
    action: str  # "speak" | "listen"
    reason: str
    matched: str = ""


class TurnArbiter:
    def __init__(self, wake_names: list[str] | None = None):
        self.wake_names = [w.strip() for w in (wake_names or ["星火"]) if w.strip()]
        self._forced = False

    def force_next(self) -> None:
        """Manual trigger from the console ("让星火说")."""
        self._forced = True

    def _wake_hit(self, text: str) -> str:
        lowered = text.lower()
        for name in self.wake_names:
            if name and name.lower() in lowered:
                return name
        return ""

    def is_question(self, text: str) -> bool:
        return bool(QUESTION_TAIL_RE.search(text) or QUESTION_HEAD_RE.search(text))

    def is_direct_question(self, text: str) -> bool:
        """Question aimed at the agent without using the wake name."""
        stripped = text.strip()
        if not stripped:
            return False
        if DIRECT_ASK_RE.search(stripped):
            return True
        return bool(AGENT_WORD_RE.search(stripped) and self.is_question(stripped))

    def peek(self, text: str) -> str:
        """Non-destructive wake/handoff match (partial ASR early detection).

        Unlike decide(), this never consumes the manual force_next flag.
        """
        wake = self._wake_hit(text)
        if wake:
            return wake
        for pattern in HANDOFF_PATTERNS:
            if pattern in text:
                return pattern
        return ""

    def decide(self, speaker: str, text: str) -> TurnDecision:
        text = text.strip()
        if self._forced:
            self._forced = False
            return TurnDecision("speak", "manual", "控制台触发")
        if not text:
            return TurnDecision("listen", "empty")
        wake = self._wake_hit(text)
        if wake:
            return TurnDecision("speak", "wake", wake)
        for pattern in HANDOFF_PATTERNS:
            if pattern in text:
                return TurnDecision("speak", "handoff", pattern)
        return TurnDecision("listen", "not-addressed")

    def describe_triggers(self) -> list[str]:
        names = "、".join(self.wake_names) if self.wake_names else "（未设置）"
        return [
            f"点名唤醒：说出「{names}」",
            "明确移交：如「你来说一下」「帮忙查一下」",
            "被直接提问且无人接话：如「你觉得呢？」（2 秒后主动接话）",
            "控制台手动触发：让星火说",
        ]

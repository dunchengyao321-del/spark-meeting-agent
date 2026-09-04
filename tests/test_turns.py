"""Turn arbiter: wake/handoff/manual triggers, direct questions, safe peek."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.meeting.turns import TurnArbiter  # noqa: E402

FAILS = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}{(' — ' + str(detail)[:160]) if detail and not condition else ''}")
    if not condition:
        FAILS.append(name)


arbiter = TurnArbiter(["星火", "小火"])

# ---- decide: the classic triggers stay intact
d = arbiter.decide("我", "星火，这个方案的风险是什么？")
check("A1 唤醒名触发", d.action == "speak" and d.reason == "wake" and d.matched == "星火")
d = arbiter.decide("会议", "小火你来说一下")
check("A2 第二个唤醒名", d.action == "speak" and d.matched == "小火")
d = arbiter.decide("会议", "这个你来说一下吧")
check("A3 移交话术触发", d.action == "speak" and d.reason == "handoff")
d = arbiter.decide("会议", "今天先把项目进度过一遍")
check("A4 未点名只听", d.action == "listen" and d.reason == "not-addressed")
d = arbiter.decide("会议", "   ")
check("A5 空文本只听", d.action == "listen" and d.reason == "empty")

# ---- manual force is consumed exactly once
arbiter.force_next()
d = arbiter.decide("我", "随便一句")
check("B1 手动触发", d.action == "speak" and d.reason == "manual")
d = arbiter.decide("我", "下一句不再强制")
check("B2 手动只触发一次", d.action == "listen")

# ---- peek: same matching power as decide, but never consumes the force flag
arbiter.force_next()
check("C1 peek 命中唤醒", arbiter.peek("星火，在吗") == "星火")
check("C2 peek 命中移交", arbiter.peek("帮忙查一下进度") == "帮忙查")
check("C3 peek 不消耗手动标记", arbiter.decide("我", "普通一句").reason == "manual")
check("C4 peek 未命中为空", arbiter.peek("今天天气不错") == "")

# ---- direct questions (for the unanswered-question pickup)
check("D1 你觉得…怎么样", arbiter.is_direct_question("你觉得这个方案怎么样？"))
check("D2 你觉得呢", arbiter.is_direct_question("你觉得呢"))
check("D3 你怎么看", arbiter.is_direct_question("这件事你怎么看"))
check("D4 点名 AI 的疑问句", arbiter.is_direct_question("AI 知道下一步怎么安排吗"))
check("D5 含你觉得即算直接提问", arbiter.is_direct_question("你觉得方案很好"))
check("D6 普通陈述", not arbiter.is_direct_question("今天先把项目进度过一遍"))
check("D7 空文本", not arbiter.is_direct_question(""))

# ---- triggers description mentions the new pickup path
desc = "；".join(arbiter.describe_triggers())
check("E1 触发说明含无人接话", "无人接话" in desc, desc)

print("turns:", "ALL PASS" if not FAILS else f"{len(FAILS)} FAILED")
sys.exit(1 if FAILS else 0)

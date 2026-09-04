"""Clarification policy: confirm when unclear, never stall a clear question.

Regression guard: weak near-equal KB hits (the ubiquitous bigram overlap in a
large KB) must NOT trigger "A or B" disambiguation for clear questions.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.meeting.clarify import clarification_for  # noqa: E402

FAILS = []


def hit(score, source="knowledge/交付知识库/实施手册/团购实施手册.md",
        heading="", title=None):
    return {"score": score, "source": source, "heading": heading,
            "title": title if title is not None else source}


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}{(' — ' + str(detail)[:160]) if detail and not condition else ''}")
    if not condition:
        FAILS.append(name)


# 1. too short -> ask to repeat
q = clarification_for("嗯", None)
check("C1 太短→请重复", q is not None and "没听全" in q, q)

# 2. empty -> nothing
check("C2 空文本→不澄清", clarification_for("   ", None) is None)

# 3. weak hits only -> never disambiguate (the 差旅报销 regression)
weak = [hit(0.349), hit(0.322, source="knowledge/交付知识库/项目档案/胖东来OS项目/周报.md")]
check("C3 弱命中不澄清", clarification_for("差旅报销多久到账", weak) is None)

# 4. strong tie, distinct names -> disambiguate
strong = [hit(1.9, source="knowledge/交付知识库/项目档案/株百项目/株百验收标准.md"),
          hit(1.8, source="knowledge/交付知识库/项目档案/胖东来项目/胖东来验收流程.md")]
q = clarification_for("验收标准", strong)
check("C4 强平局→澄清", q is not None and "株百验收标准" in q and "胖东来验收流程" in q
      and "还是" in q, q)
check("C4b 名称去路径去扩展名", q and ".md" not in q and "knowledge" not in q, q)

# 5. nested names (generic vs specific) -> prefer specific, no question
nested = [hit(1.9, source="knowledge/交付知识库/索引.md", heading="实施手册"),
          hit(1.8, source="knowledge/交付知识库/实施手册/加盟实施手册--待完善.md")]
check("C5 包含关系→不澄清", clarification_for("加盟实施手册", nested) is None)

# 6. same display name -> no question
same = [hit(1.9, heading="结算周期"), hit(1.85, heading="结算周期",
        source="knowledge/交付知识库/结算实施手册.md")]
check("C6 同名→不澄清", clarification_for("结算周期", same) is None)

# 7. strong top but weak second -> answer directly, no question
skew = [hit(2.1), hit(0.6, source="knowledge/交付知识库/其他/杂项.md")]
check("C7 一家独大→不澄清", clarification_for("团购实施手册", skew) is None)

# 8. low ASR confidence -> ask to repeat instead of guessing
q = clarification_for("项目验收标准是什么", None, confidence=0.30)
check("C8 低置信度→请重复", q is not None and "没太听清" in q, q)

# 9. confidence absent -> behaviour unchanged (Apple ASR path)
check("C9 无置信度→不澄清", clarification_for("项目验收标准是什么", None, None) is None)

# 10. good confidence + clear text -> no clarification
check("C10 高置信度→不澄清", clarification_for("项目验收标准是什么", None, 0.92) is None)

# 11. low confidence beats disambiguation: repeat first, never guess A/B
q = clarification_for("验收标准", strong, confidence=0.2)
check("C11 低置信度优先于二选一", q is not None and "没太听清" in q, q)

print("clarify:", "ALL PASS" if not FAILS else f"{len(FAILS)} FAILED")
sys.exit(1 if FAILS else 0)

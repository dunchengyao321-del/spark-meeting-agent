"""Semantic clarification: ask one short question instead of guessing.

Triggers:
- utterance too short to be meaningful ("没听全，能再说一遍吗？");
- ASR confidence below threshold ("没太听清，能再说一遍吗？") — only when the
  ASR engine actually reports one (today: whisper-style verbose_json);
- the user names something specific (exact-substring-strength KB match) and
  two distinct documents claim it equally strongly ("A 还是 B？").

Disambiguation must never block a clear question: with a bigram scorer, weak
near-ties (~0.3) happen for almost any off-topic query, so an absolute
strength floor is required before asking "A or B".
"""

DISAMBIGUATE_MIN_SCORE = 1.0  # exact-substring-level match strength
ASR_CONF_MIN = 0.45  # below this the transcript is too unreliable to act on


def _display_name(hit: dict) -> str:
    heading = str(hit.get("heading") or "").strip()
    if heading:
        return heading
    source = str(hit.get("source") or "")
    title = str(hit.get("title") or "").strip()
    if title and title != source:
        return title
    name = source.rsplit("/", 1)[-1]
    for suffix in (".markdown", ".md", ".txt"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
    return name


def clarification_for(text: str, kb_hits: list[dict] | None = None,
                      confidence: float | None = None) -> str | None:
    stripped = text.strip()
    if not stripped:
        return None
    if len(stripped) < 3:
        return "抱歉，刚才这句我没听全，能再说一遍吗？"
    if confidence is not None and confidence < ASR_CONF_MIN:
        return "刚才这句我没太听清，能再说一遍吗？"
    if kb_hits and len(kb_hits) >= 2:
        top, second = kb_hits[0], kb_hits[1]
        top_score = top.get("score", 0)
        second_score = second.get("score", 0)
        if top_score >= DISAMBIGUATE_MIN_SCORE and second_score >= top_score * 0.85:
            top_name = _display_name(top)
            second_name = _display_name(second)
            if (top_name and second_name and top_name != second_name
                    and top_name not in second_name
                    and second_name not in top_name):
                return f"想确认一下，你说的是「{top_name}」还是「{second_name}」？"
    return None

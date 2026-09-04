"""Knowledge base store with hybrid keyword retrieval and a warm cache.

v1 keeps zero dependencies: chunks are indexed by char-bigrams plus ASCII
words, and the warm cache implements "speculative prefetch" — while someone
else is talking, the pipeline warms likely context so the agent's own reply
starts with retrieval cost ≈ 0. Swap in embeddings/sqlite-vec later without
changing the interface.
"""

import json
import math
import os
import re
import time
from pathlib import Path

ASCII_WORD_RE = re.compile(r"[a-zA-Z0-9_]{2,}")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
HTML_TAG_RE = re.compile(r"<[^>]+>")


def _tokens(text: str) -> set[str]:
    """检索分词：ASCII 词 + CJK bigram。

    不再收录 CJK 单字——单字（"营"）会命中"运营/营销/经营"等海量无关文档，
    噪音远大于收益，曾导致"联营/自营"问题检索到胖东来周报。
    """
    tokens = set(ASCII_WORD_RE.findall(text.lower()))
    cjk = CJK_RE.findall(text)
    for i in range(len(cjk) - 1):
        tokens.add(cjk[i] + cjk[i + 1])
    return tokens


def _strip_html(text: str) -> str:
    """清洗飞书导出 md 里的 HTML 标签（<cite user-id>、<td> 等）。

    标签里的 ASCII 属性词会产生大量垃圾 token 污染索引与匹配。
    """
    return HTML_TAG_RE.sub(" ", text)


def _chunk_markdown(text: str, source: str, max_chars: int = 700) -> list[dict]:
    chunks: list[dict] = []
    title = ""
    heading = ""
    buffer: list[str] = []

    def flush():
        body = "\n".join(buffer).strip()
        buffer.clear()
        if len(body) >= 20:
            chunks.append({"source": source, "title": title or source,
                           "heading": heading, "text": body})

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            flush()
            new_heading = stripped.lstrip("#").strip()
            if not title:
                title = new_heading
            heading = new_heading
            continue
        if len(stripped) > max_chars:
            flush()
            for i in range(0, len(stripped), max_chars):
                chunks.append({"source": source, "title": title or source,
                               "heading": heading, "text": stripped[i:i + max_chars]})
            continue
        buffer.append(line)
        if sum(len(b) for b in buffer) > max_chars:
            flush()
    flush()
    return chunks


class KnowledgeStore:
    def __init__(self, root: Path, kb_dir: str = "docs/kb"):
        self.root = root
        self.kb_dir = (root / kb_dir) if not Path(kb_dir).is_absolute() else Path(kb_dir)
        self.index_path = root / ".kb_index.json"
        self.chunks: list[dict] = []
        self._tokens: list[set[str]] = []
        self.warm_cache: dict[str, tuple[float, list[dict]]] = {}
        self.warm_ttl = 60.0

    # ------------------------------------------------------------------ ingest
    def ingest(self) -> dict:
        self.kb_dir.mkdir(parents=True, exist_ok=True)
        chunks: list[dict] = []
        files = 0
        # os.walk(followlinks=True)：知识库目录常用软链接挂外部内容
        # （rglob 不跟随软链接目录，会整片漏收）；seen 防软链成环。
        seen_dirs: set[Path] = set()
        paths: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(self.kb_dir, followlinks=True):
            real = Path(dirpath).resolve()
            if real in seen_dirs:
                dirnames[:] = []
                continue
            seen_dirs.add(real)
            for name in sorted(filenames):
                paths.append(Path(dirpath) / name)
        for path in sorted(paths):
            if path.suffix.lower() not in {".md", ".markdown", ".txt"} or not path.is_file():
                continue
            files += 1
            text = _strip_html(path.read_text(encoding="utf-8", errors="replace"))
            try:
                rel = str(path.relative_to(self.root))
            except ValueError:  # kb_dir 在项目外（如指向公司知识目录）
                try:
                    rel = f"{self.kb_dir.name}/{path.relative_to(self.kb_dir)}"
                except ValueError:
                    rel = str(path)
            chunks.extend(_chunk_markdown(text, rel))
        for i, chunk in enumerate(chunks):
            chunk["id"] = i
        self.chunks = chunks
        self._tokens = [_tokens(c["text"] + " " + c["heading"]) for c in chunks]
        # IDF 文档频率：高频词（"供应"满库都是）权重压小，低频领域词（"联营"）放大
        df: dict[str, int] = {}
        for toks in self._tokens:
            for t in toks:
                df[t] = df.get(t, 0) + 1
        self._df = df
        self.index_path.write_text(json.dumps({
            "chunks": chunks, "df": df, "built_at": time.time(),
            "kb_dir": str(self.kb_dir),
        }, ensure_ascii=False), encoding="utf-8")
        return {"files": files, "chunks": len(chunks)}

    def ensure_loaded(self) -> None:
        if self.chunks:
            return
        if self.index_path.exists():
            try:
                data = json.loads(self.index_path.read_text(encoding="utf-8"))
                # A persisted index built for a different kb_dir is stale
                # (e.g. kb_dir switched in settings, or tests use docs/kb).
                if data.get("kb_dir") == str(self.kb_dir):
                    self.chunks = data.get("chunks", [])
                    self._tokens = [_tokens(c["text"] + " " + c["heading"]) for c in self.chunks]
                    self._df = data.get("df") or {}
                    return
            except json.JSONDecodeError:
                pass
        self.ingest()

    # ------------------------------------------------------------------ search
    def search(self, query: str, k: int = 4) -> list[dict]:
        self.ensure_loaded()
        if not self.chunks or not query.strip():
            return []
        query_tokens = _tokens(query)
        if not query_tokens:
            return []
        df = getattr(self, "_df", None) or {}
        n = max(1, len(self.chunks))

        def idf(t: str) -> float:
            # log(N/df + 1) + 1：低频领域词（联营）权重显著高于高频词（供应）
            return math.log(n / (1 + df.get(t, 0)) + 1.0) + 1.0

        scored: list[tuple[float, dict]] = []
        for chunk, chunk_tokens in zip(self.chunks, self._tokens):
            overlap = query_tokens & chunk_tokens
            if not overlap:
                continue
            score = sum(idf(t) for t in overlap)
            score /= (len(query_tokens) ** 0.5 * max(1, len(chunk_tokens)) ** 0.25)
            if query.strip() and query.strip() in chunk["text"]:
                score += 2.0
            scored.append((score, chunk))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [{**chunk, "score": round(score, 3)} for score, chunk in scored[:k]]

    def search_expanded(self, query: str, k: int = 6) -> list[dict]:
        """两轮伪相关反馈检索：先召回 top-3，提取其中的高 IDF 领域词扩展查询，
        再召回合并——解决关键事实分散在同文档不同块、与查询词面不重合的多跳问题。

        例：「联营/自营切换的影响」第一轮命中实施手册（含"经营方式/管库存"），
        扩展后第二轮才能召回「库存凭证不管理联营库存」的 FAQ 块。
        """
        first = self.search(query, 3)
        if not first:
            return []
        df = getattr(self, "_df", None) or {}
        n = max(1, len(self.chunks))

        def idf(t: str) -> float:
            return math.log(n / (1 + df.get(t, 0)) + 1.0) + 1.0

        query_tokens = _tokens(query)
        extra: set[str] = set()
        for h in first:
            toks = (_tokens(h["text"]) | _tokens(h.get("heading", ""))) - query_tokens
            # 只取高 IDF 领域词（低频词区分度强），避免被高频词带偏
            extra.update(sorted(toks, key=lambda t: -idf(t))[:10])
        expanded = query + " " + " ".join(sorted(extra))
        second = self.search(expanded, k * 2)
        # 合并去重：首轮在前保精度，第二轮补多跳事实
        merged: list[dict] = []
        seen: set[int] = set()
        for h in first + second:
            cid = int(h.get("id", -1))
            if cid in seen:
                continue
            seen.add(cid)
            merged.append(h)
            if len(merged) >= k:
                break
        return merged

    # ------------------------------------------------------------ warm prefetch
    def warm(self, text: str, k: int = 3) -> list[dict]:
        hits = self.search(text, k)
        if hits:
            key = text.strip()[:80]
            self.warm_cache[key] = (time.time(), hits)
            now = time.time()
            self.warm_cache = {k_: v for k_, v in self.warm_cache.items()
                               if now - v[0] < self.warm_ttl * 4}
        return hits

    def take_warm(self, text: str) -> list[dict] | None:
        """Consume a warm cache entry if one is fresh enough for this text."""
        now = time.time()
        best_key = None
        best_hits = None
        needle = text.strip()[:80]
        for key, (ts, hits) in self.warm_cache.items():
            if now - ts > self.warm_ttl:
                continue
            if key and (key in needle or needle in key or
                        len(_tokens(key) & _tokens(needle)) >= 2):
                if best_key is None or ts > self.warm_cache[best_key][0]:
                    best_key, best_hits = key, hits
        if best_key is not None:
            del self.warm_cache[best_key]
        return best_hits

    def stats(self) -> dict:
        self.ensure_loaded()
        return {"dir": str(self.kb_dir), "chunks": len(self.chunks),
                "warm_entries": len(self.warm_cache)}

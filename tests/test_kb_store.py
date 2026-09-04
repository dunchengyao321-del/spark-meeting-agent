"""Tests for the knowledge base store: chunking, retrieval, warm prefetch."""

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.rag.store import KnowledgeStore, _chunk_markdown, _tokens  # noqa: E402

PASS = []


def ok(name):
    PASS.append(name)
    print(f"[PASS] {name}")


def test_tokens():
    tokens = _tokens("星火智能体的 MCP 接入 v2")
    assert "星火" in tokens and "火智" in tokens  # CJK bigrams
    assert "mcp" in tokens and "v2" in tokens     # ASCII words, lowercased
    ok("K1 中文二元+ASCII 分词")


def test_chunk_markdown():
    text = "# 产品手册\n简短引言。\n## 报销流程\n" + "报销需要发票。" * 40
    chunks = _chunk_markdown(text, "docs/kb/manual.md")
    assert chunks[0]["title"] == "产品手册"
    headings = {c["heading"] for c in chunks}
    assert "报销流程" in headings
    assert all(len(c["text"]) <= 700 for c in chunks)
    ok("K2 Markdown 切块带标题/长度上限")


def make_store(tmp: Path) -> KnowledgeStore:
    kb_dir = tmp / "kb"
    kb_dir.mkdir()
    (kb_dir / "hr.md").write_text(
        "# 员工手册\n## 报销流程\n差旅报销需在系统提交发票，五个工作日内到账。\n",
        encoding="utf-8")
    (kb_dir / "product.md").write_text(
        "# 产品 FAQ\n## 星火智能体\n星火支持会议语音转写、知识库问答与工具调用。\n",
        encoding="utf-8")
    store = KnowledgeStore(tmp, kb_dir="kb")
    store.ingest()
    return store


def test_search_relevance():
    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(Path(tmp))
        hits = store.search("差旅报销多久到账？", k=2)
        assert hits and "报销" in hits[0]["text"]
        assert hits[0]["heading"] == "报销流程"
        hits2 = store.search("星火智能体能做什么", k=2)
        assert hits2 and hits2[0]["heading"] == "星火智能体"
        assert store.search("", k=2) == []
    ok("K3 中文检索命中正确章节")


def test_warm_prefetch():
    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(Path(tmp))
        warmed = store.warm("差旅报销多久到账？")
        assert warmed
        assert store.stats()["warm_entries"] == 1
        taken = store.take_warm("差旅报销多久能到账？")  # 相似问法
        assert taken is not None and taken[0]["heading"] == "报销流程"
        assert store.take_warm("差旅报销多久到账？") is None  # 已消费
    ok("K4 预热缓存：命中相似问法且消费一次")


def test_index_persistence():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_store(root)
        store2 = KnowledgeStore(root, kb_dir="kb")
        store2.ensure_loaded()  # 应从 .kb_index.json 恢复
        assert store2.chunks and not store2.search("报销", k=1) is None
    ok("K5 索引落盘并可恢复")


if __name__ == "__main__":
    test_tokens()
    test_chunk_markdown()
    test_search_relevance()
    test_warm_prefetch()
    test_index_persistence()
    print(f"kb store: ALL PASS ({len(PASS)})")

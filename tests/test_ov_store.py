"""Tests for the OpenViking knowledge store adapter: mapping, fallback, breaker."""

import json
import sys
import tempfile
import time
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.rag.ov_store import OvKnowledgeStore  # noqa: E402

PASS = []


def ok(name):
    PASS.append(name)
    print(f"[PASS] {name}")


def _make_store(kb_text: str = "") -> OvKnowledgeStore:
    tmp = Path(tempfile.mkdtemp())
    kb = tmp / "kb"
    kb.mkdir()
    (kb / "笔记.md").write_text(kb_text or "# 笔记\n\n联营切换需要管库存，切换后凭证不管理联营库存，财务需要单独对账。", encoding="utf-8")
    store = OvKnowledgeStore(tmp, {"kb_dir": "kb", "ov_enabled": True,
                                   "ov_url": "http://127.0.0.1:19999",
                                   "ov_timeout_ms": 300})
    return store


def _fake_response(payload: dict):
    """Build a fake urllib response context manager."""
    body = json.dumps({"status": "ok", "result": payload}).encode("utf-8")

    class FakeResp:
        def read(self):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    return FakeResp()


FIND_RESULT = {
    "resources": [
        {"context_type": "resource", "level": 0, "score": 0.9,
         "uri": "viking://resources/kb/.abstract.md", "abstract": "目录摘要"},
        {"context_type": "resource", "level": 2, "score": 0.8,
         "uri": "viking://resources/kb/笔记/笔记.md", "abstract": "摘要"},
    ],
    "memories": [],
}


def test_ov_hit_mapping():
    """OV find+read results are mapped to the local hit shape."""
    store = _make_store()

    def fake_urlopen(req, timeout=0):
        url = req.full_url
        if url.endswith("/health"):
            return _fake_response({"healthy": True})
        if "/api/v1/search/find" in url:
            return _fake_response(FIND_RESULT)
        if "/api/v1/content/read" in url:
            return _fake_response({"content": "联营切换的完整原文内容。"})
        raise AssertionError(f"unexpected url {url}")

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        hits = store.search("联营切换", 4)
    assert hits and hits[0]["backend"] == "openviking", hits
    assert hits[0]["source"] == "kb/笔记/笔记.md", hits[0]
    assert hits[0]["title"] == "笔记", hits[0]
    assert "完整原文" in hits[0]["text"], hits[0]
    # L0 目录摘要必须被过滤
    assert all(".abstract" not in h["source"] for h in hits), hits
    ok("ov_hit_mapping")


def test_fallback_when_ov_down():
    """OV connection failure degrades to the local keyword store."""
    store = _make_store()

    def boom(req, timeout=0):
        raise urllib.error.URLError("connection refused")

    with mock.patch("urllib.request.urlopen", side_effect=boom):
        hits = store.search("联营切换", 4)
    assert hits and "联营" in hits[0]["text"], hits
    assert hits[0].get("backend") != "openviking", hits[0]
    ok("fallback_when_ov_down")


def test_circuit_breaker_opens():
    """Three consecutive find failures open the circuit: OV is skipped entirely."""
    store = _make_store()
    calls = {"n": 0}

    def selective_boom(req, timeout=0):
        url = req.full_url
        if url.endswith("/health"):
            return _fake_response({"healthy": True})  # 服务"活着"但查询超时
        calls["n"] += 1
        raise urllib.error.URLError("timeout")

    with mock.patch("urllib.request.urlopen", side_effect=selective_boom):
        for _ in range(3):
            store.search("联营", 4)  # 3 次 find 失败 -> 熔断
        calls_after = calls["n"]
        assert calls_after == 3, calls
        store.search("联营", 4)  # 熔断期内不应再发任何 find 请求
    assert calls["n"] == calls_after, (calls, calls_after)
    ok("circuit_breaker_opens")


def test_ov_empty_result_falls_back_to_local():
    """OV returns zero usable L2 docs -> local keyword results still served."""
    store = _make_store()

    def fake_urlopen(req, timeout=0):
        url = req.full_url
        if url.endswith("/health"):
            return _fake_response({"healthy": True})
        if "/api/v1/search/find" in url:
            return _fake_response({"resources": [], "memories": []})
        raise AssertionError(f"unexpected url {url}")

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        hits = store.search("联营切换", 4)
    assert hits and "联营" in hits[0]["text"], hits
    ok("ov_empty_falls_back")


def test_disabled_goes_straight_to_local():
    """ov_enabled=false never touches the network."""
    store = _make_store()
    store.apply_config({"ov_enabled": False})

    def boom(req, timeout=0):
        raise AssertionError("network must not be called when disabled")

    with mock.patch("urllib.request.urlopen", side_effect=boom):
        hits = store.search("联营切换", 4)
    assert hits and "联营" in hits[0]["text"], hits
    ok("disabled_goes_local")


def test_warm_and_take_warm():
    """Warm prefetch round-trips through the wrapper cache."""
    store = _make_store()

    def fake_urlopen(req, timeout=0):
        url = req.full_url
        if url.endswith("/health"):
            return _fake_response({"healthy": True})
        if "/api/v1/search/find" in url:
            return _fake_response(FIND_RESULT)
        if "/api/v1/content/read" in url:
            return _fake_response({"content": "联营切换的完整原文内容。"})
        raise AssertionError(url)

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        store.warm("联营切换的影响")
        hits = store.take_warm("联营切换的影响")
    assert hits and hits[0]["backend"] == "openviking", hits
    ok("warm_take_warm")


def test_stats_reports_ov():
    store = _make_store()
    with mock.patch("urllib.request.urlopen",
                    side_effect=urllib.error.URLError("down")):
        stats = store.stats()
    assert stats["ov"]["enabled"] is True, stats
    assert stats["ov"]["available"] is False, stats
    assert stats["chunks"] >= 1, stats
    ok("stats_reports_ov")


def test_ingest_syncs_ov():
    """ingest rebuilds the local index AND re-imports the kb dir into OV."""
    store = _make_store()
    calls = []

    def fake_urlopen(req, timeout=0):
        url = req.full_url
        calls.append(url)
        if url.endswith("/health"):
            return _fake_response({"healthy": True})
        if "/api/v1/resources/temp_upload" in url:
            return _fake_response({"temp_file_id": "tf-1"})
        if "/api/v1/resources" in url:
            return _fake_response({"status": "success"})
        if "/api/v1/fs" in url:
            return _fake_response({})
        raise AssertionError(f"unexpected url {url}")

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        stats = store.ingest()
    assert stats["ov"] == "synced", stats
    assert stats["chunks"] >= 1, stats
    # 幂等：先删旧资源树，再上传 zip，最后触发导入
    assert any("/api/v1/fs" in u for u in calls), calls
    assert any("temp_upload" in u for u in calls), calls
    assert any(u.endswith("/api/v1/resources") for u in calls), calls
    ok("ingest_syncs_ov")


def test_ingest_ov_down_still_succeeds():
    """OV unavailable -> ingest still returns local stats with ov=unavailable."""
    store = _make_store()
    with mock.patch("urllib.request.urlopen",
                    side_effect=urllib.error.URLError("down")):
        stats = store.ingest()
    assert stats["ov"] == "unavailable", stats
    assert stats["chunks"] >= 1, stats
    ok("ingest_ov_down_still_succeeds")


if __name__ == "__main__":
    test_ov_hit_mapping()
    test_fallback_when_ov_down()
    test_circuit_breaker_opens()
    test_ov_empty_result_falls_back_to_local()
    test_disabled_goes_straight_to_local()
    test_warm_and_take_warm()
    test_stats_reports_ov()
    test_ingest_syncs_ov()
    test_ingest_ov_down_still_succeeds()
    print(f"\n{len(PASS)} tests passed")

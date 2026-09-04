"""In-process ASGI tests for the meeting console server (no socket binding).

Covers: static console, config read/write semantics (secrets never echoed,
blank keeps old key), KB status/search, and a full /ws/meeting lifecycle
(connect -> engine startup events -> session.stop -> clean close).
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import server.config_store as config_store  # noqa: E402

_TMP = tempfile.TemporaryDirectory(prefix="spark_asgi_")
TMP_CONFIG = Path(_TMP.name) / "config.json"
TMP_CONFIG.write_text(json.dumps({
    "volcano_api_key": "ark-test-existing",
    "volcano_llm_model": "doubao-test-model",
}, ensure_ascii=False), encoding="utf-8")
config_store.CONFIG_FILE = TMP_CONFIG

import server.app as server_app  # noqa: E402

# Keep test ingestion inside the temp dir; never touch the production index.
server_app.kb_store.index_path = Path(_TMP.name) / ".kb_index.json"

app = server_app.app
FAILS = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}{(' — ' + str(detail)[:160]) if detail and not condition else ''}")
    if not condition:
        FAILS.append(name)


async def http(method, path, body=None):
    if "?" in path:
        path, _, query = path.partition("?")
    else:
        query = ""
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path,
        "raw_path": path.encode(), "query_string": query.encode(),
        "root_path": "", "headers": [], "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 8765),
    }
    if body is not None:
        body_bytes = json.dumps(body).encode()
        scope["headers"] = [(b"content-type", b"application/json")]
    else:
        body_bytes = b""
    state = {"sent_body": False}

    async def receive():
        if not state["sent_body"]:
            state["sent_body"] = True
            return {"type": "http.request", "body": body_bytes, "more_body": False}
        await asyncio.sleep(10)
        return {"type": "http.disconnect"}

    response = {"status": None, "body": b""}

    async def send(message):
        if message["type"] == "http.response.start":
            response["status"] = message["status"]
        elif message["type"] == "http.response.body":
            response["body"] += message.get("body", b"")

    await app(scope, receive, send)
    return response


async def ws_session():
    scope = {
        "type": "websocket", "asgi": {"version": "3.0"}, "path": "/ws/meeting",
        "raw_path": b"/ws/meeting", "query_string": b"engine=pipeline",
        "headers": [], "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 8765),
    }
    inbox = asyncio.Queue()
    inbox.put_nowait({"type": "websocket.connect"})
    sent = []
    accepted = asyncio.Event()

    async def receive():
        return await inbox.get()

    async def send(message):
        sent.append(message)
        if message["type"] == "websocket.accept":
            accepted.set()

    task = asyncio.create_task(app(scope, receive, send))
    await asyncio.wait_for(accepted.wait(), 5)
    await asyncio.sleep(0.6)  # let the pipeline engine emit startup events
    inbox.put_nowait({"type": "websocket.receive",
                      "text": json.dumps({"type": "session.stop"})})
    await asyncio.sleep(0.8)
    inbox.put_nowait({"type": "websocket.disconnect", "code": 1000})
    try:
        await asyncio.wait_for(task, timeout=8)
    except asyncio.TimeoutError:
        task.cancel()
    return sent


def text_events(sent):
    events = []
    for message in sent:
        if message["type"] == "websocket.send" and "text" in message:
            events.append(json.loads(message["text"]))
    return events


async def main():
    # ------------------------------------------------------------ static
    res = await http("GET", "/")
    body = res["body"].decode()
    check("S1 首页 200", res["status"] == 200, res["status"])
    check("S2 首页含控制台标题", "星火 · 会议语音智能体" in body)
    res = await http("GET", "/app.js")
    check("S3 app.js 可访问", res["status"] == 200 and b"capture-worklet" in res["body"])
    res = await http("GET", "/capture-worklet.js")
    check("S4 capture-worklet 可访问", res["status"] == 200)
    res = await http("GET", "/no-such-file.xyz")
    check("S5 未知路径 404", res["status"] == 404, res["status"])

    # ------------------------------------------------------------ status/kb
    res = await http("GET", "/api/status")
    status = json.loads(res["body"])
    check("S6 status 结构", res["status"] == 200 and "kb" in status and "mcp" in status,
          res["body"][:200])
    check("S7 知识库已摄入", status["kb"]["chunks"] >= 4, status["kb"])
    res = await http("GET", "/api/kb/search?q=%E7%9F%A5%E8%AF%86%E5%BA%93&k=3")
    hits = json.loads(res["body"])["hits"]
    check("S8 知识库检索命中", len(hits) > 0 and "text" in hits[0], res["body"][:200])

    # ------------------------------------------------------------ config
    res = await http("GET", "/api/config")
    cfg = json.loads(res["body"])
    check("S9 配置不回显密钥", "ark-test-existing" not in json.dumps(cfg) and cfg["key_configured"])
    res = await http("POST", "/api/config", {"volcano_llm_model": "test-model-x"})
    check("S10 保存配置 ok", json.loads(res["body"]).get("ok") is True, res["body"][:200])
    saved = json.loads(TMP_CONFIG.read_text(encoding="utf-8"))
    check("S11 新值已写入", saved.get("volcano_llm_model") == "test-model-x", saved.get("volcano_llm_model"))
    check("S12 留空保留旧密钥", saved.get("volcano_api_key") == "ark-test-existing")
    await http("POST", "/api/config", {"volcano_llm_model": ""})
    saved = json.loads(TMP_CONFIG.read_text(encoding="utf-8"))
    check("S13 留空保留旧值", saved.get("volcano_llm_model") == "test-model-x", saved.get("volcano_llm_model"))

    # ------------------------------------------------------------ websocket
    sent = await ws_session()
    events = text_events(sent)
    types = [e.get("type") for e in events]
    check("W1 接受连接", any(m["type"] == "websocket.accept" for m in sent))
    check("W2 会话状态事件", any(e["type"] == "session.state" and e["status"] == "connected"
                                and e["engine"] == "pipeline" for e in events), types)
    check("W3 触发方式下发", any(e.get("triggers") for e in events))
    check("W4 初始聆听状态", any(e["type"] == "agent.state" and e["state"] == "listening"
                                for e in events), types)
    check("W5 干净关闭", any(m["type"] == "websocket.close" for m in sent))

    print("server asgi:", "ALL PASS" if not FAILS else f"{len(FAILS)} FAILED")
    if FAILS:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

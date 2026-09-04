"""Spark meeting console server (default: http://127.0.0.1:8765/).

Serves the reusable web console, the settings/config REST API, knowledge-base
and MCP endpoints, and the /ws/meeting WebSocket that carries live audio in
both directions. Local-only by default.
"""

import argparse
import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from integrations.feishu import call_bridge
from server.config_store import (CONFIG_FILE, FIELDS, SECRET_FIELDS,
                                 load_config, save_config)
from server.engines.base import SessionIO
from server.engines.pipeline import PipelineEngine
from server.llm import build_llm
from server.mcp.host import MCPManager
from server.rag.ov_store import OvKnowledgeStore

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"

kb_store = OvKnowledgeStore(ROOT, load_config())
mcp_manager = MCPManager(load_config())
active_session: dict = {"engine": None, "io": None}


@asynccontextmanager
async def lifespan(_app):
    try:
        await mcp_manager.start_all()
    except Exception:  # noqa: BLE001
        pass
    yield
    await mcp_manager.stop_all()


app = FastAPI(title="Spark Meeting Console", version="0.2.0", lifespan=lifespan)

# 浏览器插件从 vc.feishu.cn 页面跨域上报状态；服务仅监听本机，放开 CORS 无外部风险。
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


# ------------------------------------------------------------------ config
@app.get("/api/config")
async def get_config():
    config = load_config()
    # Secrets are write-only: never echo them back to the page.
    safe = {key: ("" if key in SECRET_FIELDS else config.get(key, "")) for key in FIELDS}
    llm_ready = build_llm(config).configured()
    safe["key_configured"] = llm_ready
    safe["llm_key_configured"] = llm_ready
    return safe


@app.post("/api/config")
async def post_config(payload: dict):
    config = load_config()
    kb_dir_before = str(config.get("kb_dir", "docs/kb"))
    llm_key = str(payload.get("llm_api_key", "")).strip()
    if llm_key:
        config["llm_api_key"] = llm_key
    volcano_key = str(payload.get("volcano_api_key", "")).strip()
    if volcano_key:
        config["volcano_api_key"] = volcano_key
    for key in FIELDS:
        if key not in payload:
            continue
        value = payload[key]
        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue  # 留空 = 保持不变（防止旧页面/误操作清空配置）
            config[key] = value
        elif isinstance(value, list):
            if value:
                config[key] = value
        else:
            config[key] = value
    save_config(config)
    kb_store.apply_config(config)
    kb_dir_after = str(config.get("kb_dir", "docs/kb"))
    if kb_dir_after != kb_dir_before:
        kb_store.kb_dir = (ROOT / kb_dir_after) if not Path(kb_dir_after).is_absolute() else Path(kb_dir_after)
        kb_store.chunks = []
        kb_store._tokens = []
    return {"ok": True, "key_configured": build_llm(config).configured()}


# ------------------------------------------------------------------ status
@app.get("/api/status")
async def status():
    config = load_config()
    return {
        "engine_default": config.get("meeting_engine", "pipeline"),
        "keys": {
            "llm": build_llm(config).configured(),
        },
        "kb": kb_store.stats(),
        "mcp": mcp_manager.status(),
        "session": {"active": active_session["engine"] is not None,
                    "engine": active_session["engine"]},
    }


# ---------------------------------------------------------------- kb / mcp
@app.get("/api/agent_config")
async def get_agent_config():
    """桥接配置（会议智能体 run_agent/bridge 用）：agent_config.json。"""
    src = ROOT / "02-研发实现" / "agent_config.json"
    if not src.exists():
        src = ROOT / "02-研发实现" / "config.example.json"
    return json.loads(src.read_text(encoding="utf-8"))


@app.put("/api/agent_config")
async def put_agent_config(payload: dict):
    """保存桥接配置（监控页"配置"模块写入；重启 start_agent.sh 后生效）。"""
    dst = ROOT / "02-研发实现" / "agent_config.json"
    dst.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"ok": True}


@app.post("/api/kb/ingest")
async def kb_ingest():
    return kb_store.ingest()


@app.get("/api/kb/search")
async def kb_search(q: str = "", k: int = 4):
    return {"hits": kb_store.search(q, k)}


@app.post("/api/asr/apple_auth")
async def apple_asr_auth():
    from server.asr.apple_asr import AppleASR
    try:
        return await AppleASR.request_authorization()
    except Exception as exc:  # noqa: BLE001
        return {"authorized": False, "error": str(exc)[:200]}


@app.post("/api/mcp/start")
async def mcp_start():
    await mcp_manager.start_all()
    return {"servers": mcp_manager.status()}


@app.post("/api/mcp/call")
async def mcp_call(payload: dict):
    try:
        output = await mcp_manager.call(str(payload.get("name", "")),
                                        payload.get("arguments", {}))
        return {"ok": True, "output": output}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


# ------------------------------------------------- feishu bridge (路线 A)
# 浏览器由桥接守护进程持有（integrations/feishu/bridge_host.py），
# 这里只做代理：服务重启不会杀掉会中的浏览器。
def _bridge_result(result: dict):
    return result if result.get("ok") else JSONResponse(result, status_code=400)


@app.post("/api/feishu/launch")
async def feishu_launch():
    return _bridge_result(await asyncio.to_thread(call_bridge, "launch", 90))


@app.post("/api/feishu/attach")
async def feishu_attach(payload: dict | None = None):
    payload = payload or {}
    cdp_url = str(payload.get("cdp_url", "")).strip()
    kwargs = {"cdp_url": cdp_url} if cdp_url else {}
    return _bridge_result(await asyncio.to_thread(call_bridge, "attach", 30, **kwargs))


@app.get("/api/feishu/status")
async def feishu_status():
    return await asyncio.to_thread(call_bridge, "status", 15)


@app.post("/api/feishu/page_state")
async def feishu_page_state(payload: dict):
    return await asyncio.to_thread(call_bridge, "page_state", 5, **payload)


@app.post("/api/feishu/bind")
async def feishu_bind(payload: dict):
    meeting_number = str(payload.get("meeting_number", "")).strip()
    result = await asyncio.to_thread(call_bridge, "bind", 30,
                                     meeting_number=meeting_number)
    return _bridge_result(result)


@app.post("/api/feishu/say")
async def feishu_say(payload: dict):
    result = await asyncio.to_thread(call_bridge, "say", 300,
                                     text=str(payload.get("text", "")))
    return _bridge_result(result)


@app.post("/api/feishu/unbind")
async def feishu_unbind():
    return _bridge_result(await asyncio.to_thread(call_bridge, "unbind", 10))


@app.get("/api/feishu/next_audio")
async def feishu_next_audio():
    """插件轮询取待注入会议的 TTS 音频（方案 A：浏览器内注入）。

    队列里有音频 → 200 返回 24kHz 单声道 PCM16 二进制；空 → 204。
    """
    from fastapi.responses import Response
    result = await asyncio.to_thread(call_bridge, "pop_audio", 10)
    if not result.get("ok"):
        return JSONResponse({"ok": False, "error": result.get("error", "")}, status_code=502)
    audio_b64 = result.get("audio")
    if not audio_b64:
        return Response(status_code=204)
    import base64
    return Response(content=base64.b64decode(audio_b64),
                    media_type="application/octet-stream")


@app.post("/api/feishu/stop")
async def feishu_stop():
    return _bridge_result(await asyncio.to_thread(call_bridge, "stop", 30))


# ---------------------------------------------------------------- meeting
class WsSessionIO(SessionIO):
    def __init__(self, ws: WebSocket):
        super().__init__()
        self.ws = ws
        self._lock = asyncio.Lock()

    async def send_event(self, event: dict) -> None:
        async with self._lock:
            await self.ws.send_text(json.dumps(event, ensure_ascii=False))

    async def send_audio(self, pcm24k: bytes) -> None:
        async with self._lock:
            await self.ws.send_bytes(pcm24k)


@app.websocket("/ws/meeting")
async def ws_meeting(ws: WebSocket, engine: str = ""):  # noqa: ARG001 - 单模式，参数保留兼容
    await ws.accept()
    kind = "pipeline"
    io = WsSessionIO(ws)
    engine_instance = PipelineEngine(kb_store, mcp_manager)
    active_session["engine"] = kind
    active_session["io"] = io
    engine_task = asyncio.create_task(engine_instance.run(io))

    async def receive_loop():
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                break
            if "bytes" in message and message["bytes"]:
                data = message["bytes"]
                channel = data[0] if data[0] in (0, 1) else 0
                pcm = data[1:] if data[0] in (0, 1) else data
                if io.frames.full():
                    try:
                        io.frames.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                io.frames.put_nowait((channel, pcm))
            elif "text" in message and message["text"]:
                try:
                    control = json.loads(message["text"])
                except json.JSONDecodeError:
                    continue
                ctype = control.get("type", "")
                if ctype == "session.stop":
                    break
                if ctype == "inject.say":
                    text = str(control.get("text", "")).strip()
                    if text:
                        asyncio.create_task(engine_instance.inject_text(text))
                elif ctype == "agent.force":
                    if engine_instance.arbiter:
                        engine_instance.arbiter.force_next()
                else:
                    io.controls.put_nowait(control)
        io.closed.set()

    try:
        await receive_loop()
    except WebSocketDisconnect:
        io.closed.set()
    finally:
        await engine_instance.stop()
        engine_task.cancel()
        try:
            await engine_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        active_session["engine"] = None
        active_session["io"] = None
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------------ static
# Registered last so the catch-all never shadows /api/* routes.
# The console is a local dev tool: always serve fresh assets so UI fixes
# land without a hard refresh (avoids "stale modal" style bugs).
_NO_STORE = {"Cache-Control": "no-store"}


@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html", headers=_NO_STORE)


@app.get("/{path:path}")
async def static(path: str):
    candidate = (WEB_DIR / path).resolve()
    if not str(candidate).startswith(str(WEB_DIR.resolve())) or not candidate.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(candidate, headers=_NO_STORE)


def main():
    parser = argparse.ArgumentParser(description="星火会议语音智能体控制台")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--uds", default="", help="Unix socket 监听（调试用，优先于 TCP）")
    args = parser.parse_args()

    import uvicorn

    kb_store.ensure_loaded()
    print(f"会议控制台：http://{args.host}:{args.port}/", flush=True)
    print(f"知识库：{kb_store.stats()}", flush=True)
    if args.uds:
        print(f"UDS 模式：{args.uds}", flush=True)
        uvicorn.run(app, uds=args.uds, log_level="warning")
    else:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()

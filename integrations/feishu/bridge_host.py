"""飞书桥接守护进程：浏览器生命周期与 8765 服务解耦。

浏览器归本进程所有，8765 服务重启/代码更新不会杀掉会中的浏览器。

用法：
  前台：./.venv/bin/python -m integrations.feishu.bridge_host
  后台：nohup ./.venv/bin/python -m integrations.feishu.bridge_host \
        >> /tmp/spark_feishu_bridge.log 2>&1 &

控制协议（Unix socket，每连接一条 JSON 请求/响应）：
  {"action": "launch" | "attach" | "status" | "bind" | "say" | "unbind" | "stop", ...}
"""

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from integrations.feishu.bridge import FeishuBridge  # noqa: E402

SOCKET_PATH = "/tmp/spark_feishu_bridge.sock"
PID_FILE = "/tmp/spark_feishu_bridge.pid"

bridge = FeishuBridge()


async def handle(msg: dict) -> dict:
    action = msg.get("action")
    try:
        if action == "launch":
            return await bridge.launch()
        if action == "attach":
            from integrations.feishu import bridge as bridge_module
            return await bridge.attach(msg.get("cdp_url") or bridge_module.CDP_URL)
        if action == "status":
            return await bridge.status()
        if action == "peek":
            return await bridge.peek()
        if action == "page_state":
            return bridge.set_page_state(msg)
        if action == "bind":
            meeting_number = str(msg.get("meeting_number", ""))
            r = await bridge.bind(meeting_number, manual=bool(msg.get("manual")))
            return {"ok": r.ok, "state": r.state, "reason": r.reason,
                    "candidates": r.candidates, "meeting_number": meeting_number}
        if action == "say":
            return await bridge.say(str(msg.get("text", "")))
        if action == "pop_audio":
            pcm = bridge.pop_audio()
            if pcm is None:
                return {"ok": True, "audio": None}
            import base64
            return {"ok": True, "audio": base64.b64encode(pcm).decode()}
        if action == "unbind":
            await bridge.unbind()
            return {"ok": True}
        if action == "stop":
            await bridge.stop()
            return {"ok": True}
        return {"ok": False, "error": f"unknown action: {action}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


async def handle_conn(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        data = await reader.read(65536)
        result = await handle(json.loads(data.decode())) if data else {"ok": False, "error": "empty"}
        writer.write(json.dumps(result, ensure_ascii=False).encode())
        await writer.drain()
    except Exception as exc:  # noqa: BLE001
        try:
            writer.write(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode())
            await writer.drain()
        except Exception:  # noqa: BLE001
            pass
    finally:
        writer.close()


async def serve():
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)
    server = await asyncio.start_unix_server(handle_conn, path=SOCKET_PATH)
    os.chmod(SOCKET_PATH, 0o777)
    Path(PID_FILE).write_text(str(os.getpid()))
    print(f"桥接守护进程已启动 pid={os.getpid()} socket={SOCKET_PATH}", flush=True)

    stop = asyncio.Event()

    def _sig_handler():
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _sig_handler)
        except NotImplementedError:  # noqa: BLE001
            pass

    async with server:
        await stop.wait()
    print("收到停止信号，关闭浏览器……", flush=True)
    await bridge.stop()
    Path(PID_FILE).unlink(missing_ok=True)
    Path(SOCKET_PATH).unlink(missing_ok=True)


def main():
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

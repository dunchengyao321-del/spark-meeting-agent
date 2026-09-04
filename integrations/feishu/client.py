"""桥接守护进程客户端：8765 服务与 bot_listener 经此调用浏览器能力。"""

import json
import os
import socket

SOCKET_PATH = "/tmp/spark_feishu_bridge.sock"
DAEMON_HINT = ("桥接守护进程未运行。启动方式："
               "nohup ./.venv/bin/python -m integrations.feishu.bridge_host "
               ">> /tmp/spark_feishu_bridge.log 2>&1 &")


def daemon_alive() -> bool:
    return os.path.exists(SOCKET_PATH)


def call_bridge(action: str, timeout: float = 300, **payload) -> dict:
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(SOCKET_PATH)
        s.send(json.dumps({"action": action, **payload}, ensure_ascii=False).encode())
        chunks = []
        while True:
            part = s.recv(262144)
            if not part:
                break
            chunks.append(part)
        s.close()
        return json.loads(b"".join(chunks).decode())
    except (ConnectionRefusedError, FileNotFoundError):
        return {"ok": False, "error": DAEMON_HINT}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"桥接守护进程调用失败：{exc}"}

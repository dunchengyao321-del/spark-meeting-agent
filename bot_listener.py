#!/usr/bin/env python3
"""
飞书语音机器人 - 消息监听器

通过 lark-cli 事件订阅接收飞书 IM 消息，
当收到"在会议<会议号>中说：<文本>"格式的消息时，
自动触发 meeting_voice_bot.py 入会并播放 TTS 语音。

用法：
  python3 bot_listener.py
"""

import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import time
from datetime import datetime

# ---------- 配置 ----------
# 允许触发语音的飞书用户 open_id 列表（空列表 = 允许所有人）
ALLOWED_USERS = []
# 会议号正则匹配
MEETING_PATTERN = re.compile(
    r'(?:在会议|在会|meeting)\s*(\d{9})\s*(?:中说|中说|说|讲|播报|say|speak)\s*[：:]\s*(.+)',
    re.IGNORECASE
)
# 简单指令格式: "会议号: 文本"
SIMPLE_PATTERN = re.compile(r'^(\d{9})\s*[：:]\s*(.+)$')
# 常驻主机指令：直接说 / AI 生成发言
HOST_SAY_PATTERN = re.compile(r'^(?:说|say)\s*[：:]\s*(.+)', re.IGNORECASE | re.DOTALL)
HOST_AI_PATTERN = re.compile(r'^ai\s*[：:]\s*(.+)', re.IGNORECASE | re.DOTALL)
# 飞书桥接（8765 控制台）指令：绑定会议 / 换会 / 退场 / 状态
BRIDGE_BIND_PATTERN = re.compile(r'^(?:绑定会议|绑定|换会|bind)\s*[：:]?\s*(\d{9})\s*$', re.IGNORECASE)
BRIDGE_EXIT_PATTERN = re.compile(r'^(?:退场|退出会议|exit)$', re.IGNORECASE)
BRIDGE_STATUS_PATTERN = re.compile(r'^(?:状态|status)$', re.IGNORECASE)
SOCKET_PATH = "/tmp/meeting_voice_host.sock"
CONSOLE_URL = "http://127.0.0.1:8765"
# 脚本路径
VOICE_BOT_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "meeting_voice_bot.py")
# 日志文件
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_listener.log")


def log(msg: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"[{timestamp}] {msg}"
    print(log_line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(log_line + "\n")


def parse_message(text: str) -> tuple[str, str] | None:
    """解析消息，返回 (会议号, 要说的文本) 或 None"""
    # 匹配完整格式: "在会议123456789中说：大家好"
    m = MEETING_PATTERN.search(text)
    if m:
        return m.group(1), m.group(2).strip()

    # 匹配简单格式: "123456789: 大家好"
    m = SIMPLE_PATTERN.match(text.strip())
    if m:
        return m.group(1), m.group(2).strip()

    return None


def speak_in_meeting(meeting_no: str, text: str, display_name: str = "语音助手"):
    """调用 voice_bot 脚本入会说话"""
    log(f"🎤 准备发言: 会议 {meeting_no} → {text[:50]}...")

    cmd = [
        sys.executable, VOICE_BOT_SCRIPT,
        meeting_no, text,
        "--name", display_name,
    ]

    log(f"执行: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode == 0:
            log(f"✅ 发言成功: {result.stdout[-200:]}")
        else:
            log(f"❌ 发言失败 (code={result.returncode}): {result.stderr[-500:]}")
    except subprocess.TimeoutExpired:
        log("⏰ 发言超时")
    except Exception as e:
        log(f"❌ 发言异常: {e}")


def reply_text(message_id: str, text: str):
    """通过 lark-cli 回复消息（尽力而为）"""
    if not message_id:
        return
    try:
        subprocess.run(
            ["lark-cli", "im", "+messages-reply", "--message-id", message_id, "--text", text],
            capture_output=True, text=True, timeout=30
        )
    except Exception as e:
        log(f"回复失败: {e}")


def send_to_host(payload: dict, reply_id: str = ""):
    """发送指令到常驻会议主机"""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(120)
        s.connect(SOCKET_PATH)
        s.send(json.dumps(payload).encode())
        resp = s.recv(65536).decode()
        s.close()
        log(f"host 响应: {resp[:200]}")
        try:
            r = json.loads(resp)
            if r.get("ok") and payload.get("action") == "ai":
                reply_text(reply_id, f"🎤 已发言：{r.get('text', '')}")
            elif r.get("ok"):
                reply_text(reply_id, "🎤 已发言")
            else:
                reply_text(reply_id, f"❌ 发言失败：{r.get('error')}")
        except json.JSONDecodeError:
            pass
    except Exception as e:
        log(f"发送到 host 失败: {e}")
        reply_text(reply_id, f"❌ 无法连接常驻主机：{e}")


def call_console(path: str, payload: dict | None = None, timeout: int = 120) -> dict | None:
    """调用 8765 控制台 REST；控制台未运行时返回 None。"""
    import urllib.error
    import urllib.request
    url = CONSOLE_URL + path
    try:
        if payload is None:
            req = urllib.request.Request(url, method="GET")
        else:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode(), method="POST",
                headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": f"HTTP {e.code}"}
    except Exception as e:  # noqa: BLE001
        if isinstance(e, urllib.error.URLError) and "Connection refused" in str(e.reason):
            return None
        log(f"控制台调用失败 {path}: {e}")
        return {"ok": False, "error": str(e)}


def handle_bridge_command(text: str, message_id: str) -> bool:
    """绑定会议/换会/退场/状态 → 8765 桥接。命中返回 True。"""
    m = BRIDGE_BIND_PATTERN.match(text.strip())
    if m:
        meeting_no = m.group(1)
        r = call_console("/api/feishu/bind", {"meeting_number": meeting_no})
        if r is None:
            reply_text(message_id, "❌ 控制台未运行：先启动 ./.venv/bin/python -m server.app")
        elif r.get("ok"):
            state = "（页面未显示会议号，未校验）" if r.get("state") == "unverified" else ""
            reply_text(message_id, f"✅ 已绑定会议 {meeting_no}{state}")
        else:
            extra = f"，页面实际会议号：{'、'.join(r.get('candidates', []))}" if r.get("candidates") else ""
            reply_text(message_id, f"❌ 绑定失败：{r.get('reason') or r.get('error')}{extra}")
        return True
    if BRIDGE_EXIT_PATTERN.match(text.strip()):
        call_console("/api/feishu/unbind", {})
        reply_text(message_id, "✅ 已退场（解绑会议；托管浏览器仍在，可重新绑定）")
        return True
    if BRIDGE_STATUS_PATTERN.match(text.strip()):
        r = call_console("/api/feishu/status")
        if r is None:
            reply_text(message_id, "❌ 控制台未运行：先启动 ./.venv/bin/python -m server.app")
        else:
            bound = r.get("bound_meeting") or "未绑定"
            in_mtg = "在会中" if r.get("in_meeting") else "不在会中"
            bh = "✅" if r.get("blackhole") else "❌未检测到"
            reply_text(message_id,
                       f"浏览器：{'已启动' if r.get('browser') else '未启动'}；{in_mtg}；"
                       f"绑定：{bound}；BlackHole：{bh}")
        return True
    return False


def say_via_bridge_or_host(text: str, message_id: str):
    """「说：」发言路由：8765 桥接已绑定会议 → 走桥接；否则回落旧常驻主机。"""
    status = call_console("/api/feishu/status", timeout=5)
    if status and status.get("bound_meeting"):
        r = call_console("/api/feishu/say", {"text": text}, timeout=300)
        if r and r.get("ok"):
            reply_text(message_id, f"🎤 已在会议 {status['bound_meeting']} 发言")
        else:
            reply_text(message_id, f"❌ 桥接发言失败：{(r or {}).get('error') or (r or {}).get('reason')}")
        return
    if not os.path.exists(SOCKET_PATH):
        reply_text(message_id, "❌ 未绑定会议且常驻主机未运行：先发「绑定会议 <9位会议号>」，"
                               "或启动 meeting_voice_host.py start <会议号>")
        return
    send_to_host({"action": "speak", "text": text}, message_id)


async def consume_events():
    """通过 lark-cli 事件订阅接收消息"""
    log("🚀 启动飞书语音机器人监听器...")
    log(f"  脚本: {VOICE_BOT_SCRIPT}")
    log(f"  日志: {LOG_FILE}")

    # 启动事件消费
    process = await asyncio.create_subprocess_exec(
        "lark-cli", "event", "consume", "im.message.receive_v1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    log("📡 开始监听 im.message.receive_v1 事件...")

    # 读取 NDJSON 输出
    async def read_stdout():
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            line = line.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            try:
                event = json.loads(line)
                message_type = event.get("header", {}).get("event_type", "")
                if message_type != "im.message.receive_v1":
                    continue

                event_data = event.get("event", {})
                sender = event_data.get("sender", {}).get("sender_id", {}).get("open_id", "")
                chat_type = event_data.get("message", {}).get("chat_type", "")
                message_content_str = event_data.get("message", {}).get("content", "{}")

                # 解析消息内容
                try:
                    content = json.loads(message_content_str)
                    text = content.get("text", "")
                except json.JSONDecodeError:
                    text = message_content_str

                # 检查权限
                if ALLOWED_USERS and sender not in ALLOWED_USERS:
                    log(f"⛔ 用户 {sender} 无权限")
                    continue

                log(f"📩 收到消息 from {sender} ({chat_type}): {text[:100]}")
                message_id = event_data.get("message", {}).get("message_id", "")

                # 桥接指令：绑定会议/换会/退场/状态（8765 控制台）
                if handle_bridge_command(text, message_id):
                    continue

                # 常驻主机指令：说：xxx / ai: xxx
                m_ai = HOST_AI_PATTERN.match(text.strip())
                m_say = HOST_SAY_PATTERN.match(text.strip())
                if m_ai or m_say:
                    # 「说：」优先走 8765 桥接（已绑定会议时）；桥接不可用再回落旧常驻主机
                    if m_say:
                        say_text = m_say.group(1).strip()
                        log(f"🎯 直接发言指令: {say_text[:50]}")
                        asyncio.create_task(asyncio.to_thread(
                            say_via_bridge_or_host, say_text, message_id))
                        continue
                    if not os.path.exists(SOCKET_PATH):
                        reply_text(message_id, "❌ 常驻主机未运行，请先启动 meeting_voice_host.py start <会议号>")
                        continue
                    payload = {"action": "ai", "instruction": m_ai.group(1).strip()}
                    log(f"🤖 AI 发言指令: {payload['instruction'][:50]}")
                    asyncio.create_task(asyncio.to_thread(send_to_host, payload, message_id))
                    continue

                # 解析指令
                result = parse_message(text)
                if result:
                    meeting_no, speak_text = result
                    log(f"🎯 检测到语音指令: 会议 {meeting_no}, 内容: {speak_text}")

                    # 在后台异步执行，不阻塞事件监听
                    asyncio.create_task(
                        asyncio.to_thread(speak_in_meeting, meeting_no, speak_text)
                    )
                else:
                    log(f"  未匹配语音指令格式")

            except json.JSONDecodeError:
                pass
            except Exception as e:
                log(f"⚠️ 处理事件异常: {e}")

    async def read_stderr():
        while True:
            line = await process.stderr.readline()
            if not line:
                break
            log(f"[lark-cli] {line.decode('utf-8', errors='replace').strip()}")

    # 并发读取 stdout 和 stderr
    await asyncio.gather(read_stdout(), read_stderr())

    # 等待进程结束
    await process.wait()
    log("❌ 事件监听已停止")


async def main():
    # 检查依赖
    if not os.path.exists(VOICE_BOT_SCRIPT):
        log(f"❌ 未找到 voice_bot 脚本: {VOICE_BOT_SCRIPT}")
        sys.exit(1)

    try:
        await consume_events()
    except KeyboardInterrupt:
        log("👋 用户中断，退出")
    except Exception as e:
        log(f"❌ 异常退出: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

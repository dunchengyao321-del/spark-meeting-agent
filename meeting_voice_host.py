#!/usr/bin/env python3
"""
飞书会议语音常驻主机 - 启动一次，常驻会议，按需发言

用法：
  # 启动常驻（浏览器加入会议并保持连接）
  python3 meeting_voice_host.py start 123456789 --name "星火"
  
  # 让星火在会议中说话
  python3 meeting_voice_host.py speak "大家好，我是星火"
  
  # 停止常驻
  python3 meeting_voice_host.py stop
"""

import argparse
import asyncio
import json
import os
import re
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

# ---------- 配置 ----------
PID_FILE = "/tmp/meeting_voice_host.pid"
SOCKET_PATH = "/tmp/meeting_voice_host.sock"
AUDIO_DIR = Path(tempfile.gettempdir()) / "meeting_voice_host"
TTS_VOICE = "Tingting"
PROFILE_DIR = os.path.expanduser("~/.spark/feishu-profile")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def detect_blackhole_index() -> int:
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-f", "lavfi", "-i", "anullsrc",
         "-t", "0.01", "-f", "audiotoolbox", "-list_devices", "true", "-"],
        capture_output=True, text=True, timeout=10
    )
    for line in (r.stderr + r.stdout).split("\n"):
        if "BlackHole" in line:
            m = re.search(r'\[(\d+)\]', line)
            if m:
                return int(m.group(1))
    # FFmpeg's AudioToolbox index is not stable across macOS versions. Using
    # -1 follows the current system default output, which is calibrated to
    # BlackHole by the setup flow.
    return -1


def speak_tts(text: str):
    """生成 TTS 并播放到 BlackHole"""
    AUDIO_DIR.mkdir(exist_ok=True)
    audio_path = str(AUDIO_DIR / f"speech_{uuid.uuid4().hex[:8]}.wav")

    try:
        from tts_engine import synthesize_to_wav
        synthesize_to_wav(text, audio_path)

        log(f"播放: {text[:50]}...")
        previous_output = None
        switch_audio = shutil.which("SwitchAudioSource")
        if switch_audio:
            try:
                previous_output = subprocess.check_output(
                    [switch_audio, "-c", "-t", "output"], text=True
                ).strip()
                subprocess.run(
                    [switch_audio, "-t", "output", "-s", "BlackHole 2ch"],
                    check=True, capture_output=True, text=True,
                )
            except Exception as exc:
                log(f"切换 BlackHole 输出失败: {exc}")
                previous_output = None
        try:
            subprocess.run(["ffmpeg", "-y", "-i", audio_path,
                           "-f", "audiotoolbox", "-audio_device_index", "-1", "-"],
                          check=True, timeout=300)
        finally:
            if switch_audio and previous_output:
                subprocess.run(
                    [switch_audio, "-t", "output", "-s", previous_output],
                    capture_output=True,
                )
        log("播放完成")
        os.remove(audio_path)
        return True
    except Exception as e:
        log(f"播放失败: {e}")
        return False


async def run_host(meeting_number: str, display_name: str = "星火", password: str = None):
    """常驻主机：加入会议，保持连接，监听指令"""
    from playwright.async_api import async_playwright

    if not os.path.isdir(PROFILE_DIR):
        raise RuntimeError(
            f"未找到飞书登录态 {PROFILE_DIR}。不会自动打开登录页面；"
            "请你手动运行 python3 meeting_voice_bot.py login，扫码后再启动会议。"
        )
    
    log(f"🚀 启动常驻主机 - {'发起新会议' if meeting_number == 'new' else f'会议 {meeting_number}'}")
    log(f"   显示名称: {display_name}")
    log(f"   音频设备: BlackHole 2ch")
    
    # 确保 BlackHole 是默认输入
    subprocess.run(["SwitchAudioSource", "-t", "input", "-s", "BlackHole 2ch"],
                   capture_output=True)
    
    # 启动 Unix socket 服务器（用于接收 speak 指令）
    server_started = threading.Event()
    
    async def handle_speak(text: str):
        """处理发言请求"""
        log(f"📢 收到发言请求: {text[:50]}...")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, speak_tts, text)

    async def handle_ai(instruction: str, context: str = ""):
        """AI 生成发言并朗读"""
        log(f"🤖 AI 生成发言: {instruction[:50]}...")
        loop = asyncio.get_running_loop()
        try:
            from ai_speech import generate_reply
            text = await loop.run_in_executor(None, generate_reply, instruction, context)
            log(f"🤖 AI 生成结果: {text}")
            await loop.run_in_executor(None, speak_tts, text)
            return text
        except Exception as e:
            log(f"❌ AI 发言失败: {e}")
            raise
    
    def socket_server():
        """在独立线程中运行 socket 服务器"""
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)
        
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(SOCKET_PATH)
        server.listen(5)
        os.chmod(SOCKET_PATH, 0o777)
        server_started.set()
        
        log(f"   指令监听: {SOCKET_PATH}")
        log(f"   发言命令: python3 meeting_voice_host.py speak \"要说的文本\"")
        
        while True:
            try:
                conn, _ = server.accept()
                data = conn.recv(65536)
                if data:
                    try:
                        msg = json.loads(data.decode())
                        if msg.get("action") == "speak":
                            text = msg.get("text", "")
                            if text:
                                # 在线程中运行 async 任务
                                future = asyncio.run_coroutine_threadsafe(
                                    handle_speak(text), loop
                                )
                                future.result(timeout=300)
                                conn.send(b'{"ok": true}')
                            else:
                                conn.send(b'{"ok": false, "error": "empty text"}')
                        elif msg.get("action") == "ai":
                            instruction = msg.get("instruction", "")
                            context = msg.get("context", "")
                            if instruction:
                                future = asyncio.run_coroutine_threadsafe(
                                    handle_ai(instruction, context), loop
                                )
                                text = future.result(timeout=300)
                                conn.send(json.dumps({"ok": True, "text": text}).encode())
                            else:
                                conn.send(b'{"ok": false, "error": "empty instruction"}')
                        elif msg.get("action") == "ping":
                            conn.send(b'{"ok": true, "status": "alive"}')
                        else:
                            conn.send(b'{"ok": false, "error": "unknown action"}')
                    except Exception as e:
                        conn.send(f'{{"ok": false, "error": "{e}"}}'.encode())
                conn.close()
            except Exception as e:
                log(f"Socket error: {e}")
                time.sleep(1)
    
    # 启动 socket 服务器线程
    loop = asyncio.get_running_loop()
    sock_thread = threading.Thread(target=socket_server, daemon=True)
    sock_thread.start()
    server_started.wait()
    
    # 启动浏览器并加入会议
    os.makedirs(PROFILE_DIR, exist_ok=True)
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=False,
            args=["--use-fake-ui-for-media-stream", "--no-sandbox"],
            permissions=["microphone", "camera"],
            locale="zh-CN",
        )
        page = context.pages[0] if context.pages else await context.new_page()
        
        # 打开会议页面
        if meeting_number == "new":
            await page.goto("https://vc.feishu.cn/j", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)
            r = await page.evaluate("""() => {
                const btns = document.querySelectorAll("button");
                for (const b of btns) {
                    if (b.textContent.includes("发起新会议") || b.textContent.includes("New Meeting")) {
                        b.click();
                        return "clicked";
                    }
                }
                return "no_button";
            }""")
            log(f"点击发起新会议: {r}")
            await page.wait_for_timeout(3000)
        else:
            meeting_url = f"https://vc.feishu.cn/j/{meeting_number}"
            log(f"打开会议: {meeting_url}")
            await page.goto(meeting_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)

        body = await page.evaluate("() => document.body.innerText")
        if "扫码登录" in body or "手机号登录" in body:
            await context.close()
            raise RuntimeError(
                "当前登录态已失效。不会自动触发扫码；请你手动运行 "
                "python3 meeting_voice_bot.py login 完成扫码后重试。"
            )

        # 点击入会按钮（发起新会议后进入设备检查页，同样有"加入会议"按钮）
        r = await page.evaluate("""() => {
            const btns = document.querySelectorAll("button");
            for (const b of btns) {
                const t = b.textContent;
                if (t.includes("网页版入会") || t.includes("Join On This Browser") || t.includes("加入会议") || t.includes("进入会议") || t.includes("Join Meeting")) {
                    b.click();
                    return "clicked: " + t.trim();
                }
            }
            return "no_button";
        }""")
        log(f"点击入会: {r}")
        await page.wait_for_timeout(3000)
        
        # 输入会议号并加入
        for attempt in range(5):
            await page.wait_for_timeout(2000)
            body = await page.evaluate("() => document.body.innerText")
            
            # 检查是否已进入会议
            if "离开会议" in body or "挂断" in body or "静音" in body:
                log("✅ 已成功加入会议")
                break
            
            # 输入会议号
            await page.evaluate(f"""(num) => {{
                const inp = document.querySelector("input");
                if (inp) {{
                    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
                    setter.call(inp, num);
                    inp.dispatchEvent(new Event("input", {{bubbles: true}}));
                    inp.dispatchEvent(new Event("change", {{bubbles: true}}));
                }}
                const btns = document.querySelectorAll("button");
                for (const b of btns) {{
                    if ((b.textContent.includes("加入") || b.textContent.includes("Join")) && !b.disabled) {{
                        b.click();
                        return "clicked_join";
                    }}
                }}
                return "waiting";
            }}""", meeting_number)
            
            log(f"等待加入... ({attempt+1}/5)")
        
        # 激活麦克风
        await page.evaluate("""() => {
            try { navigator.mediaDevices.getUserMedia({ audio: true }); } catch(e) {}
        }""")
        log("✅ 常驻主机已就绪，等待发言指令...")
        
        # 保持连接 - 定期 ping 检查
        try:
            while True:
                await page.wait_for_timeout(30000)  # 每30秒检查一次
                # 检查页面是否还在
                try:
                    title = await page.title()
                    if not title:
                        log("⚠️ 页面已关闭，尝试重新连接...")
                        break
                except:
                    log("⚠️ 连接丢失")
                    break
                
                # 防止会议超时断开 - 检查状态
                try:
                    body = await page.evaluate("() => document.body.innerText")
                    if "离开会议" not in body and "挂断" not in body:
                        log("⚠️ 可能已断开会议")
                except:
                    pass
        except asyncio.CancelledError:
            log("收到停止信号")
        finally:
            # 离开会议
            try:
                await page.evaluate("""() => {
                    const btns = document.querySelectorAll("button");
                    for (const b of btns) {
                        if (b.textContent.includes("离开会议") || b.textContent.includes("挂断")) {
                            b.click(); return;
                        }
                    }
                }""")
            except:
                pass
            await context.close()
    
    log("常驻主机已停止")


async def main():
    parser = argparse.ArgumentParser(description="飞书会议语音常驻主机")
    sub = parser.add_subparsers(dest="command")
    
    # start 命令
    start_p = sub.add_parser("start", help="启动常驻主机")
    start_p.add_argument("meeting", help="9 位会议号，或 new 表示以你的账号发起新会议")
    start_p.add_argument("--name", default="星火", help="显示名称")
    start_p.add_argument("--password", help="会议密码")
    
    # speak 命令
    speak_p = sub.add_parser("speak", help="让星火在会议中说话")
    speak_p.add_argument("text", help="要说的文本")

    # ai 命令
    ai_p = sub.add_parser("ai", help="AI 生成发言并朗读")
    ai_p.add_argument("instruction", help="发言指令，如：总结一下我同意这个方案")
    ai_p.add_argument("--context", default="", help="会议上下文（可选）")
    
    # stop 命令
    sub.add_parser("stop", help="停止常驻主机")
    
    # status 命令
    status_p = sub.add_parser("status", help="查看常驻主机状态")

    # login-state 命令
    sub.add_parser("login-state", help="只检查本地飞书登录态，不打开浏览器")
    
    args = parser.parse_args()
    
    if args.command == "start":
        # 检查是否已在运行
        if os.path.exists(PID_FILE):
            with open(PID_FILE) as f:
                pid = f.read().strip()
            log(f"⚠️ 常驻主机已在运行 (PID: {pid})")
            log(f"   先运行 python3 meeting_voice_host.py stop")
            sys.exit(1)
        
        # 保存 PID
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))
        
        try:
            await run_host(args.meeting, args.name, args.password)
        finally:
            if os.path.exists(PID_FILE):
                os.unlink(PID_FILE)
            if os.path.exists(SOCKET_PATH):
                os.unlink(SOCKET_PATH)
    
    elif args.command == "speak":
        # 通过 socket 发送发言指令
        if not os.path.exists(SOCKET_PATH):
            print("❌ 常驻主机未运行")
            print(f"   先运行: python3 meeting_voice_host.py start <会议号>")
            sys.exit(1)
        
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(300)
            sock.connect(SOCKET_PATH)
            msg = json.dumps({"action": "speak", "text": args.text})
            sock.send(msg.encode())
            resp = sock.recv(4096)
            result = json.loads(resp.decode())
            if result.get("ok"):
                print(f"✅ 发言成功: {args.text[:50]}...")
            else:
                print(f"❌ 发言失败: {result.get('error')}")
            sock.close()
        except ConnectionRefusedError:
            print("❌ 常驻主机未运行")
    
    elif args.command == "ai":
        # 通过 socket 发送 AI 发言指令
        if not os.path.exists(SOCKET_PATH):
            print("❌ 常驻主机未运行")
            print(f"   先运行: python3 meeting_voice_host.py start <会议号>")
            sys.exit(1)

        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(120)
            sock.connect(SOCKET_PATH)
            msg = json.dumps({"action": "ai", "instruction": args.instruction, "context": args.context})
            sock.send(msg.encode())
            resp = sock.recv(65536)
            result = json.loads(resp.decode())
            if result.get("ok"):
                print(f"✅ AI 已发言: {result.get('text', '')}")
            else:
                print(f"❌ AI 发言失败: {result.get('error')}")
            sock.close()
        except ConnectionRefusedError:
            print("❌ 常驻主机未运行")

    elif args.command == "stop":
        # 停止常驻主机
        if os.path.exists(PID_FILE):
            with open(PID_FILE) as f:
                pid = f.read().strip()
            try:
                os.kill(int(pid), signal.SIGTERM)
                print(f"✅ 已发送停止信号 (PID: {pid})")
            except ProcessLookupError:
                print("⚠️ 进程不存在")
            os.unlink(PID_FILE)
        else:
            print("❌ 常驻主机未运行")
        
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)
    
    elif args.command == "status":
        if os.path.exists(SOCKET_PATH):
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(3)
                sock.connect(SOCKET_PATH)
                sock.send(json.dumps({"action": "ping"}).encode())
                resp = sock.recv(4096)
                print(f"✅ 常驻主机运行中")
                print(f"   {json.loads(resp.decode())}")
                sock.close()
            except Exception:
                print("❌ 常驻主机未运行")
        else:
            print("❌ 常驻主机未运行")

    elif args.command == "login-state":
        if os.path.isdir(PROFILE_DIR):
            print(f"✅ 已有本地登录态: {PROFILE_DIR}")
            print("   启动会议不会打开登录页面；登录失效时请手动运行 meeting_voice_bot.py login")
        else:
            print(f"❌ 没有登录态: {PROFILE_DIR}")
            print("   需要扫码时再手动运行: python3 meeting_voice_bot.py login")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

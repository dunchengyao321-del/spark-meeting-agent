#!/usr/bin/env python3
"""
飞书会议语音机器人 - 通过浏览器自动化入会并播放 TTS 语音

工作流程：
1. 接收要说的文本
2. 使用 macOS `say` 命令生成 TTS 音频
3. 通过 Playwright 打开飞书会议网页版，加入会议
4. 将音频通过 BlackHole 虚拟麦克风送入会议

前置条件：
- 安装 BlackHole: brew install blackhole-2ch (需重启)
- 安装 Playwright: pip install playwright && python3 -m playwright install chromium
- 在系统音频设置中将 BlackHole 设为输入设备
"""

import argparse
import asyncio
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

# ---------- 配置 ----------
# Playwright ffmpeg 路径
FFMPEG_PATH = shutil.which("ffmpeg") or os.path.expanduser(
    "~/Library/Caches/ms-playwright/ffmpeg-1011/ffmpeg-mac"
)
BLACKHOLE_NAME = "BlackHole 2ch"
TTS_VOICE = "Tingting"  # 中文语音
FFMPEG_FORMAT = "audiotoolbox"  # macOS 音频输出格式
PROFILE_DIR = os.path.expanduser("~/.spark/feishu-profile")
FEISHU_HOME = "https://vc.feishu.cn/"


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def check_dependencies() -> list[str]:
    """检查依赖，返回缺失项"""
    missing = []

    # BlackHole (通过 ffmpeg audiotoolbox 检测)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        try:
            r = subprocess.run(
                [ffmpeg, "-hide_banner", "-f", "lavfi", "-i", "anullsrc",
                 "-t", "0.01", "-f", "audiotoolbox", "-list_devices", "true", "-"],
                capture_output=True, text=True, timeout=10
            )
            if "BlackHole" not in (r.stderr + r.stdout):
                missing.append("BlackHole 2ch (brew install blackhole-2ch 并重启)")
        except Exception:
            missing.append("BlackHole 2ch (无法检测音频设备)")
    else:
        missing.append("ffmpeg (brew install ffmpeg)")

    # ffmpeg
    if not os.path.exists(FFMPEG_PATH):
        if shutil.which("ffmpeg") is None:
            missing.append(f"ffmpeg (未在 {FFMPEG_PATH} 找到)")
    # ffmpeg already exists

    # playwright
    try:
        import playwright
    except ImportError:
        missing.append("playwright (pip install playwright)")

    # say
    if shutil.which("say") is None:
        missing.append("say (macOS 内置命令)")

    return missing


def get_ffmpeg() -> str:
    if os.path.exists(FFMPEG_PATH):
        return FFMPEG_PATH
    if (s := shutil.which("ffmpeg")):
        return s
    raise FileNotFoundError("ffmpeg 未找到")


def generate_tts(text: str, output_path: str, voice: str = TTS_VOICE):
    """生成 TTS 音频（引擎由 config.json 的 tts_engine 决定）"""
    log(f"生成 TTS 音频...")
    try:
        from tts_engine import synthesize_to_wav
        synthesize_to_wav(text, output_path)
        log(f"TTS 已生成: {output_path}")
        return output_path
    except Exception as e:
        log(f"TTS 失败: {e}")
        raise


def play_audio_blackhole(audio_path: str, ffmpeg: str):
    """通过 BlackHole 播放音频到虚拟麦克风"""
    log(f"通过 BlackHole 播放...")

    # 查找 BlackHole 设备索引
    r = subprocess.run(
        [ffmpeg, "-hide_banner", "-f", "lavfi", "-i", "anullsrc",
         "-t", "0.01", "-f", "audiotoolbox", "-list_devices", "true", "-"],
        capture_output=True, text=True, timeout=10
    )
    output = (r.stderr + r.stdout)

    idx = None
    for line in output.split("\n"):
        if BLACKHOLE_NAME in line:
            # AudioToolbox format: [0] BlackHole 2ch, BlackHole2ch_UID
            m = re.search(r'\[(\d+)\]', line)
            if m:
                idx = int(m.group(1))
                break

    if idx is not None:
        log(f"BlackHole 设备索引: {idx}")
        subprocess.run(
            [ffmpeg, "-y", "-i", audio_path,
             "-f", "audiotoolbox", "-audio_device_index", str(idx), "-"],
            check=True, timeout=300
        )
        log("音频播放完成")
    else:
        log("BlackHole 未找到")
        raise RuntimeError("BlackHole 2ch 未检测到，请检查音频设置")


async def get_blackhole_devices():
    """获取系统音频设备列表"""
    r = await asyncio.create_subprocess_exec(
        "system_profiler", "SPAudioDataType",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await r.communicate()
    return stdout.decode()


async def join_meeting_and_speak(
    meeting_number: str,
    text: str,
    display_name: str = "语音助手",
    password: Optional[str] = None,
    headless: bool = False,
):
    """核心流程：TTS → 入会 → 播放"""
    from playwright.async_api import async_playwright

    # 1. TTS
    audio_dir = Path(tempfile.gettempdir()) / "meeting_voice_bot"
    audio_dir.mkdir(exist_ok=True)
    audio_path = str(audio_dir / f"tts_{uuid.uuid4().hex[:8]}.wav")
    generate_tts(text, audio_path)

    # 确保系统麦克风为 BlackHole（浏览器免确认抓取的就是虚拟麦克风）
    if shutil.which("SwitchAudioSource"):
        subprocess.run(["SwitchAudioSource", "-t", "input", "-s", "BlackHole 2ch"],
                       capture_output=True, timeout=10)

    # 2. 构建 URL
    meeting_url = f"https://vc.feishu.cn/j/{meeting_number}"
    log(f"会议链接: {meeting_url}")

    # 3. 浏览器入会（持久登录态：以自己的账号入会）
    os.makedirs(PROFILE_DIR, exist_ok=True)
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=headless,
            args=[
                "--use-fake-ui-for-media-stream",
                "--no-sandbox",
                "--disable-web-security",
            ],
            permissions=["microphone", "camera"],
            locale="zh-CN",
        )
        page = context.pages[0] if context.pages else await context.new_page()

        log("打开会议页面...")
        await page.goto(meeting_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)

        # 登录态检查
        try:
            body_text = await page.evaluate('() => document.body.innerText')
            if ("扫码登录" in body_text or "手机号登录" in body_text) and "入会" not in body_text:
                log("⚠️ 未检测到登录态：请先运行 python3 meeting_voice_bot.py login 用自己的账号登录")
        except Exception:
            pass

        # 通过 JS 点击"网页版入会"按钮
        clicked = False
        try:
            r = await page.evaluate(
                """() => {
                    const btns = document.querySelectorAll("button");
                    for (const b of btns) {
                        const t = b.textContent;
                        if (t.includes("网页版入会") || t.includes("Join On This Browser") || t.includes("加入会议") || t.includes("进入会议") || t.includes("Join Meeting")) {
                            b.click();
                            return "clicked: " + b.textContent.trim();
                        }
                    }
                    return "no_button";
                }"""
            )
            log(f"点击入会按钮: {r}")
            clicked = "clicked" in r
        except Exception as e:
            log(f"点击入会按钮失败: {str(e)[:30]}")


        if not clicked:
            log("⚠️ 未找到入会按钮，可能已自动进入")

        await page.wait_for_timeout(3000)

        # 等待页面加载并查找元素
        await page.wait_for_timeout(2000)

        log("分析页面元素...")
        join_success = False

        for attempt in range(3):
            await page.wait_for_timeout(2000)
            inputs = await page.locator("input").all()
            buttons = await page.locator("button:visible").all()
            log(f"第 {attempt+1} 次尝试: {len(inputs)} 输入框, {len(buttons)} 按钮")
            for inp in inputs:
                ph = await inp.get_attribute("placeholder") or ""
                if ph:
                    log(f"  输入框: placeholder='{ph}'")
            for btn in buttons:
                txt = (await btn.inner_text()).strip()
                if txt and len(txt) < 50:
                    en = await btn.is_enabled()
                    log(f"  按钮: '{txt}' enabled={en}")
            for inp in inputs:
                ph = (await inp.get_attribute("placeholder") or "").lower()
                if "id" in ph or "会议" in ph or "meeting" in ph:
                    await inp.fill(meeting_number)
                    log(f"已输入会议号: {meeting_number}")
                    await page.wait_for_timeout(500)
                    break
            for btn in buttons:
                txt = (await btn.inner_text()).strip()
                if "加入" in txt or "Join" in txt:
                    if await btn.is_enabled():
                        await btn.click()
                        log(f"✅ 点击加入: '{txt}'")
                        join_success = True
                        break
                    else:
                        log("加入按钮不可用")
            if join_success:
                break
            await page.wait_for_timeout(2000)

        # 通过 JS 直接触发
        if not join_success:
            log("尝试 JS 直接触发...")
            r1 = await page.evaluate(
                '(num) => {\
                    const inp = document.querySelector("input");\
                    if (inp) {\
                        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;\
                        setter.call(inp, num);\
                        inp.dispatchEvent(new Event("input", {bubbles: true}));\
                        inp.dispatchEvent(new Event("change", {bubbles: true}));\
                        return "filled: " + inp.placeholder;\
                    }\
                    return "no_input";\
                }',
                meeting_number
            )
            log(f"JS 输入: {r1}")
            await page.wait_for_timeout(1000)
            r2 = await page.evaluate(
                '() => {\
                    const btns = document.querySelectorAll("button");\
                    for (const b of btns) {\
                        if ((b.textContent.includes("加入") || b.textContent.includes("Join")) && !b.disabled) {\
                            b.click();\
                            return "clicked: " + b.textContent.trim();\
                        }\
                    }\
                    return "no_button";\
                }'
            )
            log(f"JS 点击: {r2}")
            join_success = "clicked" in r2

        # 密码
        if password:
            try:
                pwd = await page.wait_for_selector('input[type="password"]', timeout=3000)
                if pwd:
                    await pwd.fill(password)
                    log("已输入密码")
            except Exception:
                pass

        if not join_success:
            debug_dir = "/tmp/meeting_voice_bot"
            os.makedirs(debug_dir, exist_ok=True)
            await page.screenshot(path=os.path.join(debug_dir, "debug.png"))
            log("⚠️ 未能加入会议，已保存截图")

        # 等待加入会议
        log("等待加入会议...")
        for i in range(15):
            await page.wait_for_timeout(2000)
            current_url = page.url
            body = await page.evaluate('() => document.body.innerText')
            
            # 关闭可能出现的弹窗
            await page.evaluate('''() => {
                const dialogs = document.querySelectorAll(".ud__dialog__wrap, .ud__modal, [class*="dialog"], [class*="modal"]");
                for (const d of dialogs) {
                    const closeBtn = d.querySelector("button") || d.querySelector("[class*="close"]") || d.querySelector("[class*="cancel"]");
                    if (closeBtn) closeBtn.click();
                }
            }''')
            
            # 检查是否已成功加入
            if "正在加入" not in body and ("离开会议" in body or "挂断" in body or "Leave" in body or "mute" in body.lower() or "静音" in body):
                log("✅ 已成功加入会议")
                break
            
            if "/w/" in current_url or "/meeting/" in current_url:
                if i == 0:
                    log(f"会议页面已加载: {current_url}")
                # 主动激活麦克风
                await page.evaluate('''() => {
                    try {
                        navigator.mediaDevices.getUserMedia({ audio: true }).then(s => {
                            s.getTracks().forEach(t => {});
                        });
                    } catch(e) {}
                }''')
                log("已尝试激活麦克风")
            else:
                log(f"等待会议连接... ({i+1}/15)")

        # 4. 播放音频
        ffmpeg = get_ffmpeg()
        log("开始播放 TTS 音频...")
        play_audio_blackhole(audio_path, ffmpeg)

        log("音频播放完成，保持连接中...")
        await page.wait_for_timeout(5000)

        # 离开会议
        log("离开会议...")
        leave_selectors = [
            'button:has-text("离开会议")',
            'button:has-text("挂断")',
            'button:has-text("Leave")',
            '[aria-label*="挂断"]',
            '[aria-label*="离开"]',
        ]
        for sel in leave_selectors:
            try:
                btn = await page.wait_for_selector(sel, timeout=2000)
                if btn and await btn.is_visible():
                    await btn.click()
                    log("已离开会议")
                    break
            except Exception:
                pass

        await page.wait_for_timeout(2000)
        await context.close()

    # 清理
    try:
        os.remove(audio_path)
    except Exception:
        pass
    log("完成")


async def login_interactive():
    """打开浏览器用自己的账号登录飞书，登录态持久保存"""
    from playwright.async_api import async_playwright

    os.makedirs(PROFILE_DIR, exist_ok=True)
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=False,
            locale="zh-CN",
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(FEISHU_HOME, wait_until="domcontentloaded", timeout=30000)
        log("请在弹出的浏览器中用自己的账号登录飞书（扫码或密码）")
        input("登录完成后回到终端按回车继续...")
        await page.goto(FEISHU_HOME, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)
        body = await page.evaluate('() => document.body.innerText')
        if "扫码登录" in body or "手机号登录" in body:
            log("❌ 仍未登录成功，请重试")
        else:
            log(f"✅ 登录态已保存: {PROFILE_DIR}")
            log("之后入会将直接以你自己的账号身份进入会议")
        await context.close()


async def main():
    if len(sys.argv) > 1 and sys.argv[1] == "login":
        await login_interactive()
        return
    parser = argparse.ArgumentParser(
        description="飞书会议语音机器人"
    )
    parser.add_argument("meeting", nargs="?", help="9 位会议号")
    parser.add_argument("text", nargs="?", help="要说的文本")
    parser.add_argument("--name", default="语音助手", help="显示名称")
    parser.add_argument("--password", help="会议密码")
    parser.add_argument("--voice", default=TTS_VOICE, help="TTS 语音")
    parser.add_argument("--headless", action="store_true", help="无头模式")
    parser.add_argument("--check", action="store_true", help="仅检查依赖")

    args = parser.parse_args()

    if args.check:
        missing = check_dependencies()
        if missing:
            print("❌ 缺少以下依赖:")
            for item in missing:
                print(f"   - {item}")
            sys.exit(1)
        else:
            print("✅ 所有依赖已满足")
            sys.exit(0)

    missing = check_dependencies()
    if missing:
        print("❌ 缺少依赖，请先安装:")
        for item in missing:
            print(f"   - {item}")
        sys.exit(1)

    await join_meeting_and_speak(
        meeting_number=args.meeting,
        text=args.text,
        display_name=args.name,
        password=args.password,
        headless=args.headless,
    )


if __name__ == "__main__":
    asyncio.run(main())

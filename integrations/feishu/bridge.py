"""飞书会议桥接层（路线 A）：托管浏览器入会 + BlackHole 双向音频。

设计文档：docs/design/feishu-delegate-speak.md §4。
入会动作永远由用户完成（用户在托管浏览器里自行加入会议），桥接层只做：
启动托管浏览器 → 校验并绑定会议号 → 往会里发声（meeting_say）。
"""

import asyncio
import json
import re
import shutil
import subprocess
import tempfile
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

PROFILE_DIR = Path.home() / ".spark" / "feishu-profile"
MEETING_HOME_URL = "https://vc.feishu.cn/"
CDP_URL = "http://127.0.0.1:9222"
BLACKHOLE_NAME = "BlackHole 2ch"
TTS_RATE = 24000  # server/tts 输出 24kHz 单声道 PCM16

# 会中状态标记（飞书网页版会议控制条/顶栏文案，含新旧两版措辞）
IN_MEETING_MARKERS = ("离开会议", "挂断", "静音", "解除静音", "Unmute", "Leave",
                      "会议详情", "布局", "麦克风", "共享")
LOGIN_EXPIRED_MARKERS = ("扫码登录", "手机号登录")

MEETING_NO_RE = re.compile(r"(?<!\d)(\d{9})(?!\d)")


@dataclass
class BindingResult:
    ok: bool
    state: str  # bound | unverified | rejected
    reason: str = ""
    candidates: list = field(default_factory=list)


def validate_binding(page_text: str, page_title: str, requested: str) -> BindingResult:
    """绑定校验（纯函数，可离线测试）。

    规则：
    - 页面不在会中 → 拒绝（请用户先在托管浏览器里入会）；
    - 会中且页面出现请求的会议号 → 绑定成功；
    - 会中但页面只有别的会议号 → 拒绝并列出实际会议号；
    - 会中但页面读不到任何会议号 → 允许绑定但标记 unverified（尽力而为）。
    """
    requested = (requested or "").strip()
    if not re.fullmatch(r"\d{9}", requested):
        return BindingResult(False, "rejected", "会议号必须是 9 位数字")

    blob = f"{page_title or ''}\n{page_text or ''}"
    if any(marker in blob for marker in LOGIN_EXPIRED_MARKERS):
        return BindingResult(False, "rejected", "登录态已失效，请先运行 meeting_voice_bot.py login 重新扫码")
    if not any(marker in blob for marker in IN_MEETING_MARKERS):
        return BindingResult(False, "rejected", "托管浏览器当前不在会议中，请先在浏览器里加入会议")

    candidates = sorted(set(MEETING_NO_RE.findall(blob)))
    if requested in candidates:
        return BindingResult(True, "bound", "会议号校验通过", candidates)
    if candidates:
        return BindingResult(False, "rejected",
                             f"托管浏览器当前所在会议不是 {requested}", candidates)
    return BindingResult(True, "unverified",
                         "已在会中，但页面未显示会议号，按你的指令绑定（未校验）", [])


def pcm_to_wav(pcm: bytes, wav_path: str, rate: int = TTS_RATE) -> str:
    with wave.open(wav_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm)
    return wav_path


def find_blackhole_device() -> str | None:
    """返回 BlackHole 设备名（SwitchAudioSource 口径），未安装返回 None。"""
    exe = shutil.which("SwitchAudioSource")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "-a", "-t", "output"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:  # noqa: BLE001
        return None
    for line in out.splitlines():
        if "BlackHole" in line:
            return line.strip()
    return None


def play_wav_to_blackhole(wav_path: str) -> None:
    """把 WAV 播放到 BlackHole（沿用常驻主机验证过的默认输出切换方案）。"""
    switch = shutil.which("SwitchAudioSource")
    previous = None
    if switch:
        try:
            previous = subprocess.run([switch, "-c", "-t", "output"],
                                      capture_output=True, text=True, timeout=10).stdout.strip()
            subprocess.run([switch, "-t", "output", "-s", BLACKHOLE_NAME],
                           check=True, capture_output=True, timeout=10)
        except Exception:  # noqa: BLE001
            previous = None
    try:
        subprocess.run(["ffmpeg", "-y", "-i", wav_path,
                        "-f", "audiotoolbox", "-audio_device_index", "-1", "-"],
                       check=True, capture_output=True, timeout=300)
    finally:
        if switch and previous:
            subprocess.run([switch, "-t", "output", "-s", previous],
                           capture_output=True, timeout=10)


class FeishuBridge:
    """托管浏览器 + 发声通道。由 get_bridge() 单例管理。"""

    def __init__(self):
        self._playwright = None
        self._context = None
        self._page = None
        self.attached = False  # True = CDP 接管用户自己的 Chrome；stop() 只断开不关浏览器
        self.bound_meeting: str | None = None
        self.bound_state: str = ""
        self.remote_page: dict | None = None  # 浏览器插件上报的会议页面状态
        self._say_lock = asyncio.Lock()
        self._audio_queue: list = []  # 待注入会议的 PCM 音频队列（方案 A）

    # ------------------------------------------------------------- 插件页面状态
    def set_page_state(self, payload: dict) -> dict:
        self.remote_page = {
            "url": str(payload.get("url", "")),
            "title": str(payload.get("title", "")),
            "text": str(payload.get("text", ""))[:4000],
            "ts": time.time(),
        }
        return {"ok": True}

    def _remote_fresh(self) -> bool:
        return bool(self.remote_page) and (time.time() - self.remote_page["ts"]) < 20

    # ------------------------------------------------------------- 浏览器
    async def launch(self) -> dict:
        if self._context is not None:
            return {"ok": True, "already_running": True}
        if not PROFILE_DIR.is_dir():
            raise RuntimeError(
                f"未找到飞书登录态 {PROFILE_DIR}；请先运行 python3 meeting_voice_bot.py login 扫码")
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError("未安装 playwright：./.venv/bin/pip install playwright") from exc

        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            args=["--use-fake-ui-for-media-stream", "--no-sandbox"],
            permissions=["microphone", "camera"],
            locale="zh-CN",
        )
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        await self._page.goto(MEETING_HOME_URL, wait_until="domcontentloaded", timeout=30000)
        # 飞书会议从 BlackHole 取声：把默认输入切到 BlackHole
        if shutil.which("SwitchAudioSource"):
            subprocess.run(["SwitchAudioSource", "-t", "input", "-s", BLACKHOLE_NAME],
                           capture_output=True, timeout=10)
        return {"ok": True, "hint": "请在托管浏览器中加入目标会议，然后发送会议号绑定"}

    async def attach(self, cdp_url: str = CDP_URL) -> dict:
        """CDP 接管用户自己的 Chrome（需以 --remote-debugging-port 启动）。

        与 launch 互斥；stop() 时只断开控制，不关闭用户的浏览器。
        """
        if self._context is not None:
            return {"ok": True, "already_running": True, "mode": self.mode()}
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError("未安装 playwright：./.venv/bin/pip install playwright") from exc
        self._playwright = await async_playwright().start()
        try:
            browser = await self._playwright.chromium.connect_over_cdp(cdp_url)
        except Exception as exc:  # noqa: BLE001
            await self._playwright.stop()
            self._playwright = None
            raise RuntimeError(
                f"无法连接 Chrome 调试端口 {cdp_url}。请先完全退出 Chrome（Cmd+Q），"
                f"再运行 tools/start_chrome_cdp.sh 重开。原始错误：{exc}") from exc
        if not browser.contexts:
            await self._playwright.stop()
            self._playwright = None
            raise RuntimeError("已连上 Chrome，但没有可用窗口；请先打开一个标签页")
        self._context = browser.contexts[0]
        self._page = self._context.pages[0] if self._context.pages else None
        self.attached = True
        if shutil.which("SwitchAudioSource"):
            subprocess.run(["SwitchAudioSource", "-t", "input", "-s", BLACKHOLE_NAME],
                           capture_output=True, timeout=10)
        return {"ok": True, "mode": "attached",
                "hint": "已接管你的 Chrome；请在其中加入目标会议，然后发送会议号绑定"}

    def mode(self) -> str:
        if self._context is None:
            return "none"
        return "attached" if self.attached else "managed"

    async def _snapshot(self) -> tuple[str, str]:
        """读取当前最佳标签页的正文与标题。

        飞书入会流程会新开标签页，因此不能只盯启动时的页面：
        遍历上下文所有标签页，优先选含会中标记的，其次选最后一个可读的。
        """
        if self._context is None:
            raise RuntimeError("托管浏览器未启动：请先 POST /api/feishu/launch")
        pages = self._context.pages
        if not pages:
            raise RuntimeError("托管浏览器没有打开的标签页")
        fallback = None
        for page in pages:
            try:
                text = await page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                title = await page.title() or ""
            except Exception:  # noqa: BLE001
                continue
            fallback = (page, text, title)
            if any(marker in text or marker in title for marker in IN_MEETING_MARKERS):
                self._page = page
                return text, title
        if fallback is None:
            raise RuntimeError("托管浏览器所有标签页均不可读（页面可能已崩溃）")
        self._page, text, title = fallback
        return text, title

    # ------------------------------------------------------------- 绑定
    async def peek(self) -> dict:
        """调试用：列出所有标签页的 URL/标题/正文片段，用于排查识别失败。"""
        if self._context is None:
            return {"ok": False, "error": "浏览器未连接"}
        tabs = []
        for page in self._context.pages:
            try:
                text = await page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                title = await page.title() or ""
                url = page.url
            except Exception as exc:  # noqa: BLE001
                tabs.append({"url": "<不可读>", "error": str(exc)[:80]})
                continue
            tabs.append({"url": url, "title": title,
                         "text_head": " ".join(text.split())[:300]})
        return {"ok": True, "tabs": tabs}

    async def bind(self, meeting_number: str, manual: bool = False) -> BindingResult:
        digits = re.sub(r"\D", "", meeting_number)
        if not digits:
            return BindingResult(False, "rejected", "会议号为空", [])
        if manual:
            # 手动绑定：用户在自己浏览器入会、桥接无法读页面时，信任用户指定。
            self.bound_meeting = digits
            self.bound_state = "manual"
            return BindingResult(True, "manual", "手动绑定（未做页面校验）", [digits])
        if self._remote_fresh():
            result = validate_binding(self.remote_page["text"],
                                      self.remote_page["title"], digits)
        elif self._context is not None:
            text, title = await self._snapshot()
            result = validate_binding(text, title, digits)
        else:
            return BindingResult(False, "rejected",
                                 "未收到浏览器插件的页面状态：请在常用 Chrome 安装"
                                 " tools/feishu_bridge_extension 插件后重试，或改用手动绑定", [])
        if result.ok:
            self.bound_meeting = digits
            self.bound_state = result.state
        return result

    async def unbind(self) -> None:
        self.bound_meeting = None
        self.bound_state = ""

    # ------------------------------------------------------------- 发声
    async def say(self, text: str) -> dict:
        """meeting_say 语音通道（方案 A：浏览器内注入）。

        TTS 产出 24kHz 单声道 PCM16 后放入内存队列，由飞书会议页面里的
        浏览器插件轮询取出、经 Web Audio 混入会议麦克风轨道播出。
        不再走 BlackHole（Chrome 采集虚拟设备不稳定）。
        同一时刻只允许一句在合成。
        """
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "empty text"}
        if self.bound_meeting is None:
            return {"ok": False, "error": "尚未绑定会议：请先发送会议号绑定"}
        from server.config_store import load_config
        from server.tts import build_tts

        async with self._say_lock:
            tts = build_tts(load_config())
            pcm = await tts.synthesize(text)
            if not pcm:
                return {"ok": False, "error": f"TTS（{tts.name}）未产出音频"}
            self._audio_queue.append(pcm)
        return {"ok": True, "engine": tts.name, "meeting": self.bound_meeting,
                "queued_bytes": len(pcm)}

    # 插件经 GET /api/feishu/next_audio 拉走一段待注入音频（PCM 二进制）
    def pop_audio(self) -> bytes | None:
        if not self._audio_queue:
            return None
        return self._audio_queue.pop(0)

    # ------------------------------------------------------------- 生命周期
    async def stop(self) -> None:
        """退场：解绑并关闭/断开浏览器。

        attached 模式下只断开控制，绝不关闭用户自己的 Chrome。
        """
        self.bound_meeting = None
        self.bound_state = ""
        attached = self.attached
        if self._context is not None and not attached:
            try:
                await self._context.close()
            except Exception:  # noqa: BLE001
                pass
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:  # noqa: BLE001
                pass
        self._context = None
        self._page = None
        self._playwright = None
        self.attached = False

    async def status(self) -> dict:
        info = {"browser": self._context is not None,
                "mode": self.mode(),
                "extension": self._remote_fresh(),
                "bound_meeting": self.bound_meeting,
                "bound_state": self.bound_state,
                "blackhole": find_blackhole_device(),
                "in_meeting": False, "candidates": []}
        if self._remote_fresh():
            blob = f"{self.remote_page['title']}\n{self.remote_page['text']}"
            info["in_meeting"] = any(m in blob for m in IN_MEETING_MARKERS)
            info["candidates"] = sorted(set(MEETING_NO_RE.findall(blob)))
            return info
        if self._context is None:
            return info
        try:
            text, title = await self._snapshot()
        except Exception:  # noqa: BLE001
            info["browser"] = False
            return info
        blob = f"{title}\n{text}"
        info["in_meeting"] = any(m in blob for m in IN_MEETING_MARKERS)
        info["candidates"] = sorted(set(MEETING_NO_RE.findall(blob)))
        return info


_bridge: FeishuBridge | None = None


def get_bridge() -> FeishuBridge:
    global _bridge
    if _bridge is None:
        _bridge = FeishuBridge()
    return _bridge

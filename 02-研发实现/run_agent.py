"""
run_agent.py —— 飞书会议语音智能体 · Playwright 启动器

职责：
  1) 以 headed 模式启动 Chrome，带齐媒体与防节流相关 flags；
  2) context 授权 microphone/camera 权限，并 add_init_script 注册 shim.js；
  3) 打开 vc.feishu.cn（或指定会议链接），等待人工完成登录与入会；
  4) 轮询检测"入会成功"特征（页面出现会议控制栏文案）；
  5) 入会后周期性读取 window.__agentShim 状态并打印日志；
  6) 入会后自动打开本场会议妙记页（实时转写经 minutes_collector 同步给 B）。

用法：
  python3 run_agent.py --meeting-id 123456789
  python3 run_agent.py --url "https://vc.feishu.cn/j/123456789"
  python3 run_agent.py --config agent_config.json   # {"meeting_id": "...", 或 "url": "..."}
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Optional

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

log = logging.getLogger("run_agent")

BASE_DIR = Path(__file__).resolve().parent
SHIM_PATH = BASE_DIR / "shim.js"
FEATURES_DIR = BASE_DIR / "features"   # 特性脚本目录：shim 之后按序注入（聊天回帖等）

# 启动 flags：媒体自动授权 + 自动播放放开 + 防后台节流 + 沙箱兼容（缺一不可，见 README 排查表）
LAUNCH_ARGS = [
    "--no-sandbox",                            # TRAE 沙箱内 Chrome 自身沙箱无法初始化，必须关闭
    "--disable-breakpad",                      # 崩溃上报目录写权限受限，直接禁用
    "--disable-crash-reporter",
    "--use-fake-ui-for-media-stream",          # 自动通过浏览器的麦克风/摄像头授权弹窗
    "--autoplay-policy=no-user-gesture-required",  # 允许 AudioContext 无手势自动启动
    "--disable-background-timer-throttling",   # 后台标签页不节流定时器（20ms 音频时钟靠它）
    "--disable-renderer-backgrounding",        # 后台不降级渲染进程优先级
    "--disable-backgrounding-occluded-windows",# 窗口被遮挡时不视为后台
    "--disable-popup-blocking",               # 放行程序点击打开的新标签（自动开妙记页用）
    "--mute-audio",                           # 浏览器整机静音：智能体截获音频在页面内 WebRTC 层，不经扬声器，静音不影响听会
    # 飞书域名与本机回环绕过系统代理：全局代理会让会议页 WS/媒体连接慢 30 倍甚至卡死在「连接中…」
    "--proxy-bypass-list=*.feishu.cn;*.larksuite.com;*.feishucdn.com;*.larkoffice.com;<-loopback>",
    "--disable-features=WebRtcHideLocalIpsWithMdns,LocalNetworkAccessChecks,LocalNetworkAccessChecksWarn,PrivateNetworkAccessRespectPreflightResults",
    # ↑ 沙箱内 mDNS 解析失败会导致 ICE 缺候选；公网页面连本地 127.0.0.1 WebSocket 会被 PNA 拦截，一并关闭
]

# 入会成功的页面特征：会议控制栏常见按钮文案，命中 >=2 个视为已入会
JOIN_HINT_TEXTS = ["解除静音", "共享屏幕", "结束", "开启视频", "停止视频", "参会人", "离开会议", "静音", "共享"]

# 会议结束的页面特征：命中任意一个即视为会议已结束（自动退出，勿用裸「结束」——会误伤会中按钮）
END_HINT_TEXTS = ["会议已结束", "会议结束", "感谢参会", "会议已失效", "会议不存在", "该会议已被解散"]

# 会议状态落盘（监控台轮询展示「进行中/已结束」）
STATUS_FILE = BASE_DIR / "meeting_status.json"

# 页面内检测脚本：返回 {joined, hit, url}
DETECT_JOINED_JS = """
() => {
  const t = document.body ? document.body.innerText : '';
  const hints = %s;
  const hit = hints.filter(k => t.includes(k));
  return { joined: hit.length >= 2, hit, url: location.href };
}
""" % json.dumps(JOIN_HINT_TEXTS, ensure_ascii=False)

# 读取 shim 探针的脚本（未注入时返回 null）
READ_SHIM_JS = "() => (window.__agentShim ? window.__agentShim : null)"

# 会议结束检测脚本：返回 {ended, hit}
DETECT_ENDED_JS = """
() => {
  const t = document.body ? document.body.innerText : '';
  const hints = %s;
  const hit = hints.filter(k => t.includes(k));
  return { ended: hit.length >= 1, hit };
}
""" % json.dumps(END_HINT_TEXTS, ensure_ascii=False)

# 妙记列表页兜底扫描：登录态下打开 www.feishu.cn/minutes/home（自动跳组织域名），
# 找列表里「录音中」的妙记条目链接（会议内找不到入口时的可靠路径）。
MINUTES_HOME_SCAN_JS = r"""
() => {
  const skip = ['minutes/home', 'minutes/me', 'minutes/shared', 'minutes/trash'];
  for (const a of document.querySelectorAll('a[href*="/minutes/"]')) {
    if (!a.href || skip.some(s => a.href.includes(s))) continue;
    const text = ((a.closest('li,tr,div') || a).textContent || '');
    if (text.includes('录音中')) return { minutesUrl: a.href };
  }
  return null;
}
"""

# 妙记入口探测脚本（在会议页执行）：优先返回页面上的妙记链接交给 Python 新开标签；
# 找不到链接则依次尝试：确认「开启妙记」弹窗 → 点工具栏「妙记」→ 点开「更多」菜单（下轮再找）。
# 返回 {action, minutesUrl?}
MINUTES_STEP_JS = r"""
() => {
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const shortOk = t => { const s = (t || '').replace(/\s+/g, ''); return s.length > 0 && s.length <= 12; };
  // 0) 页面已有妙记链接（侧面板/系统消息里的「在妙记中打开」）→ 交给 Python 新开标签
  for (const a of document.querySelectorAll('a[href*="/minutes/"]')) {
    if (a.href && vis(a)) return { action: 'open', minutesUrl: a.href };
  }
  // 1) 妙记相关弹窗（开启确认等）→ 点确认类按钮
  const dlgs = [...document.querySelectorAll('[role="dialog"], [class*="modal"], [class*="Modal"], [class*="dialog"]')]
    .filter(el => vis(el) && (el.textContent || '').includes('妙记'));
  for (const dlg of dlgs) {
    const ok = [...dlg.querySelectorAll('button, [role="button"]')]
      .find(b => vis(b) && /开启|开始|确认|确定/.test(b.textContent || ''));
    if (ok) { ok.click(); return { action: 'confirm' }; }
  }
  // 2) 工具栏/菜单里可见的「妙记」短文本按钮 → 点击
  const cand = [...document.querySelectorAll('button, [role="button"], [class*="button"], [class*="Button"], li, span, div, a')]
    .filter(el => vis(el) && shortOk(el.textContent));
  const mj = cand.find(el => (el.textContent || '').includes('妙记'));
  if (mj) { mj.click(); return { action: 'click-miaoji' }; }
  // 3) 可能藏在「更多」菜单 → 先点开（下一轮再找妙记）
  const more = cand.find(el => /更多/.test(el.textContent || ''));
  if (more) { more.click(); return { action: 'click-more' }; }
  return { action: 'none' };
}
"""


def extract_meeting_id(url: str) -> str:
    digits = "".join(ch for ch in url if ch.isdigit())
    return digits[-9:] if len(digits) >= 9 else digits


def write_status(state: str, meeting_id: str) -> None:
    """会议状态落盘：active=进行中 / ended=已结束（监控台轮询展示）。"""
    try:
        STATUS_FILE.write_text(json.dumps({
            "state": state, "meeting_id": meeting_id,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError:
        pass


def detect_meeting_end(context, check_page_gone: bool) -> str:
    """检测会议结束，返回原因（空串=未结束）。

    触发条件：1) 会议页面（vc.feishu.cn）被关闭/跳转走；2) 页面出现结束文案。
    check_page_gone=False 时只查文案（入会前可能有登录页跳转，不能按页面消失判断）。
    """
    pages = context.pages
    meeting_pages = [pg for pg in pages if "vc.feishu.cn" in pg.url]
    if check_page_gone and not meeting_pages:
        return "会议页面已关闭"
    for pg in meeting_pages:
        try:
            r = pg.evaluate(DETECT_ENDED_JS)
        except PlaywrightError:
            continue
        if r and r.get("ended"):
            return "命中结束文案: %s" % "、".join(r.get("hit") or [])
    return ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="飞书会议语音智能体 · Playwright 启动器")
    parser.add_argument("--meeting-id", help="9 位飞书会议 ID（拼成 https://vc.feishu.cn/j/<id>）")
    parser.add_argument("--url", help="完整会议链接（优先于 --meeting-id）")
    parser.add_argument("--config", help="配置文件路径（JSON，含 meeting_id 或 url 字段）")
    parser.add_argument("--channel", default="chrome",
                        help="浏览器渠道：chrome（默认，用本机 Chrome）或 chromium")
    parser.add_argument("--join-timeout", type=float, default=1800,
                        help="等待人工入会的超时秒数（默认 1800s）")
    parser.add_argument("--log-level", default="INFO", help="日志级别（默认 INFO）")
    parser.add_argument("--ws-url", default="ws://127.0.0.1:8765/ws",
                        help="桥接服务地址（默认 8765；本机被占用时用 8876 等）")
    parser.add_argument("--profile-dir", default=str(BASE_DIR / ".agent-profile"),
                        help="Chrome 持久化 profile 目录（保存飞书登录态，默认 .agent-profile）")
    parser.add_argument("--debug-port", type=int, default=9222,
                        help="CDP 调试端口（默认 9222，0 关闭；开启后可用 hot_inject.py 免重启热注入）")
    return parser.parse_args()


def resolve_meeting_url(args: argparse.Namespace) -> str:
    """按优先级确定会议链接：命令行 --url > --meeting-id > 配置文件 > 首页。"""
    url: Optional[str] = args.url
    meeting_id: Optional[str] = args.meeting_id
    if args.config:
        cfg_path = Path(args.config)
        if not cfg_path.is_file():
            raise SystemExit(f"配置文件不存在: {cfg_path}")
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        url = url or cfg.get("url")
        meeting_id = meeting_id or cfg.get("meeting_id")
    if url:
        return url
    if meeting_id:
        digits = "".join(ch for ch in meeting_id if ch.isdigit())
        if len(digits) != 9:
            log.warning("会议 ID 通常应为 9 位数字，当前为 %r，仍按原样拼接", meeting_id)
        return f"https://vc.feishu.cn/j/{digits}"
    # 都不给则打开首页，由人工自行输入会议号
    return "https://vc.feishu.cn/"


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if not SHIM_PATH.is_file():
        raise SystemExit(f"未找到 shim.js: {SHIM_PATH}")
    shim_source = SHIM_PATH.read_text(encoding="utf-8")
    # 特性脚本：shim 之后按文件名字典序注入（彼此独立、自带防重复守卫）
    feature_sources = []
    if FEATURES_DIR.is_dir():
        for fp in sorted(FEATURES_DIR.glob("*.js")):
            feature_sources.append((fp.name, fp.read_text(encoding="utf-8")))
            log.info("已加载特性脚本: %s", fp.name)
    meeting_url = resolve_meeting_url(args)
    meeting_id = extract_meeting_id(meeting_url)
    log.info("目标会议链接: %s", meeting_url)
    launch_args = list(LAUNCH_ARGS)
    if args.debug_port > 0:
        launch_args.append(f"--remote-debugging-port={args.debug_port}")
        log.info("CDP 调试端口: %d（hot_inject.py 可免重启热注入）", args.debug_port)

    with sync_playwright() as p:
        # 1) 启动浏览器（持久化 profile：登录态复用，下次免登录）；TRAE 沙箱需 chromium_sandbox=False
        log.info("浏览器 profile 目录: %s", args.profile_dir)
        try:
            context = p.chromium.launch_persistent_context(
                user_data_dir=args.profile_dir,
                channel=args.channel, headless=False, args=launch_args,
                permissions=["microphone", "camera"], chromium_sandbox=False,
            )
        except PlaywrightError as e:
            log.warning("以 channel=%s 启动失败（%s），改用内置 Chromium", args.channel, e)
            context = p.chromium.launch_persistent_context(
                user_data_dir=args.profile_dir,
                headless=False, args=launch_args,
                permissions=["microphone", "camera"], chromium_sandbox=False,
            )

        # 2) 先声明 WS 覆盖地址，再注入 shim.js（都必须在页面任何脚本之前执行）
        context.add_init_script(f"window.__AGENT_WS_URL = {json.dumps(args.ws_url)};")
        context.add_init_script(shim_source)
        for _name, _src in feature_sources:
            context.add_init_script(_src)
        page = context.pages[0] if context.pages else context.new_page()
        # 抓取页面控制台（shim 日志、CSP 拦截报错都会出现在这里）
        page.on("console", lambda m: log.info("页面[%s] %s", m.type, m.text[:300]))
        page.on("pageerror", lambda e: log.warning("页面异常: %s", str(e)[:300]))
        page.on("dialog", lambda d: d.dismiss())  # 自动关掉 JS 弹窗（离开会议确认等），防驱动崩溃
        # 飞书会把会议室开在新标签页：新页面也要接管控制台日志
        context.on("page", lambda pg: (
            pg.on("console", lambda m: log.info("页面(新标签)[%s] %s", m.type, m.text[:300])),
            pg.on("pageerror", lambda e: log.warning("页面(新标签)异常: %s", str(e)[:300])),
            pg.on("dialog", lambda d: d.dismiss())))

        def probe_all():
            """遍历所有标签页，返回 (join_result, shim_state)，各维度取最优页（会议室常在新标签）"""
            best_result, best_shim = None, None
            for pg in context.pages:
                try:
                    r = pg.evaluate(DETECT_JOINED_JS)
                except PlaywrightError:
                    r = None  # 页面跳转中 evaluate 会失败，跳过
                try:
                    s = pg.evaluate(READ_SHIM_JS)
                except PlaywrightError:
                    s = None
                if r and (not best_result or len(r.get("hit", [])) > len(best_result.get("hit", []))):
                    best_result = r
                if s and (not best_shim
                          or s.get("stats", {}).get("remoteTracks", 0)
                          >= best_shim.get("stats", {}).get("remoteTracks", 0)):
                    best_shim = s
            return best_result, best_shim

        # 3) 打开会议页面，等待人工完成登录与入会
        page.goto(meeting_url, wait_until="domcontentloaded")
        log.info("页面已打开。请在浏览器窗口中完成登录并加入会议（可能需过等候室）……")

        joined = False
        deadline = time.monotonic() + args.join_timeout
        last_shim_log = 0.0
        # 妙记自动打开状态：done=已在浏览器里见到妙记页；clicks 防反复开合面板；cool=点击后冷却
        minutes_done = False
        minutes_clicks = 0
        minutes_cool = 0
        minutes_tried = 0
        minutes_gave_up = False
        try:
            while True:
                now = time.monotonic()
                try:
                    if not joined:
                        if now > deadline:
                            log.error("等待入会超时（%ss），退出", args.join_timeout)
                            break
                        # 打开的就是已散场的会议 → 直接退出，不空等
                        reason = detect_meeting_end(context, check_page_gone=False)
                        if reason:
                            log.info("会议已结束（%s），无需入会，智能体退出", reason)
                            write_status("ended", meeting_id)
                            break
                        result, shim_now = probe_all()
                        # 远端音频轨已建立 = 必定已在会中，比 UI 文案检测更可靠
                        rt = (shim_now or {}).get("stats", {}).get("remoteTracks", 0) if shim_now else 0
                        if (result and result.get("joined")) or rt >= 1:
                            joined = True
                            write_status("active", meeting_id)
                            log.info("检测到已入会！命中特征: %s，远端轨: %s，当前地址: %s",
                                     (result or {}).get("hit"), rt, (result or {}).get("url"))
                            log.info("开始周期性读取 window.__agentShim 状态（每 5s）……")
                        else:
                            shim_brief = "无 shim"
                            if shim_now:
                                shim_brief = "shim=%s 重连=%s" % (
                                    shim_now.get("state"), shim_now.get("stats", {}).get("wsReconnects"))
                            log.info("等待人工入会中……（命中特征: %s，%s）",
                                     (result or {}).get("hit") or [], shim_brief)
                            time.sleep(3)
                    else:
                        # 4) 入会后周期性打印 shim 状态 + 会议结束检测
                        if now - last_shim_log >= 5:
                            last_shim_log = now
                            # 会议结束（页面关闭/散场文案）→ 记录已自动保存，智能体自动退出
                            reason = detect_meeting_end(context, check_page_gone=True)
                            if reason:
                                log.info("检测到会议结束（%s）——会议记录已自动保存，智能体自动退出",
                                         reason)
                                write_status("ended", meeting_id)
                                break
                            shim = probe_all()[1]
                            if shim is None:
                                log.warning("window.__agentShim 不存在，shim.js 可能未注入成功")
                            else:
                                stats = shim.get("stats", {})
                                levels = shim.get("levels", {})
                                log.info(
                                    "shim 状态: ws=%s mic=%s 电平(下/上)=%.3f/%.3f "
                                    "远端轨=%s 下行帧=%s 上行帧=%s 欠载=%s 重连=%s",
                                    shim.get("state"), shim.get("micMode"),
                                    levels.get("downlink", 0), levels.get("uplink", 0),
                                    stats.get("remoteTracks"), stats.get("downlinkFrames"),
                                    stats.get("uplinkFrames"), stats.get("uplinkUnderruns"),
                                    stats.get("wsReconnects"),
                                )
                            # 5) 自动打开本场会议妙记页（采集脚本随新标签自动生效，转写实时同步给 B）
                            if not minutes_done:
                                m_pages = [pg for pg in context.pages if "/minutes/" in pg.url]
                                if m_pages:
                                    minutes_done = True
                                    log.info("妙记页已在浏览器中打开：%s（实时转写自动同步中）",
                                             m_pages[0].url[:80])
                                elif not minutes_gave_up:
                                    if minutes_cool > 0:
                                        minutes_cool -= 1
                                    else:
                                        minutes_tried += 1
                                        # 兜底：会议页找不到入口时（游客/免登常见），
                                        # 借登录态查妙记列表页里「录音中」的条目
                                        if minutes_tried >= 6 and minutes_tried % 6 == 0:
                                            try:
                                                hp = context.new_page()
                                                hp.goto("https://www.feishu.cn/minutes/home",
                                                        wait_until="domcontentloaded",
                                                        timeout=20000)
                                                hp.wait_for_timeout(3500)
                                                found = hp.evaluate(MINUTES_HOME_SCAN_JS)
                                                if found and found.get("minutesUrl"):
                                                    hp.goto(found["minutesUrl"],
                                                            wait_until="domcontentloaded",
                                                            timeout=20000)
                                                    minutes_done = True
                                                    log.info("经妙记列表页自动打开录音中的妙记：%s",
                                                             found["minutesUrl"][:80])
                                                else:
                                                    hp.close()
                                            except PlaywrightError as e:
                                                log.warning("妙记列表页探测失败: %s", str(e)[:100])
                                        if minutes_done:
                                            pass
                                        elif minutes_tried > 36 or minutes_clicks >= 3:
                                            # ~3 分钟或点击 3 次仍无妙记页 → 放弃自动点击，保留被动检测
                                            minutes_gave_up = True
                                            log.info("未找到妙记入口，停止自动打开"
                                                     "（会议中手动点「妙记」后采集会自动开始）")
                                        else:
                                            step = None
                                            for pg in context.pages:
                                                if "vc.feishu.cn" not in pg.url:
                                                    continue
                                                try:
                                                    step = pg.evaluate(MINUTES_STEP_JS)
                                                except PlaywrightError:
                                                    step = None
                                                if step and step.get("action") != "none":
                                                    break
                                            if step:
                                                act = step.get("action")
                                                if act == "open" and step.get("minutesUrl"):
                                                    try:
                                                        mp = context.new_page()
                                                        mp.goto(step["minutesUrl"],
                                                                wait_until="domcontentloaded",
                                                                timeout=20000)
                                                        minutes_done = True
                                                        log.info("已自动打开本场会议妙记页：%s",
                                                                 step["minutesUrl"][:80])
                                                    except PlaywrightError as e:
                                                        log.warning("打开妙记页失败: %s", str(e)[:120])
                                                elif act in ("click-miaoji", "confirm"):
                                                    minutes_clicks += 1
                                                    minutes_cool = 2
                                                    log.info("妙记自动打开：已点击（%s），等待页面响应", act)
                                                elif act == "click-more":
                                                    minutes_cool = 1
                        time.sleep(1)
                except PlaywrightError:
                    # 浏览器窗口被关闭/崩溃 = 会议监控自动停止
                    log.info("智能体浏览器已关闭——会议记录已自动保存，监控自动停止")
                    write_status("ended", meeting_id)
                    break
        except KeyboardInterrupt:
            log.info("收到 Ctrl+C，正在关闭浏览器……")
            write_status("ended", meeting_id)
        finally:
            context.close()
    log.info("智能体已退出")


if __name__ == "__main__":
    sys.exit(main())

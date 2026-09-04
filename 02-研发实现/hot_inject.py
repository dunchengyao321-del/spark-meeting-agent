"""
hot_inject.py —— 免重启热注入：通过 CDP 把 JS 文件注入正在运行的会议页面

用途：智能体浏览器以 --debug-port 9222（默认）启动后，shim 的任何迭代、
调试探针、临时功能脚本都可以热注入到正在运行的会议页，无需重启浏览器、
无需重新入会。

用法：
  python3 hot_inject.py <js文件> [--port 9222] [--match vc.feishu.cn/w/meeting]

热注入脚本约定：
  - 自包含 IIFE，自带防重复守卫（如 window.__featureXInjected）；
  - 需要清理时暴露 window.__featureXDestroy，便于下次注入前调用；
  - 向桥接发控制消息可用 window.__agentShim.sendControl({...})（shim 已暴露）。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright


async def main() -> int:
    parser = argparse.ArgumentParser(description="免重启热注入（CDP）")
    parser.add_argument("js_file", help="要注入的 JS 文件路径")
    parser.add_argument("--port", type=int, default=9222, help="CDP 端口（默认 9222）")
    parser.add_argument("--match", default="vc.feishu.cn/w/meeting",
                        help="目标页面 URL 包含的子串（默认会议页）")
    parser.add_argument("--expression", action="store_true",
                        help="把 js_file 参数当作 JS 表达式直接执行，而不是读文件")
    parser.add_argument("--open", metavar="URL",
                        help="未找到匹配页面时，自动打开该 URL（复用首个标签页跳转，没有则新开）")
    args = parser.parse_args()

    source = args.js_file if args.expression else Path(args.js_file).read_text(encoding="utf-8")

    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{args.port}")
        except Exception as exc:  # noqa: BLE001
            print(f"连接 CDP 失败（智能体浏览器是否在运行且 --debug-port={args.port}？）: {exc}")
            return 1
        for ctx in browser.contexts:
            for page in ctx.pages:
                if args.match in page.url:
                    try:
                        result = await page.evaluate(source)
                    except Exception as exc:  # noqa: BLE001
                        print(f"注入执行失败: {exc}")
                        return 2
                    print(f"已注入 {page.url[:70]}")
                    if result is not None:
                        print("返回值:", str(result)[:500])
                    return 0
        urls = [pg.url for ctx in browser.contexts for pg in ctx.pages]
        if args.open and browser.contexts:
            # 复用首个标签页跳转（init script 已注册在该目标上，导航后 shim 自动生效）
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            try:
                await page.goto(args.open, wait_until="domcontentloaded", timeout=20000)
            except Exception as exc:  # noqa: BLE001
                print(f"自动打开页面失败: {exc}")
                return 4
            print(f"已打开 {page.url[:70]}")
            return 0
        print(f"未找到匹配 {args.match!r} 的页面。当前标签页: {urls}")
        return 3


sys.exit(asyncio.run(main()))

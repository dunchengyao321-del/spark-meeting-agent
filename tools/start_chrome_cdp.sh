#!/bin/bash
# 星火 · 以调试端口启动 Chrome（独立配置副本，含书签与登录态）
# 前提：Chrome 已完全退出（Cmd+Q）。
set -e

if pgrep -x "Google Chrome" >/dev/null 2>&1; then
  echo "Chrome 正在运行。请先完全退出（Cmd+Q），再重新运行本脚本。"
  exit 1
fi

"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.spark/chrome-cdp-profile" &
sleep 3
if curl -s --max-time 3 http://127.0.0.1:9222/json/version >/dev/null; then
  echo "Chrome 调试模式已就绪（端口 9222）。"
else
  echo "端口未就绪，请检查。"
fi

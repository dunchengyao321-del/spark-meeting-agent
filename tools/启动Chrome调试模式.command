#!/bin/bash
# 星火 · 以调试端口启动 Chrome（双击或由系统打开均可）
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.spark/chrome-cdp-profile" &
sleep 2
exit 0

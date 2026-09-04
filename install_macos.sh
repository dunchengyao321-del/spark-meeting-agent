#!/bin/zsh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
command -v brew >/dev/null || { echo '需要 Homebrew'; exit 1; }
brew list ffmpeg >/dev/null 2>&1 || brew install ffmpeg
brew list blackhole-2ch >/dev/null 2>&1 || brew install blackhole-2ch
python3 -m pip install -r requirements-realtime.txt
python3 -m playwright install chromium
printf '\n依赖已安装。\n'
printf '首次登录（仅需手动执行）： python3 meeting_voice_bot.py login\n'
printf '检查登录态： python3 meeting_voice_host.py login-state\n'
printf '启动会议：     python3 meeting_voice_host.py start <会议号>\n'
printf '启动实时语音： python3 meeting_voice_host.py realtime start\n'

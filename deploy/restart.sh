#!/bin/bash
# 重启会议控制台常驻服务（macOS launchd）。
# 用 kickstart -k 原地重启，避免 bootout+bootstrap 的竞态（I/O error）；
# 服务未加载时自动改为 bootstrap，并带重试与健康检查。
set -u
LABEL="com.spark.meeting-console"
DOMAIN="gui/$(id -u)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
URL="http://127.0.0.1:8765/api/status"

if [ ! -f "$PLIST" ]; then
  echo "未找到 $PLIST，请先：cp $(dirname "$0")/com.spark.meeting-console.plist ~/Library/LaunchAgents/" >&2
  exit 1
fi

if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  launchctl kickstart -k "$DOMAIN/$LABEL" || { echo "kickstart 失败" >&2; exit 1; }
else
  launchctl bootstrap "$DOMAIN" "$PLIST" || {
    echo "bootstrap 失败，3 秒后重试…" >&2
    sleep 3
    launchctl bootstrap "$DOMAIN" "$PLIST" || { echo "仍失败，请查看 /tmp/spark_meeting_server.log" >&2; exit 1; }
  }
fi

for _ in 1 2 3 4 5 6 7 8 9 10; do
  if curl -fsS -m 2 "$URL" >/dev/null 2>&1; then
    echo "服务已就绪：http://127.0.0.1:8765/"
    exit 0
  fi
  sleep 1
done
echo "服务未在 10 秒内就绪，请查看 /tmp/spark_meeting_server.log" >&2
exit 1

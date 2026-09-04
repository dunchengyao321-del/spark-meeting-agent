#!/bin/zsh
# 端到端语音对话测试：通过页面 API 启动智能体 → 注入提问音频 → 抓取对话日志
set -u
cd "$(dirname "$0")"
UI=http://127.0.0.1:8765

echo '[1/5] 启动智能体（页面同款接口）'
curl -sS -X POST "$UI/api/agent" -H 'Content-Type: application/json' -d '{"action":"start"}' || { echo 'UI 不可用'; exit 1; }
echo

echo '[2/5] 等待连接...'
for i in $(seq 1 30); do
  sleep 1
  TAIL=$(curl -sS "$UI/api/agent" | /opt/homebrew/bin/python3 -c 'import sys,json;print(json.load(sys.stdin).get("log_tail",""))')
  echo "$TAIL" | grep -q '已连接' && { echo "已连接（第${i}秒）"; break; }
  echo "$TAIL" | grep -qiE 'error|❌|Traceback|配置未完成' && { echo '启动失败:'; echo "$TAIL" | tail -20; exit 1; }
done
echo "$TAIL" | grep -q '已连接' || { echo '30秒内未连接，日志:'; echo "$TAIL" | tail -20; exit 1; }

echo '[3/5] 注入提问音频到 BlackHole'
sleep 1
ffmpeg -hide_banner -loglevel error -i /tmp/ask.wav -f audiotoolbox -audio_device_index 0 - || echo '注入失败'

echo '[4/5] 等待回复（最多35秒）...'
for i in $(seq 1 35); do
  sleep 1
  TAIL=$(curl -sS "$UI/api/agent" | /opt/homebrew/bin/python3 -c 'import sys,json;print(json.load(sys.stdin).get("log_tail",""))')
  echo "$TAIL" | grep -q '你：' && echo "$TAIL" | grep -qE 'response|。|！|，' && break
done

echo '[5/5] 停止智能体'
curl -sS -X POST "$UI/api/agent" -H 'Content-Type: application/json' -d '{"action":"stop"}' >/dev/null
echo
echo '===== 对话日志 ====='
echo "$TAIL"

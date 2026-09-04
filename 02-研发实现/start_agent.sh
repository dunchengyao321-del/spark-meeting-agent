#!/bin/bash
# start_agent.sh —— 飞书会议语音智能体 · 一键启动
#
# 用法：
#   ./start_agent.sh <9位会议ID>      # 推荐：直接带会议号
#   ./start_agent.sh                  # 读 agent_config.json（没有则读 config.example.json 模板）
#
# 它会：自检依赖 -> 启动桥接服务(后台) -> 启动智能体浏览器(前台)。
# 前置条件：星火控制台服务已在运行（http://127.0.0.1:8765/ 可打开）、本机装有 Chrome。
set -u
cd "$(dirname "$0")"

CONFIG_FILE="agent_config.json"
[ -f "$CONFIG_FILE" ] || CONFIG_FILE="config.example.json"

read_cfg () { python3 -c "import json,sys; print(json.load(open('$CONFIG_FILE')).get('$1','$2'))" 2>/dev/null; }

MEETING_ID="${1:-$(read_cfg meeting_id '')}"
WS_URL="$(read_cfg ws_url 'ws://127.0.0.1:8876/ws')"
PIPELINE_URL="$(read_cfg pipeline_url 'ws://127.0.0.1:8765/ws/meeting')"
BRIDGE_PORT="$(read_cfg bridge_port '8876')"
GAIN="$(read_cfg downlink_gain '5.0')"
CHANNEL="$(read_cfg pipeline_channel '0')"
DEBUG_PORT="$(read_cfg debug_port '9222')"
JOIN_TIMEOUT="$(read_cfg join_timeout '3600')"

echo "==============================================="
echo "  飞书会议语音智能体 · 一键启动"
echo "==============================================="

# ---- 0. 前置检查 ----
if ! curl -s -m 3 -o /dev/null "http://127.0.0.1:8765/api/status"; then
  echo "[错误] 星火控制台服务未运行（http://127.0.0.1:8765/ 打不开）。"
  echo "       请先启动星火控制台，再运行本脚本。"
  exit 1
fi
echo "[检查] 星火控制台在线"

if [ ! -d "/Applications/Google Chrome.app" ]; then
  echo "[错误] 未找到 Chrome 浏览器（/Applications/Google Chrome.app）"
  exit 1
fi
echo "[检查] Chrome 已安装"

# ---- 1. 依赖自检（缺什么装什么） ----
python3 - <<'EOF'
import subprocess, sys
missing = []
for pkg in ["websockets", "playwright", "numpy"]:
    try:
        __import__(pkg)
    except ImportError:
        missing.append(pkg)
if missing:
    print("[依赖] 安装缺失包:", " ".join(missing), flush=True)
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--break-system-packages", "-q"] + missing)
print("[检查] Python 依赖就绪", flush=True)
EOF
if [ $? -ne 0 ]; then echo "[错误] Python 依赖安装失败"; exit 1; fi

# ---- 2. 启动桥接服务（后台） ----
if lsof -tiTCP:${BRIDGE_PORT} -sTCP:LISTEN >/dev/null 2>&1; then
  echo "[检查] 桥接服务已在运行（端口 ${BRIDGE_PORT}）"
else
  nohup python3 bridge_server.py --port "${BRIDGE_PORT}" \
      --pipeline-url "${PIPELINE_URL}" \
      --downlink-gain "${GAIN}" \
      --pipeline-channel "${CHANNEL}" \
      > bridge.log 2>&1 &
  echo $! > bridge.pid
  sleep 2
  echo "[启动] 桥接服务已后台启动（端口 ${BRIDGE_PORT}，日志 bridge.log）"
fi

# ---- 3. 启动智能体浏览器（前台，Ctrl+C 退出） ----
echo "[启动] 正在打开智能体浏览器……"
if [ -n "${MEETING_ID}" ]; then
  echo "[提示] 会议号: ${MEETING_ID}（浏览器将直接打开会议页）"
  python3 run_agent.py --url "https://vc.feishu.cn/w/meeting/${MEETING_ID}" \
      --ws-url "${WS_URL}" --debug-port "${DEBUG_PORT}" --join-timeout "${JOIN_TIMEOUT}"
else
  echo "[提示] 未指定会议号，浏览器打开后请手动输入 9 位会议 ID"
  python3 run_agent.py --ws-url "${WS_URL}" --debug-port "${DEBUG_PORT}" --join-timeout "${JOIN_TIMEOUT}"
fi

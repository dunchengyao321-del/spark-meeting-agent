#!/bin/bash
# start.sh —— 星火会议分身 · 全栈一键启动（通用版）
#
# 用法：
#   ./start.sh <9位会议号>     # 直接进指定会议
#   ./start.sh                 # 浏览器打开后手动输入会议号
#
# 拉起的组件：
#   1933  OpenViking 上下文数据库（知识库语义检索，可选——未就绪时自动降级关键词检索）
#   8765  A 智能体管线（听/说/仲裁）      8766  B 智能体（会议分身大脑）
#   8876  浏览器桥接（音频双向）          8877  监控台（http://127.0.0.1:8877/）
#
# 首次使用：在监控台「系统配置」页填入火山引擎 API Key、设置唤醒词与人格即可。
set -u
cd "$(dirname "$0")"

# 防止外部环境的 Python 变量污染虚拟环境（IDE/终端常会注入 PYTHONHOME）
unset PYTHONHOME PYTHONPATH 2>/dev/null || true

VENV=".venv/bin/python"
AGENT_DIR="02-研发实现"
PIP_MIRROR="https://pypi.tuna.tsinghua.edu.cn/simple"

echo "==============================================="
echo "  星火会议分身 · 一键启动"
echo "==============================================="

# ---- 0. 虚拟环境与依赖 ----
if [ ! -x "$VENV" ]; then
  echo "[初始化] 创建 Python 虚拟环境 .venv（约 1 分钟）…"
  (/opt/homebrew/opt/python@3.14/bin/python3.14 -m venv .venv 2>/dev/null) || python3 -m venv .venv
fi
echo "[检查] 校验依赖（缺失会自动用国内镜像安装）…"
NEED=0
for pkg in fastapi uvicorn websockets numpy playwright; do
  if ! "$VENV" -c "import $pkg" 2>/dev/null; then
    echo "  - 安装 $pkg …"
    "$VENV" -m pip install -q -i "$PIP_MIRROR" "$pkg" && NEED=1
  fi
done
[ "$NEED" = "1" ] && echo "[检查] 依赖已补齐" || echo "[检查] 依赖全部就绪"

# ---- 1. OpenViking 上下文数据库（1933，知识库语义检索） ----
# 未就绪不影响会议：管线会在 800ms 内降级为本地关键词检索。
OV_DIR="ov_runtime"
if lsof -tiTCP:1933 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "[检查] OpenViking 已在运行（1933）"
else
  OV_OK=1
  # 1a. Ollama（本地 embedding 服务，bge-m3 模型的载体）
  if ! command -v ollama >/dev/null 2>&1; then
    echo "[警告] 未安装 Ollama，跳过 OpenViking（知识库使用关键词检索）"
    OV_OK=0
  elif ! curl -s -m 2 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    echo "[启动] Ollama 服务…"
    nohup ollama serve > /tmp/spark_ollama.log 2>&1 &
    sleep 3
  fi
  # 1b. bge-m3 embedding 模型（首次拉取约 1.2GB）
  if [ "$OV_OK" = "1" ] && ! ollama list 2>/dev/null | grep -q "bge-m3"; then
    echo "[初始化] 拉取 bge-m3 embedding 模型（首次约 1.2GB，请耐心等待）…"
    ollama pull bge-m3 || OV_OK=0
  fi
  # 1c. 专用运行环境（openviking 暂无 Python 3.14 wheel，固定用 3.10）
  if [ "$OV_OK" = "1" ] && [ ! -x "$OV_DIR/venv/bin/python" ]; then
    if [ -x /opt/homebrew/opt/python@3.10/bin/python3.10 ]; then
      echo "[初始化] 创建 OpenViking 运行环境（约 2 分钟）…"
      /opt/homebrew/opt/python@3.10/bin/python3.10 -m venv "$OV_DIR/venv"
      "$OV_DIR/venv/bin/python" -m pip install -q openviking || OV_OK=0
    else
      echo "[警告] 缺少 python3.10（brew install python@3.10），跳过 OpenViking"
      OV_OK=0
    fi
  fi
  # 1d. 配置文件（首次从模板生成，需填入火山引擎 Key）
  if [ "$OV_OK" = "1" ] && [ ! -f "$OV_DIR/ov.conf" ]; then
    if [ -f "$OV_DIR/ov.conf.example" ]; then
      sed "s|__PROJECT_ROOT__|$PWD|g" "$OV_DIR/ov.conf.example" > "$OV_DIR/ov.conf"
      echo "[初始化] 已生成 $OV_DIR/ov.conf —— 请编辑填入你的火山引擎 ARK API Key 后重新运行本脚本"
      OV_OK=0
    else
      echo "[警告] 缺少 $OV_DIR/ov.conf 配置模板，跳过 OpenViking"
      OV_OK=0
    fi
  fi
  # 1e. 启动服务
  if [ "$OV_OK" = "1" ]; then
    export OPENVIKING_CONFIG_FILE="$PWD/$OV_DIR/ov.conf"
    nohup "$OV_DIR/venv/bin/openviking-server" > "$OV_DIR/server.log" 2>&1 &
    sleep 5
    if curl -s -m 3 http://127.0.0.1:1933/health >/dev/null 2>&1; then
      echo "[启动] OpenViking 上下文数据库 http://127.0.0.1:1933/"
    else
      echo "[警告] OpenViking 未就绪（详见 ov_runtime/server.log），知识库使用关键词检索"
    fi
  fi
fi

# ---- 2. A 智能体管线（8765） ----
if lsof -tiTCP:8765 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "[检查] A 管线已在运行（8765）"
else
  nohup "$VENV" -m server.app --port 8765 > /tmp/spark_pipeline.log 2>&1 &
  sleep 3
  echo "[启动] A 管线 http://127.0.0.1:8765/"
fi

# ---- 3. B 智能体 · 会议分身（8766） ----
if lsof -tiTCP:8766 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "[检查] B 智能体已在运行（8766）"
else
  nohup "$VENV" context_agent.py --port 8766 > /tmp/spark_context.log 2>&1 &
  sleep 2
  echo "[启动] B 智能体 http://127.0.0.1:8766/health"
fi

# ---- 4. 浏览器桥接（8876） ----
GAIN=$("$VENV" -c "import json;print(json.load(open('$AGENT_DIR/agent_config.json')).get('downlink_gain',2.0))" 2>/dev/null || echo 2.0)
if lsof -tiTCP:8876 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "[检查] 桥接已在运行（8876）"
else
  (cd "$AGENT_DIR" && nohup "../$VENV" bridge_server.py --port 8876 \
      --pipeline-url ws://127.0.0.1:8765/ws/meeting \
      --downlink-gain "$GAIN" --pipeline-channel 1 > /tmp/spark_bridge.log 2>&1 &)
  sleep 2
  echo "[启动] 桥接 ws://127.0.0.1:8876/ws"
fi

# ---- 5. 监控台（8877） ----
if lsof -tiTCP:8877 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "[检查] 监控台已在运行（8877）"
else
  (cd "$AGENT_DIR" && nohup "../$VENV" monitor_server.py > /tmp/spark_monitor.log 2>&1 &)
  sleep 1
  echo "[启动] 监控台 http://127.0.0.1:8877/"
fi

# ---- 6. 智能体浏览器（前台，Ctrl+C 退出） ----
MEETING_ID="${1:-}"
cd "$AGENT_DIR"
if [ -n "$MEETING_ID" ]; then
  echo "[启动] 打开智能体浏览器，进入会议 $MEETING_ID …"
  "../$VENV" run_agent.py --url "https://vc.feishu.cn/w/meeting/${MEETING_ID}" \
      --ws-url ws://127.0.0.1:8876/ws --debug-port 9222 --join-timeout 3600
else
  echo "[启动] 打开智能体浏览器（未指定会议号，请手动输入）……"
  "../$VENV" run_agent.py --ws-url ws://127.0.0.1:8876/ws --debug-port 9222 --join-timeout 3600
fi

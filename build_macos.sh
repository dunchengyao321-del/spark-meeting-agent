#!/bin/zsh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
python3 -m pip install -r requirements.txt
python3 tests/test_spark_app.py
OUT_DIR="$ROOT/dist/SparkFeishuVoiceApp"
rm -rf "$OUT_DIR"
python3 -m PyInstaller --noconfirm --clean \
  --name SparkFeishuVoice \
  --onefile \
  --distpath "$OUT_DIR" \
  --workpath "$ROOT/build/SparkFeishuVoice" \
  --specpath "$ROOT/build" \
  --add-data "config.example.json:." \
  --add-data "style_profile.py:." \
  --collect-submodules websockets \
  --hidden-import server.app \
  --hidden-import style_profile \
  --hidden-import meeting_voice_bot \
  --hidden-import meeting_voice_host \
  --hidden-import feishu_voice \
  --hidden-import tts_engine \
  --hidden-import ai_speech \
  --collect-all playwright \
  spark_app.py
cp config.example.json "$OUT_DIR/config.json.example"
cp style_profile.example.json "$OUT_DIR/"
printf '\nBuilt: %s/dist/SparkFeishuVoice\n' "$ROOT"

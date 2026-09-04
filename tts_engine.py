#!/usr/bin/env python3
"""星火 TTS 引擎 - 可插拔语音合成

config.json:
  tts_engine: say | volcano（默认 say）
  tts_voice:  say 音色名（如 Tingting）
  volcano_appid / volcano_access_token / volcano_cluster / volcano_voice_type
  （火山引擎支持音色复刻，voice_type 填复刻后的音色 ID，即可用你自己的声音）
"""
import base64
import json
import subprocess
import tempfile
import urllib.request
import uuid
from pathlib import Path

CONFIG_FILE = Path(__file__).parent / "config.json"
VOLCANO_TTS_URL = "https://openspeech.bytedance.com/api/v1/tts"


def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _say_wav(text: str, voice: str, out_wav: str):
    aiff = out_wav + ".aiff"
    subprocess.run(["say", "-v", voice, "-o", aiff, text],
                   check=True, capture_output=True, timeout=120)
    subprocess.run(["ffmpeg", "-y", "-i", aiff,
                    "-acodec", "pcm_s16le", "-ar", "48000", "-ac", "1", out_wav],
                   check=True, capture_output=True, timeout=60)
    Path(aiff).unlink(missing_ok=True)


def _volcano_wav(text: str, cfg: dict, out_wav: str):
    token = cfg.get("volcano_access_token", "")
    body = json.dumps({
        "app": {
            "appid": cfg.get("volcano_appid", ""),
            "token": token,
            "cluster": cfg.get("volcano_cluster", "volcano_tts"),
        },
        "audio": {
            "voice_type": cfg.get("volcano_voice_type", ""),
            "encoding": "wav",
            "speed_ratio": 1.0,
        },
        "request": {
            "reqid": str(uuid.uuid4()),
            "text": text,
            "operation": "query",
        },
    }).encode("utf-8")
    req = urllib.request.Request(VOLCANO_TTS_URL, data=body, method="POST", headers={
        "Authorization": f"Bearer;{token}",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    if result.get("code") != 3000:
        raise RuntimeError(f"火山 TTS 失败: {result}")
    Path(out_wav).write_bytes(base64.b64decode(result["data"]))


def synthesize_to_wav(text: str, out_wav: str = None) -> str:
    """合成语音为 WAV（48kHz 单声道），供 BlackHole 播放"""
    cfg = load_config()
    engine = cfg.get("tts_engine", "say")
    if out_wav is None:
        out_wav = str(Path(tempfile.gettempdir()) / f"spark_tts_{uuid.uuid4().hex[:8]}.wav")
    if engine == "volcano":
        _volcano_wav(text, cfg, out_wav)
    else:
        _say_wav(text, cfg.get("tts_voice", "Tingting"), out_wav)
    return out_wav


def wav_to_opus(wav_path: str, opus_path: str = None) -> str:
    if opus_path is None:
        opus_path = str(Path(wav_path).with_suffix(".opus"))
    subprocess.run(["ffmpeg", "-y", "-i", wav_path,
                    "-acodec", "libopus", "-ac", "1", "-ar", "16000", opus_path],
                   check=True, capture_output=True, timeout=60)
    return opus_path


def probe_duration_ms(path: str) -> int:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(path)],
                       check=True, capture_output=True, text=True, timeout=30)
    return int(float(r.stdout.strip()) * 1000)


def synthesize_to_opus(text: str) -> tuple[str, int]:
    """合成语音为 OPUS（16kHz 单声道），供飞书语音消息上传；返回 (路径, 时长ms)"""
    wav = synthesize_to_wav(text)
    opus = wav_to_opus(wav)
    Path(wav).unlink(missing_ok=True)
    return opus, probe_duration_ms(opus)

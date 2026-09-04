"""Apple ASR adapter (macOS Speech framework).

Default = Apple's speech service first (much better zh-CN accuracy), with
on-device recognition as the offline fallback. `apple_asr_local=true` in
config forces fully-offline on-device mode.

Wraps the bundled Swift CLI `tools/apple_asr`. The binary embeds an Info.plist
usage description (NSSpeechRecognitionUsageDescription) so macOS can grant it
Speech Recognition permission. Domain hotwords (wake names + KB terms) are fed
to the recognizer as contextualStrings via `--hotwords <file>`.
Exit codes from the CLI:
  0 ok · 1 recognition error · 2 not authorized · 3 recognizer unavailable
  4 timeout · 5 authorization dialog shown (waiting for the user) · 64 usage
"""

import asyncio
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from server.asr.base import ASRBase, ASRResult
from server.audio_utils import build_wav

TOOLS_DIR = Path(__file__).resolve().parent.parent.parent / "tools"
BINARY = TOOLS_DIR / "apple_asr"
SOURCE = TOOLS_DIR / "apple_asr.swift"
PLIST = TOOLS_DIR / "apple_asr_Info.plist"
SDK_CANDIDATES = [
    "/Library/Developer/CommandLineTools/SDKs/MacOSX26.2.sdk",
    "/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk",
]


def ensure_binary() -> None:
    """Compile the CLI on first use (requires Xcode command line tools)."""
    if BINARY.exists():
        return
    sdk = next((s for s in SDK_CANDIDATES if Path(s).exists()), None)
    args = ["swiftc", "-O"]
    if sdk:
        args += ["-sdk", sdk]
    args += ["-module-cache-path", "/tmp/swiftmc",
             "-Xlinker", "-sectcreate", "-Xlinker", "__TEXT",
             "-Xlinker", "__info_plist", "-Xlinker", str(PLIST),
             "-o", str(BINARY), str(SOURCE)]
    subprocess.run(args, check=True, capture_output=True, timeout=300)
    subprocess.run(["codesign", "-s", "-", "--force", str(BINARY)],
                   check=True, capture_output=True, timeout=60)


class AppleASR(ASRBase):
    name = "apple"
    sample_rate = 16000

    def __init__(self, config: dict):
        self.locale = str(config.get("apple_asr_locale", "")).strip() or "zh-CN"
        self.force_local = str(config.get("apple_asr_local", "")).strip().lower() in ("1", "true", "on")
        self._hotwords: list[str] = []
        self._hotwords_file: str | None = None

    def set_hotwords(self, words: list[str]) -> None:
        """Update domain hotwords (wake names, KB terms) for contextualStrings."""
        self._hotwords = [w for w in dict.fromkeys(words) if 1 < len(w) <= 20][:250]
        self._hotwords_file = None  # 触发下次转写时重写热词文件

    def configured(self) -> bool:
        return BINARY.exists() or bool(shutil.which("swiftc"))

    async def transcribe(self, pcm: bytes, rate: int) -> ASRResult:
        ensure_binary()
        wav_bytes = build_wav(pcm, rate)
        started = time.time()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fh:
            fh.write(wav_bytes)
            wav_path = fh.name
        cmd = [str(BINARY)]
        if self.force_local:
            cmd.append("--local")
        if self._hotwords:
            if not self._hotwords_file:
                with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False,
                                                 encoding="utf-8") as hw:
                    hw.write("\n".join(self._hotwords))
                    self._hotwords_file = hw.name
            cmd += ["--hotwords", self._hotwords_file]
        cmd += [wav_path, self.locale]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=40)
        finally:
            Path(wav_path).unlink(missing_ok=True)
        rc = proc.returncode
        if rc == 2:
            raise RuntimeError("本地 ASR 未授权：系统设置 → 隐私与安全性 → 语音识别，允许本工具（设置页可点「授权本地语音识别」）")
        if rc == 5:
            raise RuntimeError("已弹出语音识别授权窗口，请在桌面上点「允许」后重试")
        if rc != 0:
            raise RuntimeError(f"本地 ASR 失败 rc={rc}: {stderr.decode('utf-8', 'replace').strip()[:140]}")
        return ASRResult(text=stdout.decode("utf-8", "replace").strip(),
                         confidence=None,
                         duration_ms=int((time.time() - started) * 1000),
                         provider=self.name)

    @staticmethod
    async def request_authorization() -> dict:
        """Run the CLI with --auth to trigger/observe the macOS permission flow."""
        ensure_binary()
        proc = await asyncio.create_subprocess_exec(
            str(BINARY), "--auth",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
        except asyncio.TimeoutError:
            proc.kill()
            return {"authorized": False, "detail": "授权等待超时"}
        rc = proc.returncode
        detail = stdout.decode("utf-8", "replace").strip()
        return {"authorized": rc == 0, "rc": rc, "detail": detail}

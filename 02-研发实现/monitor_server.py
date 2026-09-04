"""
monitor_server.py —— 智能体监控台静态服务 + 配置/会议/知识库 API（8877 端口，零依赖）

页面：http://127.0.0.1:8877/
- 实时对话数据走 ws://127.0.0.1:8876/monitor（由 bridge_server 广播）
- 唤醒/人格配置直接读写星火控制台 http://127.0.0.1:8765/api/config（CORS 已放开）
- 桥接配置（agent_config.json）经本服务 /agent_config 读写
- 会议记录（transcripts/，按「日期-会议号」分文件）经本服务读取
- 一键入会 /api/join_meeting：更新会议号配置 + 自动入会
  （有飞书页→热跳转；无飞书页→自动打开；浏览器没跑→自动拉起，全程免手工）
- 知识库导入 /api/kb_upload：批量写入 kb/ 并触发两端热重建索引
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
HTML = BASE / "monitor.html"
CFG = BASE / "agent_config.json"
CFG_EXAMPLE = BASE / "config.example.json"
TRANSCRIPT_DIR = BASE / "transcripts"
KB_DIR = ROOT / "kb"
PORT = 8877
B_API = "http://127.0.0.1:8766"
PIPELINE_API = "http://127.0.0.1:8765"


def _read_agent_config() -> dict:
    src = CFG if CFG.exists() else CFG_EXAMPLE
    try:
        return json.loads(src.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _read_meeting_status() -> dict:
    """会议状态（run_agent.py 落盘）：active=进行中 / ended=已结束。"""
    try:
        return json.loads((BASE / "meeting_status.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"state": "unknown"}


def _post_json(url: str, body: dict, timeout: int = 10) -> dict:
    try:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body, ctype: str = "text/html; charset=utf-8") -> None:
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False),
                   "application/json; charset=utf-8")

    def log_message(self, *_args) -> None:  # 静音访问日志
        pass

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        return json.loads(raw or "{}")

    # ------------------------------------------------------------------ GET
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            self._send(200, HTML.read_text(encoding="utf-8"))
        elif path == "/agent_config":
            self._json(_read_agent_config())
        elif path == "/api/transcript_meetings":
            self._list_meetings()
        elif path == "/api/transcripts":
            qs = parse_qs(parsed.query)
            self._serve_transcripts(qs.get("meeting", [""])[0])
        elif path == "/api/meeting_status":
            self._json(_read_meeting_status())
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    # ------------------------------------------------------------------ PUT
    def do_PUT(self) -> None:
        if self.path == "/agent_config":
            body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8")
            json.loads(body)  # 校验 JSON 合法性
            CFG.write_text(body, encoding="utf-8")
            self._json({"ok": True})
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    # ------------------------------------------------------------------ POST
    def do_POST(self) -> None:
        if self.path == "/api/join_meeting":
            self._join_meeting()
        elif self.path == "/api/kb_upload":
            self._kb_upload()
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    # ------------------------------------------------------------------ 一键入会
    @staticmethod
    def _cdp_alive(port: int) -> bool:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/list", timeout=2) as resp:
                return resp.status == 200
        except Exception:  # noqa: BLE001
            return False

    def _launch_browser(self, cfg: dict, url: str) -> str:
        """浏览器没在跑：清理僵死的旧启动器，后台拉起新的智能体浏览器。"""
        subprocess.run(["pkill", "-f", "run_agent.py"], capture_output=True)
        time.sleep(1)
        debug_port = int(cfg.get("debug_port", 9222))
        cmd = [sys.executable, str(BASE / "run_agent.py"),
               "--url", url,
               "--ws-url", str(cfg.get("ws_url", "ws://127.0.0.1:8876/ws")),
               "--debug-port", str(debug_port),
               "--join-timeout", str(cfg.get("join_timeout", 3600))]
        logf = open(BASE / "agent_browser.log", "ab")
        subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT,
                         cwd=str(BASE), start_new_session=True)
        for _ in range(12):  # 等 CDP 就绪（最多 ~12s）
            time.sleep(1)
            if self._cdp_alive(debug_port):
                return "浏览器已自动启动并打开会议页；如未登录请在浏览器窗口完成登录"
        return "浏览器已启动（CDP 尚未就绪），请查看浏览器窗口"

    def _join_meeting(self) -> None:
        """更新会议号配置 + 自动入会（三级策略，全部免手工）：

        1) 浏览器在跑且有飞书页面 → CDP 热跳转；
        2) 浏览器在跑但无飞书页面 → 自动打开会议页（复用首个标签页，shim 自动生效）；
        3) 浏览器没在跑 → 清理僵死旧进程，自动拉起智能体浏览器并入会。
        """
        try:
            payload = self._body()
        except json.JSONDecodeError:
            self._json({"ok": False, "error": "请求体不是合法 JSON"}, 400)
            return
        meeting_id = "".join(ch for ch in str(payload.get("meeting_id", "")) if ch.isdigit())
        if len(meeting_id) < 8:
            self._json({"ok": False, "error": "会议号格式不对（应为 9 位数字）"}, 400)
            return
        # 1) 更新配置（会议记录隔离以它为准）
        cfg = _read_agent_config()
        prev_meeting = str(cfg.get("meeting_id", ""))
        cfg["meeting_id"] = meeting_id
        CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        # 1.5) 换会即清旧妙记实时转写：杜绝上一场内容混入本场上下文
        #      （同会重入保留——浏览器中途重启时妙记页还在推送，文件仍是本场的）
        if meeting_id != prev_meeting:
            for f in (ROOT / "external_transcripts").glob("minutes_live.*"):
                try:
                    f.unlink()
                except OSError:
                    pass
            # 换会同步清空「待本人回答」队列：上一场的问题不带入新会议（B 未启动时静默跳过）
            try:
                req = urllib.request.Request(f"{B_API}/pending_questions", method="DELETE")
                urllib.request.urlopen(req, timeout=3).close()
            except Exception:  # noqa: BLE001
                pass
        url = f"https://vc.feishu.cn/w/meeting/{meeting_id}"
        debug_port = int(cfg.get("debug_port", 9222))

        # 2) 浏览器在跑：热跳转或自动打开会议页
        if self._cdp_alive(debug_port):
            try:
                result = subprocess.run(
                    [sys.executable, str(BASE / "hot_inject.py"),
                     f"location.href = '{url}'; 'jumping'",
                     "--expression", "--match", "feishu.cn",
                     "--port", str(debug_port), "--open", url],
                    capture_output=True, text=True, timeout=40)
                out = (result.stdout or "") + (result.stderr or "")
                if "已注入" in out:
                    self._json({"ok": True, "meeting_id": meeting_id, "jumped": True,
                                "hint": f"已跳转智能体浏览器，正在进入会议 {meeting_id}"})
                    return
                if "已打开" in out:
                    self._json({"ok": True, "meeting_id": meeting_id, "jumped": True,
                                "hint": f"已自动打开会议页面，正在进入会议 {meeting_id}"})
                    return
            except Exception:  # noqa: BLE001 - 热跳转失败则落到拉起浏览器
                pass

        # 3) 浏览器没在跑（或热跳转失败）：自动拉起
        hint = self._launch_browser(cfg, url)
        self._json({"ok": True, "meeting_id": meeting_id, "jumped": True, "hint": hint})

    # ------------------------------------------------------------------ 知识库导入
    def _kb_upload(self) -> None:
        """批量写入知识库文件（JSON: {files: [{name, content}]}），并热重建索引。"""
        try:
            payload = self._body()
        except json.JSONDecodeError:
            self._json({"ok": False, "error": "请求体不是合法 JSON"}, 400)
            return
        files = payload.get("files") or []
        if not files:
            self._json({"ok": False, "error": "没有文件内容"}, 400)
            return
        KB_DIR.mkdir(exist_ok=True)
        saved, skipped = [], []
        for f in files[:50]:
            name = Path(str(f.get("name", ""))).name  # 防目录穿越
            if not name or Path(name).suffix.lower() not in (".md", ".markdown", ".txt"):
                skipped.append(name or "(未命名)")
                continue
            content = str(f.get("content", ""))
            if not content.strip():
                skipped.append(name)
                continue
            (KB_DIR / name).write_text(content, encoding="utf-8")
            saved.append(name)
        # 两端热重建索引（管线 8765 + B 智能体 8766），失败不阻断
        r1 = _post_json(f"{PIPELINE_API}/api/kb/ingest", {}, timeout=30)
        r2 = _post_json(f"{B_API}/kb/reload", {}, timeout=30)
        self._json({"ok": True, "saved": saved, "skipped": skipped,
                    "pipeline_chunks": r1.get("chunks"), "b_chunks": r2.get("chunks")})

    # ------------------------------------------------------------------ 会议记录
    def _list_meetings(self) -> None:
        """按会议分组列出记录文件：当前会议置顶，其余按时间倒序。"""
        current = str(_read_agent_config().get("meeting_id", ""))
        # 只有「进行中」的会议才算当前会议（散场后不再标「当前」）
        active = current if _read_meeting_status().get("state") == "active" else ""
        meetings = []
        if TRANSCRIPT_DIR.exists():
            for f in sorted(TRANSCRIPT_DIR.glob("meeting-*.jsonl"),
                            key=lambda x: x.stat().st_mtime, reverse=True):
                # 文件名：meeting-<日期>-<会议号>.jsonl
                parts = f.stem.split("-")
                day = parts[1] if len(parts) > 1 else ""
                mid = parts[2] if len(parts) > 2 else ""
                lines = 0
                first_ts = last_ts = ""
                try:
                    with open(f, encoding="utf-8") as fh:
                        for raw in fh:
                            raw = raw.strip()
                            if not raw:
                                continue
                            lines += 1
                            try:
                                ts = json.loads(raw).get("ts", "")
                                if ts and not first_ts:
                                    first_ts = ts
                                if ts:
                                    last_ts = ts
                            except json.JSONDecodeError:
                                continue
                except OSError:
                    pass
                meetings.append({"date": day, "meeting_id": mid, "lines": lines,
                                 "first": first_ts, "last": last_ts,
                                 "current": mid == active and bool(active)})
        self._json({"current": current, "meetings": meetings})

    def _serve_transcripts(self, meeting: str) -> None:
        """读取指定会议的记录；不传 meeting 时默认当前会议。"""
        if not meeting:
            meeting = str(_read_agent_config().get("meeting_id", ""))
        if not meeting or not TRANSCRIPT_DIR.exists():
            self._json([])
            return
        files = sorted(TRANSCRIPT_DIR.glob(f"meeting-*-{meeting}.jsonl"),
                       key=lambda x: x.stat().st_mtime, reverse=True)
        if not files:
            self._json([])
            return
        records = []
        for line in files[0].read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        self._json(records)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"监控页: http://127.0.0.1:{PORT}/", flush=True)
    server.serve_forever()

#!/usr/bin/env python3
"""星火·飞书语音分身 PoC

以用户本人身份在飞书发送语音消息：
macOS say TTS -> OPUS 转码 -> im/v1/files 上传 -> im/v1/messages 用户身份发送

用法:
  python3 feishu_voice.py login                     # OAuth 授权（首次）
  python3 feishu_voice.py chats                     # 列出我所在的群聊
  python3 feishu_voice.py speak --chat 测试 "你好"   # 按群名关键词发送
  python3 feishu_voice.py speak --chat-id oc_xxx "你好"
"""

import argparse
import http.server
import json
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

BASE = "https://open.feishu.cn/open-apis"
AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
REDIRECT_URI = "http://localhost:8964/callback"
SCOPES = "im:message im:message.send_as_user im:chat:readonly offline_access"
ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.json"
TOKEN_FILE = ROOT / ".tokens.json"


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        sys.exit(f"缺少 {CONFIG_FILE}，请复制 config.example.json 并填入 app_secret")
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    if not cfg.get("app_id") or not cfg.get("app_secret"):
        sys.exit("请先在 config.json 中填写 app_id 和 app_secret")
    return cfg


def api(method: str, path: str, token: str = None, data: dict = None, params: dict = None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        sys.exit(f"API 调用失败 {method} {path} -> HTTP {e.code}: {detail}")


def load_tokens() -> dict:
    if TOKEN_FILE.exists():
        return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    return {}


def save_tokens(tokens: dict):
    TOKEN_FILE.write_text(json.dumps(tokens, ensure_ascii=False, indent=2), encoding="utf-8")


def get_tenant_token(cfg: dict) -> str:
    tokens = load_tokens()
    cached = tokens.get("tenant")
    if cached and cached.get("expire_at", 0) > time.time() + 60:
        return cached["token"]
    resp = api("POST", "/auth/v3/tenant_access_token/internal",
               data={"app_id": cfg["app_id"], "app_secret": cfg["app_secret"]})
    if resp.get("code") != 0:
        sys.exit(f"获取 tenant_access_token 失败: {resp}")
    tokens["tenant"] = {"token": resp["tenant_access_token"],
                        "expire_at": time.time() + resp.get("expire", 7200)}
    save_tokens(tokens)
    return resp["tenant_access_token"]


def exchange_code(cfg: dict, code: str) -> dict:
    resp = api("POST", "/authen/v2/oauth/token", data={
        "grant_type": "authorization_code",
        "client_id": cfg["app_id"],
        "client_secret": cfg["app_secret"],
        "code": code,
        "redirect_uri": REDIRECT_URI,
    })
    if resp.get("code") != 0:
        sys.exit(f"换取 user_access_token 失败: {resp}")
    return resp


def refresh_user_token(cfg: dict, tokens: dict) -> dict:
    refresh_token = tokens.get("user", {}).get("refresh_token")
    if not refresh_token:
        sys.exit("user_access_token 已过期且无 refresh_token，请重新 login")
    resp = api("POST", "/authen/v2/oauth/token", data={
        "grant_type": "refresh_token",
        "client_id": cfg["app_id"],
        "client_secret": cfg["app_secret"],
        "refresh_token": refresh_token,
    })
    if resp.get("code") != 0:
        sys.exit(f"刷新 user_access_token 失败: {resp}，请重新 login")
    return resp


def get_user_token(cfg: dict) -> str:
    tokens = load_tokens()
    user = tokens.get("user")
    if not user:
        sys.exit("尚未授权，请先运行: python3 feishu_voice.py login")
    if user.get("expire_at", 0) > time.time() + 60:
        return user["token"]
    data = refresh_user_token(cfg, tokens)
    tokens["user"] = {
        "token": data["access_token"],
        "refresh_token": data.get("refresh_token", user.get("refresh_token")),
        "expire_at": time.time() + data.get("expires_in", 7200),
        "scope": data.get("scope", ""),
        "info": user.get("info"),
    }
    save_tokens(tokens)
    return data["access_token"]


def cmd_login(_args):
    cfg = load_config()
    state = secrets.token_urlsafe(16)
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            query = urllib.parse.parse_qs(parsed.query)
            code = query.get("code", [None])[0]
            error = query.get("error", [None])[0]
            if not code and not error:
                # 忽略无参数的预取/探测请求
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"waiting")
                return
            result["code"] = code
            result["state"] = query.get("state", [None])[0]
            result["error"] = error
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h2>授权成功，可以关闭此页面，回到终端。</h2>".encode())

        def log_message(self, *_):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 8964), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    auth_url = (AUTHORIZE_URL + "?" + urllib.parse.urlencode({
        "client_id": cfg["app_id"],
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
        "prompt": "consent",
    }))
    print("请在浏览器中完成授权（如未自动打开，请手动访问）：")
    print(auth_url)
    webbrowser.open(auth_url)

    deadline = time.time() + 300
    while "code" not in result and "error" not in result and time.time() < deadline:
        time.sleep(0.5)
    server.shutdown()

    if result.get("error"):
        sys.exit(f"授权失败: {result}")
    if result.get("state") != state:
        print(f"⚠️ state 不一致: got={result.get('state')!r} expected={state!r}（本地回调，继续处理）", flush=True)
    if not result.get("code"):
        sys.exit("授权超时，请重试")

    data = exchange_code(cfg, result["code"])
    user_info = api("GET", "/authen/v1/user_info", token=data["access_token"])
    info = user_info.get("data", {})
    tokens = load_tokens()
    tokens["user"] = {
        "token": data["access_token"],
        "refresh_token": data.get("refresh_token"),
        "expire_at": time.time() + data.get("expires_in", 7200),
        "scope": data.get("scope", ""),
        "info": {"name": info.get("name"), "open_id": info.get("open_id")},
    }
    save_tokens(tokens)
    print(f"授权成功: {info.get('name')} (open_id={info.get('open_id')})")
    print(f"已获权限: {data.get('scope')}")


def tts_to_opus(text: str, voice: str) -> tuple[str, int]:
    from tts_engine import synthesize_to_opus
    return synthesize_to_opus(text)


def upload_audio(cfg: dict, opus_path: str, duration_ms: int) -> str:
    tenant_token = get_tenant_token(cfg)
    boundary = "----spark" + secrets.token_hex(8)
    fields = {"file_type": "opus", "file_name": Path(opus_path).name,
              "duration": str(duration_ms)}
    body = b""
    for key, value in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                 f'name="{key}"\r\n\r\n{value}\r\n').encode()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; "
             f'name="file"; filename="{Path(opus_path).name}"\r\n'
             f"Content-Type: application/octet-stream\r\n\r\n").encode()
    body += Path(opus_path).read_bytes() + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(BASE + "/im/v1/files", data=body, method="POST")
    req.add_header("Authorization", f"Bearer {tenant_token}")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        sys.exit(f"上传音频失败: HTTP {e.code}: {e.read().decode(errors='replace')}")
    if data.get("code") != 0:
        sys.exit(f"上传音频失败: {data}")
    return data["data"]["file_key"]


def send_audio_as_user(cfg: dict, chat_id: str, file_key: str) -> dict:
    user_token = get_user_token(cfg)
    resp = api("POST", "/im/v1/messages", token=user_token,
               params={"receive_id_type": "chat_id"},
               data={"receive_id": chat_id, "msg_type": "audio",
                     "content": json.dumps({"file_key": file_key})})
    if resp.get("code") != 0:
        sys.exit(f"发送语音失败: {resp}")
    return resp["data"]


def find_chat(cfg: dict, keyword: str) -> tuple[str, str]:
    user_token = get_user_token(cfg)
    matches = []
    page_token = ""
    while True:
        params = {"user_id_type": "open_id", "page_size": "100"}
        if page_token:
            params["page_token"] = page_token
        resp = api("GET", "/im/v1/chats", token=user_token, params=params)
        if resp.get("code") != 0:
            sys.exit(f"获取群聊列表失败: {resp}")
        data = resp.get("data", {})
        for item in data.get("items", []):
            name = item.get("name") or "(未命名)"
            if keyword in name:
                matches.append((item["chat_id"], name))
        if not data.get("has_more"):
            break
        page_token = data.get("page_token", "")
        if not page_token:
            break
    if not matches:
        sys.exit(f"未找到包含「{keyword}」的群聊，请先运行 chats 查看")
    if len(matches) > 1:
        print("匹配到多个群聊，使用第一个：")
        for chat_id, name in matches:
            print(f"  {name} ({chat_id})")
    return matches[0]


def cmd_chats(_args):
    cfg = load_config()
    user_token = get_user_token(cfg)
    page_token = ""
    total = 0
    while True:
        params = {"user_id_type": "open_id", "page_size": "100"}
        if page_token:
            params["page_token"] = page_token
        resp = api("GET", "/im/v1/chats", token=user_token, params=params)
        if resp.get("code") != 0:
            sys.exit(f"获取群聊列表失败: {resp}")
        data = resp.get("data", {})
        for item in data.get("items", []):
            total += 1
            print(f"{item.get('name') or '(未命名)'}  chat_id={item['chat_id']}")
        if not data.get("has_more"):
            break
        page_token = data.get("page_token", "")
        if not page_token:
            break
    print(f"共 {total} 个会话")


def cmd_speak(args):
    cfg = load_config()
    if not args.chat and not args.chat_id:
        sys.exit("请指定 --chat <群名关键词> 或 --chat-id <oc_xxx>")
    if args.chat_id:
        chat_id, chat_name = args.chat_id, args.chat_id
    else:
        chat_id, chat_name = find_chat(cfg, args.chat)
    print(f"目标会话: {chat_name}")
    print(f"合成语音: {args.text}")
    opus_path, duration_ms = tts_to_opus(args.text, cfg.get("tts_voice", "Tingting"))
    print(f"音频已生成: {duration_ms}ms")
    file_key = upload_audio(cfg, opus_path, duration_ms)
    print(f"已上传: {file_key}")
    message = send_audio_as_user(cfg, chat_id, file_key)
    print(f"发送成功 message_id={message.get('message_id')}")
    print("请到飞书确认：这条语音应以你本人身份出现在会话里")


def main():
    parser = argparse.ArgumentParser(description="星火·飞书语音分身 PoC")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("login", help="OAuth 授权").set_defaults(func=cmd_login)
    sub.add_parser("chats", help="列出我所在的会话").set_defaults(func=cmd_chats)
    speak_parser = sub.add_parser("speak", help="以本人身份发送语音")
    speak_parser.add_argument("text", help="要说的内容")
    speak_parser.add_argument("--chat", help="群名关键词")
    speak_parser.add_argument("--chat-id", help="会话 chat_id (oc_xxx)")
    speak_parser.set_defaults(func=cmd_speak)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

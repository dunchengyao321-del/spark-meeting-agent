#!/usr/bin/env python3
"""Unified entry point for the Spark voice assistant project."""

import argparse
import sys


def _delegate(module: str, argv: list[str]):
    sys.argv = [module, *argv]
    if module == "server.app":
        from server.app import main as entry
    elif module == "meeting_voice_host.py":
        from meeting_voice_host import main as entry
    elif module == "meeting_voice_bot.py":
        from meeting_voice_bot import main as entry
    elif module == "style_profile.py":
        from style_profile import main as entry
    else:
        raise RuntimeError(f"unsupported module: {module}")
    return entry()


def main():
    parser = argparse.ArgumentParser(description="星火语音智能体统一入口")
    sub = parser.add_subparsers(dest="command", required=True)

    web = sub.add_parser("web", help="启动网页语音控制台")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--host", default="127.0.0.1")

    config = sub.add_parser("config", help="打开本地配置页面（已并入网页控制台）")
    config.add_argument("--port", type=int, default=8765)
    config.add_argument("--host", default="127.0.0.1")
    config.add_argument("--no-browser", action="store_true")

    login = sub.add_parser("feishu-login", help="打开飞书扫码登录")
    login.set_defaults(command_name="feishu-login")

    speak = sub.add_parser("feishu-speak", help="以本人身份发送飞书语音")
    speak.add_argument("text")
    speak.add_argument("--chat")
    speak.add_argument("--chat-id")
    speak.set_defaults(command_name="feishu-speak")

    meeting = sub.add_parser("meeting", help="飞书会议常驻主机")
    meeting.add_argument("meeting_action", choices=["start", "stop", "status"])
    meeting.add_argument("meeting", nargs="?", help="会议号")
    meeting.add_argument("--name", default="星火")
    meeting.add_argument("--password")
    meeting.set_defaults(command_name="meeting")

    style = sub.add_parser("style", help="管理说话风格")
    style.add_argument("args", nargs=argparse.REMAINDER)
    style.set_defaults(command_name="style")

    args = parser.parse_args()

    if args.command == "web":
        return _delegate("server.app", ["--port", str(args.port), "--host", args.host])
    if args.command == "config":
        # 旧 config_ui.py 已下线：设置面板已并入网页控制台（server.app）。
        print(f"配置已并入网页控制台：http://{args.host}:{args.port}/ （设置按钮）")
        return _delegate("server.app", ["--port", str(args.port), "--host", args.host])
    if args.command == "feishu-login":
        return _delegate("meeting_voice_bot.py", ["login"])
    if args.command == "feishu-speak":
        argv = ["speak", args.text]
        if args.chat:
            argv.extend(["--chat", args.chat])
        if args.chat_id:
            argv.extend(["--chat-id", args.chat_id])
        return _delegate("feishu_voice.py", argv)
    if args.command == "meeting":
        argv = [args.meeting_action]
        if args.meeting:
            argv.append(args.meeting)
        argv.extend(["--name", args.name])
        if args.password:
            argv.extend(["--password", args.password])
        return _delegate("meeting_voice_host.py", argv)
    if args.command == "style":
        return _delegate("style_profile.py", args.args)


if __name__ == "__main__":
    main()

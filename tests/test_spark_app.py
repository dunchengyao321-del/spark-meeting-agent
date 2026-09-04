"""Standalone smoke test for the unified CLI entry point."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import spark_app  # noqa: E402

FAILS = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}{(' — ' + detail) if detail and not condition else ''}")
    if not condition:
        FAILS.append(name)


def run_case(argv, expected_module, expected_args):
    captured = {}

    def fake_delegate(module, args):
        captured["module"] = module
        captured["args"] = args
        return 0

    original_argv = sys.argv[:]
    try:
        spark_app._delegate = fake_delegate  # type: ignore[attr-defined]
        sys.argv = ["spark_app.py", *argv]
        spark_app.main()
    finally:
        sys.argv = original_argv
    return captured == {"module": expected_module, "args": expected_args}, captured


def main():
    ok, got = run_case(["web", "--port", "9000", "--host", "0.0.0.0"],
                       "server.app", ["--port", "9000", "--host", "0.0.0.0"])
    check("CLI web 入口", ok, str(got))

    ok, got = run_case(["config", "--port", "8787", "--no-browser"],
                       "server.app", ["--port", "8787", "--host", "127.0.0.1"])
    check("CLI 配置入口（并入网页控制台）", ok, str(got))

    ok, got = run_case(["feishu-login"], "meeting_voice_bot.py", ["login"])
    check("CLI 飞书登录入口", ok, str(got))

    ok, got = run_case(["feishu-speak", "你好", "--chat", "测试"],
                       "feishu_voice.py", ["speak", "你好", "--chat", "测试"])
    check("CLI 飞书发语音入口", ok, str(got))

    ok, got = run_case(["meeting", "start", "588123808", "--name", "deli"],
                       "meeting_voice_host.py", ["start", "588123808", "--name", "deli"])
    check("CLI 会议启动入口", ok, str(got))

    print("spark_app:", "ALL PASS" if not FAILS else f"{len(FAILS)} FAILED")
    if FAILS:
        sys.exit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Maintain a transparent, user-editable speaking-style profile.

This learns wording, cadence and preferences from examples. It does not claim
to clone a person's biological voice; Realtime voice selection and voice
cloning are separate provider capabilities.
"""

import argparse
import json
from pathlib import Path

def profile_file() -> Path:
    candidates = [Path.cwd() / "style_profile.json", Path(__file__).resolve().parent / "style_profile.json"]
    return next((path for path in candidates if path.exists()), candidates[0])


def load_profile() -> dict:
    if not profile_file().exists():
        return {}
    return json.loads(profile_file().read_text(encoding="utf-8"))


def save_profile(profile: dict):
    profile_file().write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")


def add_example(text: str):
    text = text.strip()
    if not text:
        return
    profile = load_profile()
    profile.setdefault("examples", [])
    if text not in profile["examples"]:
        profile["examples"].append(text)
        profile["examples"] = profile["examples"][-50:]
        save_profile(profile)


def build_style_prompt(profile: dict) -> str:
    if not profile:
        return ""
    examples = profile.get("examples", [])[-20:]
    rules = profile.get("rules", [])
    lines = ["用户的说话风格画像（只模仿表达习惯，不伪造事实）："]
    if rules:
        lines.append("偏好：" + "；".join(rules))
    if examples:
        lines.append("用户示例：" + " | ".join(examples))
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="学习和管理 AI 的说话风格")
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("learn", help="添加一条你亲自说过的话")
    add.add_argument("text")
    rule = sub.add_parser("rule", help="添加一条表达偏好")
    rule.add_argument("text")
    sub.add_parser("show", help="查看当前画像")
    args = parser.parse_args()
    profile = load_profile()
    profile.setdefault("examples", [])
    profile.setdefault("rules", [])
    if args.command == "learn":
        profile["examples"].append(args.text)
        save_profile(profile)
        print(f"已保存示例，共 {len(profile['examples'])} 条")
    elif args.command == "rule":
        profile["rules"].append(args.text)
        save_profile(profile)
        print(f"已保存偏好，共 {len(profile['rules'])} 条")
    else:
        print(build_style_prompt(profile) or "尚未建立风格画像")


if __name__ == "__main__":
    main()

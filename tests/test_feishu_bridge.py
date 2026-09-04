"""飞书桥接层：绑定校验、WAV 封装、称呼匹配（全离线，不起浏览器）。"""

import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from integrations.feishu.bridge import (MEETING_NO_RE, pcm_to_wav,  # noqa: E402
                                        validate_binding)
from server.meeting.turns import TurnArbiter  # noqa: E402

FAILS = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}{(' — ' + str(detail)[:160]) if detail and not condition else ''}")
    if not condition:
        FAILS.append(name)


IN_MEETING = "静音 解除静音 共享屏幕 离开会议 聊天 参会人 12"
TITLE_FMT = "项目周会 - 飞书会议 {no}"

# ---- A. 绑定校验
r = validate_binding(IN_MEETING, TITLE_FMT.format(no="588123808"), "588123808")
check("A1 会中且会议号匹配 → 绑定", r.ok and r.state == "bound")

r = validate_binding(IN_MEETING, TITLE_FMT.format(no="588123808"), "111222333")
check("A2 会议号不匹配 → 拒绝并给出实际会议号",
      not r.ok and r.state == "rejected" and r.candidates == ["588123808"])

r = validate_binding(IN_MEETING, "项目周会 - 飞书会议", "588123808")
check("A3 会中但读不到会议号 → 未校验绑定", r.ok and r.state == "unverified")

r = validate_binding("扫码登录 手机号登录", "", "588123808")
check("A4 登录态失效 → 拒绝", not r.ok and "登录" in r.reason)

r = validate_binding("首页 发起新会议 加入会议", "", "588123808")
check("A5 不在会中 → 拒绝", not r.ok and "不在会议中" in r.reason)

r = validate_binding(IN_MEETING, TITLE_FMT.format(no="588123808"), "58812")
check("A6 非 9 位会议号 → 拒绝", not r.ok and "9 位" in r.reason)

body = f"会议号 588 123 808 已复制 {IN_MEETING}"
r = validate_binding(body, "", "588123808")
check("A7 分段显示的会议号不误判为候选", r.ok)

# ---- B. 会议号提取
check("B1 9 位会议号提取", MEETING_NO_RE.findall("会议 588123808 和 111222333") == ["588123808", "111222333"])
check("B2 长数字不误提取", MEETING_NO_RE.findall("订单 1234567890") == [])

# ---- C. WAV 封装
pcm = bytes(range(256)) * 4
wav_path = str(Path(__file__).parent / "_tmp_route_check.wav")
pcm_to_wav(pcm, wav_path, 24000)
with wave.open(wav_path, "rb") as wf:
    check("C1 WAV 头正确", wf.getnchannels() == 1 and wf.getsampwidth() == 2
          and wf.getframerate() == 24000 and wf.getnframes() == len(pcm) // 2)
Path(wav_path).unlink(missing_ok=True)

# ---- D. 称呼枚举（定稿：顿承尧/承尧/顿顿/deli/顿老师，大小写不敏感）
names = ["顿承尧", "承尧", "顿顿", "deli", "顿老师"]
arbiter = TurnArbiter(names)
check("D1 中文名触发", arbiter.decide("会议", "承尧，你怎么看？").action == "speak")
check("D2 英文称呼触发", arbiter.decide("会议", "deli 这个排期没问题吧").action == "speak")
check("D3 大小写不敏感", arbiter.decide("会议", "Deli 你觉得呢").action == "speak")
check("D4 顿老师触发", arbiter.decide("会议", "让顿老师说一下").action == "speak")
check("D5 未点名不听", arbiter.decide("会议", "今天天气不错").action == "listen")

print()
if FAILS:
    print(f"{len(FAILS)} 项失败: {FAILS}")
    sys.exit(1)
print("全部通过")

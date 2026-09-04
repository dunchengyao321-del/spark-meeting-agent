# 飞书会议语音智能体 · 研发实现骨架（方案C：页面层注入）

操作者用浏览器打开飞书会议网页版（vc.feishu.cn）进入会议室；本骨架接管该页面的音频：
**下行**把会议室声音实时传给后端（ASR→LLM），**上行**把后端 TTS 音频注入浏览器，让会议室所有人听到智能体说话。

## 文件清单

| 文件 | 作用 |
| --- | --- |
| `shim.js` | 页面注入脚本：伪造设备列表、拦截 getUserMedia 返回合成麦克风轨、包装 RTCPeerConnection 捕获远端音频轨、AudioWorklet 收发 PCM、WebSocket 断线重连、200ms 抖动缓冲、`window.__agentShim` 探针 |
| `bridge_server.py` | Python WebSocket 服务：下行 meeting_pcm→ASR 回调占位、上行 tts_pcm→浏览器、Float32↔Int16 转换、背压丢帧、ASR/LLM/TTS 抽象基类接口 |
| `run_agent.py` | Playwright 启动器：flags + 权限授权 + 注入 shim + 打开 vc.feishu.cn + 等待人工入会 + 周期打印 shim 状态 |
| `test_loopback.html` | 最小回环测试页：getUserMedia→本地回放+实时电平表，不进真实会议即可验证注入链路 |
| `requirements.txt` | Python 依赖 |

## 安装

```bash
cd 02-研发实现
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium   # 若用本机 Chrome（--channel chrome）可跳过
```

## 三步运行

```bash
# 第 1 步：启动桥接服务器（联调阶段建议带 --tone-test，每 5s 发一声 440Hz 测试音）
python3 bridge_server.py --tone-test

# 第 2 步：启动智能体浏览器（另开一个终端），把 123456789 换成真实 9 位会议号
python3 run_agent.py --meeting-id 123456789
# 或：python3 run_agent.py --url "https://vc.feishu.cn/j/123456789"
# 或：python3 run_agent.py --config agent_config.json   # {"meeting_id": "123456789"}

# 第 3 步：在弹出的浏览器里人工完成登录 → 输入会议号 → 加入会议（可能过等候室）。
# 终端检测到会议控制栏后，会每 5s 打印一次 window.__agentShim 状态。
```

## 不进会议的自测（回环）

```bash
# 终端 A
python3 bridge_server.py --tone-test
# 终端 B：用注入 shim 的浏览器打开回环页（python3 - <<'EOF' 方式或临时脚本）
python3 - <<'EOF'
from playwright.sync_api import sync_playwright
from pathlib import Path
shim = Path("shim.js").read_text(encoding="utf-8")
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=False, args=[
        "--use-fake-ui-for-media-stream",
        "--autoplay-policy=no-user-gesture-required",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
    ])
    ctx = b.new_context(permissions=["microphone", "camera"])
    ctx.add_init_script(shim)
    ctx.new_page().goto(Path("test_loopback.html").resolve().as_uri())
    input("按回车关闭……\n")
EOF
```

在页面点"开始测试"：若轨 label 显示 `Microphone (HD Audio Device)` 之类的虚拟名、
每 5 秒听到一声提示音、电平表随之跳动、`__agentShim` 探针 `state=connected`，则注入链路全部打通。

## 对接 ASR / LLM / TTS

`bridge_server.py` 中实现了三个抽象基类，接入真实服务时各自继承实现：

```python
class MyASR(ASREngine):
    async def feed_pcm(self, pcm_f32, sample_rate): ...   # 流式喂给真实 ASR

class MyLLM(LLMEngine):
    async def chat(self, text): ...

class MyTTS(TTSEngine):
    async def synthesize(self, text):                     # 异步生成器，产出 Float32 PCM 块
        yield chunk

server = BridgeServer(asr=MyASR(), tts=MyTTS())
server.on_downlink(my_custom_callback)                    # 也可注册额外下行回调
await server.speak_text("大家好，我是会议助手")            # 上行入口
```

## 已知坑排查表

| 症状 | 原因 | 对策 |
| --- | --- | --- |
| 页面弹出麦克风授权框卡住 | 缺 `--use-fake-ui-for-media-stream` 或 context 未授权 | 确认 5 个 flags 齐全 + `new_context(permissions=["microphone","camera"])` |
| AudioContext 一直 suspended / 无声音 | 自动播放策略拦截 | 必须带 `--autoplay-policy=no-user-gesture-required`；代码里也已 `ctx.resume()` 兜底 |
| 浏览器最小化/切后台后音频断流、20ms 定时器变慢 | Chrome 后台节流 | 必须带 `--disable-background-timer-throttling --disable-renderer-backgrounding --disable-backgrounding-occluded-windows` 三件套 |
| 入会页设备列表为空或报 NotFoundError | 页面先调 enumerateDevices 检测权限 | shim 已伪造含 label 的设备列表（如 `Microphone (HD Audio Device)`），无需真实授权 |
| 会议室里听到智能体自己的声音（自听反馈/回声） | 下行把远端声又播回本地扬声器 | shim 的采集链经 **0 增益 GainNode** 挂图，本地不外放；回环测试页请戴耳机 |
| 智能体声音忽快忽慢/卡顿 | 网络抖动、无缓冲直接播 | shim 已内置 ~200ms 抖动缓冲；观察探针 `uplinkUnderruns`（欠载）与 `uplinkDiscards`（溢出丢弃） |
| 一直停在等候室 | 主持人未放行 | 属正常人工流程，run_agent 会持续轮询直到出现会议控制栏（`--join-timeout` 可调） |
| `playwright launch channel=chrome` 报错 | 本机未装 Chrome | 改用 `--channel chromium` 或先 `playwright install chromium` |
| `window.__agentShim` 为 null | init script 未生效（页面先于注入加载） | add_init_script 必须在 `page.goto` 之前注册；run_agent.py 已保证顺序 |
| 上行队列持续堆积、延迟越来越大 | TTS 产出快于播放或浏览器卡顿 | bridge_server 队列满时自动丢最旧帧保实时性；检查后端产出节奏是否按 20ms/帧 |

## 协议速查

- 单条 WebSocket：`ws://127.0.0.1:8765/ws`，二进制帧第 1 字节为通道号（`0x01` 下行 / `0x02` 上行），其后为 Int16 小端 PCM，48kHz 单声道，20ms/帧（960 采样）；文本帧为 JSON 控制消息（hello/ping/pong）。

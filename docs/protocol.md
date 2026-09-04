# WebSocket 协议 · /ws/meeting

浏览器控制台与本地服务端之间的全部实时通信走一个 WebSocket。

## 连接

```
ws://127.0.0.1:8765/ws/meeting?engine=pipeline|realtime
```

`engine` 缺省读 `config.json` 的 `meeting_engine`。

## 客户端 → 服务端

| 帧 | 内容 |
|----|------|
| 二进制 | `[声道 1 字节][PCM16 单声道 @16kHz]`；声道 `0`=麦克风，`1`=会议声音 |
| 文本 | `{"type":"session.stop"}` 结束会话 |
| 文本 | `{"type":"inject.say","text":"..."}` 让智能体直接说/回答 |
| 文本 | `{"type":"agent.force"}` 下一位参会者说完后智能体主动接话 |

## 服务端 → 客户端

| 帧 | 内容 |
|----|------|
| 二进制 | 智能体语音：PCM16 单声道 @24kHz |
| 文本 | 见下表 |

| 事件 | 字段 | 说明 |
|------|------|------|
| `session.state` | engine, status(connecting/connected/stopped), triggers?, llm?, llm_model? | 会话状态 |
| `agent.state` | state(listening/thinking/speaking), reason? | 智能体状态灯 |
| `transcript.partial` | speaker, text, partial_asr? | 流式字幕：智能体为增量追加；带 `partial_asr` 的为边说边转草稿（整段替换语义） |
| `transcript.final` | speaker, text | 定稿字幕；speaker=我/会议/agent/console |
| `clear_audio` | – | 打断：立即清空播放队列 |
| `metrics.turn` | asr_ms, retrieval_ms, llm_ttft_ms, tts_ttfa_ms, total_ms / voice_to_voice_ms | 单轮延迟 |
| `kb.prefetch` | query, hits, warm?, early? | 预判式上下文预热；`early`=草稿阶段提前命中 |
| `mcp.call` / `mcp.result` | name, arguments / output | MCP 工具调用 |
| `session.error` | error | 错误 |

## REST

| 路由 | 说明 |
|------|------|
| `GET/POST /api/config` | 读写配置（密钥不回显，留空保持不变） |
| `GET /api/status` | 引擎/密钥/知识库/MCP 状态 |
| `POST /api/kb/ingest` | 重新摄入 `docs/kb/` |
| `GET /api/kb/search?q=&k=` | 检索测试 |
| `POST /api/asr/apple_auth` | 触发/查询 macOS 本地语音识别授权 |
| `POST /api/asr/bench` | 解码 `voice_samples/` 样本实测 ASR 延迟/识别文本 |
| `POST /api/mcp/start` | 连接 config.json 中声明的 MCP 服务器 |
| `POST /api/mcp/call` | `{"name":"服务器__工具","arguments":{}}` |

"""context_agent.py —— B 智能体：会议上下文 + 知识库 + MCP 深度问答服务

A/B 双智能体架构中的 B（理解层）：
  A 智能体（浏览器链路）负责实时交互——听唤醒词、抓问题意图、TTS 发声；
  B 智能体（本服务）负责深度理解——拿到 A 转发的问题后，融合以下来源生成精准回答：
    1) 会议长上下文：transcripts/*.jsonl 最近 100 条（A 只有 5 条短窗，B 记得整场会）
    2) 知识库检索：docs/kb 目录（复用 server.rag.store）
    3) MCP 工具：代码库搜索、文件读取等（复用 server.mcp.host）
    4) 飞书妙记（可选）：会后级准确转写导入后作为补充上下文

接口（默认 http://127.0.0.1:8766）：
  POST /ask    {"question": "...", "speaker": "会议"} -> {"answer": "...", ...}
  GET  /health -> 服务状态
  GET  /context?lines=20 -> 最近会议上下文（调试用）

运行：python3 context_agent.py [--port 8766]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

import sys
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from server.config_store import load_config  # noqa: E402
from server.llm import build_llm  # noqa: E402
from server.mcp.host import MCPManager  # noqa: E402
from server.rag.store import KnowledgeStore  # noqa: E402

TRANSCRIPT_DIR = ROOT / "02-研发实现" / "transcripts"
# 外部高保真转写目录：把飞书妙记/录音 App 导出的 txt/md 转写文件放进此目录，
# B 每次回答前自动读取最新修改的一份并入上下文（准确率最高的会议内容来源）。
EXTERNAL_TRANSCRIPT_DIR = ROOT / "external_transcripts"
EXTERNAL_MAX_CHARS = 8000
EXTERNAL_MAX_AGE_SEC = 2 * 3600  # 外部转写超过 2 小时未更新 = 旧会议残留，弃用（杜绝跨会议混淆）
CONTEXT_LINES = 500          # B 的会议长记忆窗口（A 只有 5 条）；整场会全量记忆
KB_TOP_K = 4

# 问题里出现这些词时，B 主动调 MCP 代码库工具补充事实
_MCP_HINTS = ("代码", "函数", "接口", "文件", "类名", "方法", "参数", "配置项",
              "shim", "pipeline", "bridge", "adapter", "知识库里", "查一下",
              "kb", "文档", "repo", "仓库", "实现")

# 触发"看投屏"意图的关键词：命中且 60s 内有截帧时，LLM 请求附带会议画面截图
_SCREEN_HINTS = ("投屏", "屏幕", "共享", "ppt", "PPT", "演示", "图表", "画面",
                 "这张图", "这个图", "截图", "显示", "表格", "文档里", "页面上",
                 "看到", "看一下", "看下", "写着", "内容是什么")
SCREEN_FRAME_FILE = ROOT / "02-研发实现" / "latest_screen.jpg"
SCREEN_FRESH_S = 60


def _wants_screen(question: str) -> bool:
    return any(h in question for h in _SCREEN_HINTS)


def _latest_screen_b64() -> str:
    """读取最新投屏截帧（adapter 落盘），过旧返回空。"""
    try:
        if not SCREEN_FRAME_FILE.exists():
            return ""
        if time.time() - SCREEN_FRAME_FILE.stat().st_mtime > SCREEN_FRESH_S:
            return ""
        import base64
        return base64.b64encode(SCREEN_FRAME_FILE.read_bytes()).decode("ascii")
    except OSError:
        return ""

# 答案压缩：B 返回给 A 播报的文本上限（TTS 念太长会议体验差）
ANSWER_MAX_CHARS = 400

config = load_config()
kb_store = KnowledgeStore(ROOT, config.get("kb_dir", "docs/kb"))
mcp_manager = MCPManager(config)

app = FastAPI(title="Spark Context Agent (B)", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


class AskBody(BaseModel):
    question: str
    speaker: str = "会议"


class MinutesBody(BaseModel):
    paragraphs: list[dict] = []   # [{speaker, text}]，妙记网页版全量段落
    source: str = "minutes-web"


def _current_meeting_id() -> str:
    """当前会议号（agent_config.json），用于多会议记录隔离。"""
    try:
        cfg = json.loads((ROOT / "02-研发实现" / "agent_config.json")
                         .read_text(encoding="utf-8"))
        return str(cfg.get("meeting_id", ""))
    except Exception:  # noqa: BLE001
        return ""


def _today_transcripts(lines: int = CONTEXT_LINES) -> list[dict]:
    """读取当前会议的记录（按「日期-会议号」分文件存储，天然隔离）。

    只读 meeting-*-<当前会议号>.jsonl：每场会议单独留存，
    绝不混入其他会议的内容影响上下文。
    """
    if not TRANSCRIPT_DIR.exists():
        return []
    current = _current_meeting_id()
    if not current:
        return []
    files = sorted(TRANSCRIPT_DIR.glob(f"meeting-*-{current}.jsonl"),
                   key=lambda f: f.stat().st_mtime, reverse=True)
    if not files:
        return []
    records: list[dict] = []
    for line in files[0].read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("text"):
            records.append(r)
    return records[-lines:]


def _format_context(records: list[dict]) -> str:
    if not records:
        return ""
    lines = [f"[{r.get('ts', '')[11:19]}] {r.get('speaker', '?')}：{r.get('text', '')}"
             for r in records]
    return "以下是本场会议到目前为止的完整对话记录（含时间戳，按时间顺序）：\n" + "\n".join(lines)


def _external_transcript() -> str:
    """读取外部高保真转写（飞书妙记/录音 App 导出的最新一份 txt/md）。"""
    if not EXTERNAL_TRANSCRIPT_DIR.exists():
        return ""
    files = sorted(
        [f for f in EXTERNAL_TRANSCRIPT_DIR.iterdir()
         if f.suffix.lower() in (".txt", ".md") and f.is_file()],
        key=lambda f: f.stat().st_mtime, reverse=True)
    if not files:
        return ""
    latest = files[0]
    try:
        if time.time() - latest.stat().st_mtime > EXTERNAL_MAX_AGE_SEC:
            return ""  # 超过时效未更新：是上一场会议的残留，绝不混入本场上下文
        text = latest.read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return ""
    if not text:
        return ""
    if len(text) > EXTERNAL_MAX_CHARS:
        text = text[-EXTERNAL_MAX_CHARS:]  # 保留最近的部分（会议后半段更相关）
    return (f"以下是飞书妙记/录音转写的高保真会议内容（来源文件 {latest.name}，"
            f"识别准确率最高，与上面记录冲突时以此为准）：\n{text}")


def _wants_mcp(question: str) -> bool:
    q = question.lower()
    return any(h in q for h in _MCP_HINTS)


async def _rewrite_queries(llm, question: str) -> list[str]:
    """LLM 查询改写：把口语问题改写成 2-3 个实体词检索查询。

    分析型问题（"切换有什么影响"）的查询词面与答案块（"库存凭证不管联营库存"）
    零重合，关键词检索先天召不回；改写出的实体查询（"联营 库存 管理规则"）能命中。
    失败/超时回退为原问题，绝不阻断主链路。
    """
    prompt = (
        "把下面的会议问题改写成 2 到 3 个知识库检索查询，用于关键词搜索。"
        "要求：每个查询只含实体名词和领域术语（不超过 8 个字一个查询），覆盖问题的不同方面；"
        "不要输出解释、编号或标点，每行一个查询。\n"
        f"问题：{question}"
    )
    parts: list[str] = []
    try:
        async def _collect():
            async for item in llm.stream_chat([{"role": "user", "content": prompt}]):
                if item["type"] == "delta":
                    parts.append(item["text"])
        await asyncio.wait_for(_collect(), timeout=4.0)
    except Exception:  # noqa: BLE001
        return [question]
    queries: list[str] = []
    for line in "".join(parts).splitlines():
        q = line.strip().strip("0123456789.、-—* \t")
        if 2 <= len(q) <= 30 and q != question:
            queries.append(q)
    print(f"[B智能体] 查询改写: {queries}", flush=True)
    return queries[:3] or [question]


# 自然语言问题 → 搜索关键词：剥掉礼貌/动词性停用片段，保留实体词。
# 整句直接当子串搜代码库必然零命中（"帮我查一下噪声闸门的实现"≠代码行）。
_STOP_PHRASES = ("帮我", "帮忙", "请", "请问", "一下", "我想", "我要", "你能", "你可以",
                 "吗", "呢", "吧", "啊", "呀", "嘛", "么", "了", "的")


def _extract_keywords(question: str) -> str:
    t = question
    for p in _STOP_PHRASES:
        t = t.replace(p, " ")
    segs = [s for s in re.split(r"[，。！？、,.!?;；:：\s\"'（）()【】\[\]]+", t)
            if len(s) >= 2]
    words: list[str] = []
    for s in segs:
        parts = re.split(r"(在|有|是|和|跟|与|怎么|什么|哪里|哪些|哪个|如何|怎样|多少"
                         r"|为什么|是不是|有没有|能不能)", s)
        words.extend(p for p in parts if len(p) >= 2
                     and not re.fullmatch(r"[你我他她它]+", p))
    seen: set[str] = set()
    out: list[str] = []
    for w in words:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return " ".join(out[:8])


async def _mcp_lookup(question: str) -> str:
    """按问题意图调 MCP 工具补事实。失败静默返回空。"""
    if not mcp_manager or not _wants_mcp(question):
        return ""
    blocks: list[str] = []
    # MCPManager.call 要求「服务器名__工具名」限定名；先建 工具名 -> 服务器名 映射。
    # （connections 是 list 而非 dict、裸工具名调用，是此前 MCP 静默失效的两个根因）
    owner: dict[str, str] = {}
    try:
        for srv in getattr(mcp_manager, "connections", []) or []:
            for t in getattr(srv, "tools", []) or []:
                owner[t.get("name", "")] = srv.name
    except Exception:  # noqa: BLE001
        owner = {}
    keywords = _extract_keywords(question) or question[:40]
    print(f"[B智能体] MCP 查询: 关键词={keywords!r} 可用工具={sorted(k for k in owner if k in ('code_search','kb_search','read_file'))}", flush=True)
    try:
        if "code_search" in owner:
            out = await mcp_manager.call(f"{owner['code_search']}__code_search",
                                         {"q": keywords, "limit": 8})
            print(f"[B智能体] code_search 返回 {len(str(out))} 字符: {str(out)[:80]!r}", flush=True)
            if out and "无命中" not in str(out):
                blocks.append("代码库搜索结果：\n" + str(out)[:1200])
        if "kb_search" in owner:
            out = await mcp_manager.call(f"{owner['kb_search']}__kb_search",
                                         {"q": question[:60], "k": 4})
            print(f"[B智能体] kb_search 返回 {len(str(out))} 字符: {str(out)[:80]!r}", flush=True)
            if out and "无命中" not in str(out):
                blocks.append("项目知识库搜索结果：\n" + str(out)[:1200])
    except Exception as exc:  # noqa: BLE001
        print(f"[B智能体] MCP 调用异常: {type(exc).__name__}: {exc}", flush=True)
    return "\n\n".join(blocks)


@app.on_event("startup")
async def _startup() -> None:
    kb_store.ensure_loaded()
    try:
        await mcp_manager.start_all()
    except Exception:  # noqa: BLE001
        pass
    print(f"[B智能体] 知识库: {kb_store.stats()}", flush=True)


@app.on_event("shutdown")
async def _shutdown() -> None:
    try:
        await mcp_manager.stop_all()
    except Exception:  # noqa: BLE001
        pass


@app.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        "kb": kb_store.stats(),
        "mcp": mcp_manager.status(),
        "transcript_dir": str(TRANSCRIPT_DIR),
    }


@app.post("/kb/reload")
async def kb_reload() -> dict:
    """知识库热重建：监控台批量导入文件后调用，无需重启 B。"""
    cfg = load_config()
    kb_dir = str(cfg.get("kb_dir", "kb"))
    kb_store.kb_dir = (ROOT / kb_dir) if not Path(kb_dir).is_absolute() else Path(kb_dir)
    result = await asyncio.to_thread(kb_store.ingest)
    return {"ok": True, **result}


@app.get("/context")
async def context(lines: int = 20) -> dict:
    records = _today_transcripts(lines)
    return {"count": len(records), "records": records}


@app.get("/pending_questions")
async def pending_questions() -> dict:
    """待本人回答的问题列表（监控页拉取）。"""
    fp = EXTERNAL_TRANSCRIPT_DIR / "pending_questions.jsonl"
    items: list[dict] = []
    if fp.exists():
        for line in fp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return {"count": len(items), "items": list(reversed(items))}


@app.delete("/pending_questions")
async def clear_pending_questions() -> dict:
    fp = EXTERNAL_TRANSCRIPT_DIR / "pending_questions.jsonl"
    if fp.exists():
        fp.unlink()
    return {"ok": True}


MINUTES_LIVE_FILE = EXTERNAL_TRANSCRIPT_DIR / "minutes_live.txt"


@app.post("/minutes_ingest")
async def minutes_ingest(body: MinutesBody) -> dict:
    """接收妙记网页版采集脚本推送的全量转写段落，覆盖写 minutes_live.txt。

    采集端每 2.5s 扫描一次 DOM，有变化才推；段落会随转写修正变化，
    所以每次全量覆盖（而不是追加），B 读到的永远是最新定稿。
    """
    if not body.paragraphs:
        return {"ok": False, "error": "empty paragraphs"}
    EXTERNAL_TRANSCRIPT_DIR.mkdir(exist_ok=True)
    lines = []
    for p in body.paragraphs:
        speaker = str(p.get("speaker", "?")).strip() or "?"
        text = str(p.get("text", "")).strip()
        if text:
            lines.append(f"{speaker}：{text}")
    content = "\n".join(lines)
    tmp = MINUTES_LIVE_FILE.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(MINUTES_LIVE_FILE)   # 原子覆盖，避免 B 读到半截
    return {"ok": True, "paragraphs": len(lines), "chars": len(content)}


@app.post("/ask")
async def ask(body: AskBody) -> dict:
    t0 = time.time()
    question = body.question.strip()
    if not question:
        return {"ok": False, "error": "empty question"}

    records = await asyncio.to_thread(_today_transcripts, CONTEXT_LINES)
    meeting_block = _format_context(records)
    external_block = await asyncio.to_thread(_external_transcript)

    # 多路召回：LLM 改写出的实体查询（每路 top10）+ 关键词版 + 原句版，合并去重。
    cfg0 = load_config()
    rewrite_llm = build_llm(cfg0)
    queries = await _rewrite_queries(rewrite_llm, question)
    kw = _extract_keywords(question)
    all_queries = queries + [kw or question, question]
    hits_all: list[dict] = []
    for q in all_queries:
        hits_all.extend(await asyncio.to_thread(kb_store.search, q, 10))
    # 召回收敛：同一文档只取 1 条、总共最多 4 条——片段越多 LLM 越容易把
    # 不相关内容揉进回答（"知识库查询混乱"的直接来源），少而准优先。
    # 相邻块扩展：关键定义/表格常被切在命中块的相邻块，一并捎给 LLM。
    per_doc: dict[str, int] = {}
    seen_id: set[int] = set()
    picked: list[dict] = []
    for h in hits_all:
        cid = int(h.get("id", -1))
        src = h.get("source", "")
        if cid in seen_id or per_doc.get(src, 0) >= 1:
            continue
        seen_id.add(cid)
        per_doc[src] = per_doc.get(src, 0) + 1
        picked.append(h)
        if len(picked) >= 4:
            break
    chunks = getattr(kb_store, "chunks", []) or []
    kb_hits: list[dict] = []
    for h in picked:
        kb_hits.append(h)
        cid = int(h.get("id", -1))
        for nid in (cid - 1, cid + 1):
            if 0 <= nid < len(chunks) and nid not in seen_id \
                    and chunks[nid].get("source") == h.get("source"):
                seen_id.add(nid)
                kb_hits.append(chunks[nid])
    kb_block = ""
    if kb_hits:
        kb_block = ("知识库相关片段：\n" + "\n".join(
            f"- ({h['source']}) {h['text']}" for h in kb_hits))

    mcp_block = await _mcp_lookup(question)

    cfg = load_config()
    llm = build_llm(cfg)
    persona = str(cfg.get("persona", "")).strip()
    base_prompt = str(cfg.get("local_voice_system_prompt", "")).strip()
    wake_names = cfg.get("meeting_wake_names") or ["星火"]
    system = "\n\n".join(part for part in [
        # 人格主体：页面可维护的分身人格（local_voice_system_prompt），B 只补充协议层指令
        base_prompt or "你是用户的会议分身，代替用户参加会议，被点名时简洁回答问题。",
        f"你的名字是「{'、'.join(wake_names)}」——参会者用其中任何一个称呼你，都是在叫你，坦然认领。",
        "你当前掌握的信息：本场会议的完整对话记录（下方）"
        + ("、飞书妙记高保真实时转写（下方，与记录冲突时以妙记为准）" if external_block else "")
        + ("、会议投屏画面（问题涉及画面时会附带截图）" if _wants_screen(question) else "")
        + "。只依据这些信息回答，信息里没有的就直说不知道。",
        "【需本人才回答的问题】判断标准：需要用户本人解释、承诺、拍板的事"
        "（价格/排期/人事/对外承诺/只有本人知道的情况）。命中时你必须："
        "回答的第一行单独写「[需本人]」，然后用一两句话告诉提问者已记录会转达。"
        "除此之外的回答绝不要出现「[需本人]」标记。",
        persona and f"补充人设：{persona}",
        external_block,
        meeting_block,
        kb_block,
        mcp_block,
        f"规则：1) 结合会议记录理解「{body.speaker}」的问题，指代（这个/那个/刚才说的）必须能从记录中还原；"
        "2) 答案适合语音播报：口语化、先结论、一到三句话、不要列表符号和引用编号。",
    ] if part)

    messages = [{"role": "system", "content": system},
                {"role": "user", "content": question}]
    # 投屏视觉：问题涉及画面且 60s 内有截帧时，附图走多模态（OpenAI 视觉格式）
    screen_b64 = ""
    if _wants_screen(question):
        screen_b64 = await asyncio.to_thread(_latest_screen_b64)
        if screen_b64:
            messages[-1] = {"role": "user", "content": [
                {"type": "text",
                 "text": question + "\n（附图：当前会议投屏画面，请结合画面内容回答）"},
                {"type": "image_url",
                 "image_url": {"url": "data:image/jpeg;base64," + screen_b64}},
            ]}
            print(f"[B智能体] 附投屏截图: {len(screen_b64) // 1024}KB base64", flush=True)
    parts: list[str] = []
    try:
        async for item in llm.stream_chat(messages):
            if item["type"] == "delta":
                parts.append(item["text"])
            elif item["type"] == "error":
                return {"ok": False, "error": item["error"][:200]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}

    answer = "".join(parts).strip()
    answer = re.sub(r"[\[【]\s*\d+\s*[\]】]", "", answer)  # 剥引用编号

    # 待决问题落盘：回答以「[需本人]」开头时，记录到 pending_questions.jsonl
    # （用户在监控页/会后查看哪些问题需要本人解释或拍板）
    need_owner = False
    if answer.startswith("[需本人]"):
        need_owner = True
        answer = answer[len("[需本人]"):].strip()
        try:
            EXTERNAL_TRANSCRIPT_DIR.mkdir(exist_ok=True)
            with open(EXTERNAL_TRANSCRIPT_DIR / "pending_questions.jsonl", "a",
                      encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "speaker": body.speaker, "question": question,
                    "reply": answer[:120],
                }, ensure_ascii=False) + "\n")
        except OSError:
            pass
        print(f"[B智能体] 待决问题已记录: {question[:50]}", flush=True)

    if len(answer) > ANSWER_MAX_CHARS:
        answer = answer[:ANSWER_MAX_CHARS].rsplit("。", 1)[0] + "。"
    return {
        "ok": True,
        "answer": answer,
        "need_owner": need_owner,
        "context_lines": len(records),
        "kb_hits": len(kb_hits),
        "mcp_used": bool(mcp_block),
        "latency_ms": int((time.time() - t0) * 1000),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="B 智能体：会议上下文 + 知识库 + MCP 问答服务")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    print(f"B 智能体（理解中枢）：http://{args.host}:{args.port}/health", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()

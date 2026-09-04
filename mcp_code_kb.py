#!/usr/bin/env python3
"""spark-code-kb —— 星火代码/知识库 MCP 服务（stdio JSON-RPC，零依赖）

让会议中的智能体可以通过 MCP 工具主动查询：
  kb_search(q, k)        检索星火独立知识库（走星火控制台 /api/kb/search）
  code_search(q, limit)  在 server/ 与 02-研发实现/ 全文搜索（文件名 + 行号）
  read_file(path, max_chars)  读取仓库内文件内容（防目录穿越）

协议与 server/mcp/host.py 对齐：initialize / tools/list / tools/call，
传输为换行分隔的 JSON-RPC 2.0（stdio）。
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
KB_API = "http://127.0.0.1:8765/api/kb/search"
CODE_DIRS = ["server", "02-研发实现"]
SKIP_DIRS = {"node_modules", "__pycache__", ".agent-profile", "transcripts", ".git"}
TEXT_EXT = {".py", ".js", ".md", ".txt", ".json", ".html", ".sh", ".css"}

TOOLS = [
    {
        "name": "kb_search",
        "description": "检索星火项目独立知识库（PRD、联调记录、使用说明、架构文档等），按相关度返回片段。当用户问到项目设计、历史问题、使用方式时优先调用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "检索问题"},
                "k": {"type": "integer", "description": "返回条数，默认 4", "default": 4},
            },
            "required": ["q"],
        },
    },
    {
        "name": "code_search",
        "description": "在星火代码仓库（server/ 与 02-研发实现/）中全文搜索，返回 文件:行号: 内容。当用户问到具体实现、函数、报错位置时调用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "搜索关键词"},
                "limit": {"type": "integer", "description": "最大返回行数，默认 20", "default": 20},
            },
            "required": ["q"],
        },
    },
    {
        "name": "read_file",
        "description": "读取星火代码仓库中指定文件的内容（相对仓库根的路径，如 02-研发实现/shim.js）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对路径"},
                "max_chars": {"type": "integer", "description": "最大字符数，默认 4000", "default": 4000},
            },
            "required": ["path"],
        },
    },
]


def kb_search(args: dict) -> str:
    qs = urllib.parse.urlencode({"q": str(args.get("q", ""))[:200], "k": int(args.get("k", 4))})
    with urllib.request.urlopen(f"{KB_API}?{qs}", timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    hits = data.get("hits", [])
    if not hits:
        return "（知识库无命中）"
    return "\n\n".join(
        f"[{i + 1}] {h.get('source', '')} / {h.get('heading', '')}\n{str(h.get('text', ''))[:400]}"
        for i, h in enumerate(hits)
    )


def code_search(args: dict) -> str:
    q = str(args.get("q", ""))[:200].lower()
    limit = max(1, int(args.get("limit", 20)))
    # 多词模式：空格/标点分词，任一词命中即收录，按命中词数打分排序。
    # 自然语言问句（"帮我查一下噪声闸门的实现"）不再要求整句子串命中。
    terms = [t for t in re.split(r"[\s，。！？、,.!?;；:：\"'()（）【】\[\]]+", q)
             if len(t) >= 2]
    if not terms:
        terms = [q] if q else []
    # 长词 N-gram 扩展：中文无空格分词，"代码库里噪声闸门"整词必然零命中，
    # 展开 4/3/2 字滑窗后"噪声闸门"等真实代码词才能命中。
    expanded: set[str] = set()
    for t in terms:
        expanded.add(t)
        if len(t) > 4:
            for n in (4, 3, 2):
                for i in range(len(t) - n + 1):
                    expanded.add(t[i:i + n])
    scored: list[tuple[tuple[int, int], str]] = []
    for d in CODE_DIRS:
        for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, d)):
            dirnames[:] = [x for x in dirnames if not x.startswith(".") and x not in SKIP_DIRS]
            for fn in filenames:
                if os.path.splitext(fn)[1].lower() not in TEXT_EXT:
                    continue
                fp = os.path.join(dirpath, fn)
                rel = os.path.relpath(fp, ROOT)
                try:
                    with open(fp, encoding="utf-8", errors="ignore") as f:
                        for ln, line in enumerate(f, 1):
                            low = line.lower()
                            matched = [t for t in expanded if t in low]
                            if matched:
                                # 评分：最长命中词长度优先（"噪声闸门"4字 > "实现"2字），
                                # 次看命中词数量——最相关的行自然顶到最前。
                                best = max(len(t) for t in matched)
                                scored.append(((best, len(matched)),
                                               f"{rel}:{ln}: {line.strip()[:160]}"))
                except OSError:
                    continue
    if not scored:
        return "（代码库无命中）"
    scored.sort(key=lambda x: (-x[0][0], -x[0][1]))
    return "\n".join(line for _, line in scored[:limit])


def read_file(args: dict) -> str:
    rel = str(args.get("path", ""))[:300].lstrip("/")
    max_chars = max(200, int(args.get("max_chars", 4000)))
    fp = os.path.realpath(os.path.join(ROOT, rel))
    if not fp.startswith(ROOT) or not os.path.isfile(fp):
        return f"文件不存在或路径越界: {rel}"
    with open(fp, encoding="utf-8", errors="ignore") as f:
        return f.read(max_chars)


HANDLERS = {"kb_search": kb_search, "code_search": code_search, "read_file": read_file}


def reply(msg_id, result) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result},
                                ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        msg_id = msg.get("id")
        if method == "initialize":
            reply(msg_id, {"protocolVersion": "2024-11-05",
                           "capabilities": {"tools": {}},
                           "serverInfo": {"name": "spark-code-kb", "version": "0.1.0"}})
        elif method == "tools/list":
            reply(msg_id, {"tools": TOOLS})
        elif method == "tools/call" and msg_id is not None:
            params = msg.get("params", {})
            name = params.get("name", "")
            arguments = params.get("arguments") or {}
            fn = HANDLERS.get(name)
            try:
                text = fn(arguments) if fn else f"未知工具: {name}"
            except Exception as exc:  # noqa: BLE001
                text = f"工具执行失败: {type(exc).__name__}: {exc}"
            reply(msg_id, {"content": [{"type": "text", "text": text}]})
        # notifications/* 无需应答


if __name__ == "__main__":
    main()

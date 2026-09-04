#!/usr/bin/env python3
"""Mock stdio MCP server used by tests/test_mcp_host.py.

Speaks line-delimited JSON-RPC: initialize / notifications/initialized /
tools/list / tools/call. Exposes one tool (`get_sales`) so the full path
config -> MCPManager -> tool discovery -> call_tool can be asserted.
"""

import json
import sys

TOOLS = [{
    "name": "get_sales",
    "description": "查询指定产品线的季度销售额",
    "inputSchema": {
        "type": "object",
        "properties": {"product": {"type": "string", "description": "产品线名称"}},
        "required": ["product"],
    },
}]


def reply(msg_id, result):
    print(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result},
                     ensure_ascii=False), flush=True)


def reply_error(msg_id, code, message):
    print(json.dumps({"jsonrpc": "2.0", "id": msg_id,
                      "error": {"code": code, "message": message}},
                     ensure_ascii=False), flush=True)


def main():
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
            reply(msg_id, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mock-mcp", "version": "0.1"},
            })
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            reply(msg_id, {"tools": TOOLS})
        elif method == "tools/call":
            params = msg.get("params") or {}
            product = str((params.get("arguments") or {}).get("product", "")).strip()
            if not product:
                reply_error(msg_id, -32602, "缺少 product 参数")
            else:
                reply(msg_id, {"content": [
                    {"type": "text", "text": f"{product} 本季度销售额 120 万，同比增长 8%。"}]})
        elif msg_id is not None:
            reply_error(msg_id, -32601, f"method not found: {method}")


if __name__ == "__main__":
    main()

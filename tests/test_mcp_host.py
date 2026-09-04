"""Integration tests for the MCP host (no external network).

stdio path: real subprocess running tests/mock_mcp_server.py.
HTTP path:  framing/parsing/session logic against a fake transport.
"""

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.mcp.host import MCPConnection, MCPHttpConnection, MCPManager  # noqa: E402

PYTHON = sys.executable
MOCK_SERVER = str(Path(__file__).resolve().with_name("mock_mcp_server.py"))

PASS = []


def ok(name):
    PASS.append(name)
    print(f"[PASS] {name}")


async def test_stdio_lifecycle():
    manager = MCPManager({"mcp_servers": [{
        "name": "mock", "transport": "stdio",
        "command": PYTHON, "args": [MOCK_SERVER],
    }]})
    await manager.start_all()
    status = manager.status()
    assert status[0]["status"] == "connected", status
    assert status[0]["tools"][0]["name"] == "get_sales"
    tools = manager.as_llm_tools()
    assert tools[0]["function"]["name"] == "mock__get_sales"
    assert "销售额" in tools[0]["function"]["description"]
    output = await manager.call("mock__get_sales", {"product": "星火"})
    assert output == "星火 本季度销售额 120 万，同比增长 8%。"
    await manager.stop_all()
    ok("M1 stdio 连接/发现/调用/关闭")


async def test_stdio_bad_command():
    conn = MCPConnection({"name": "bad", "transport": "stdio",
                          "command": "/nonexistent/bin/x"})
    await conn.start()
    assert conn.status == "error"
    ok("M2 stdio 命令不存在→error 状态")


async def test_manager_routing():
    manager = MCPManager({"mcp_servers": [{
        "name": "mock", "transport": "stdio",
        "command": PYTHON, "args": [MOCK_SERVER],
    }]})
    await manager.start_all()
    try:
        await manager.call("missing__tool", {})
        raise AssertionError("应当抛错")
    except RuntimeError as exc:
        assert "不可用" in str(exc)
    await manager.stop_all()
    ok("M3 未注册工具调用报错")


def _sse(payload: dict) -> bytes:
    return ("event: message\ndata: " + json.dumps(payload, ensure_ascii=False)
            + "\n\n").encode("utf-8")


async def test_http_framing():
    conn = MCPHttpConnection({"name": "kb", "transport": "http",
                              "url": "http://mcp.example/mcp"})
    seen: list[dict] = []

    async def fake_post(payload: dict):
        seen.append(payload)
        method = payload.get("method")
        msg_id = payload.get("id")
        if method == "initialize":
            conn._capture_session({"Mcp-Session-Id": "sess-1"})
            body = json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "serverInfo": {"name": "fake", "version": "0"}}}).encode()
            return 200, body
        if method == "notifications/initialized":
            return 202, b""
        if method == "tools/list":
            return 200, _sse({"jsonrpc": "2.0", "id": msg_id, "result": {
                "tools": [{"name": "search_kb", "description": "搜知识库",
                           "inputSchema": {"type": "object"}}]}})
        if method == "tools/call":
            return 200, _sse({"jsonrpc": "2.0", "id": msg_id, "result": {
                "content": [{"type": "text", "text": "命中 3 条"}]}})
        return 200, b"{}"

    conn._post = fake_post
    await conn.start()
    assert conn.status == "connected", conn.error
    assert conn._session_id == "sess-1"
    assert [t["name"] for t in conn.tools] == ["search_kb"]
    output = await conn.call_tool("search_kb", {"q": "报销"})
    assert output == "命中 3 条"
    assert seen[1]["method"] == "notifications/initialized"
    ok("M4 HTTP 连接/SSE 解析/会话头/调用")


async def test_http_error_surface():
    conn = MCPHttpConnection({"name": "kb", "transport": "http",
                              "url": "http://mcp.example/mcp"})

    async def fake_post(payload: dict):
        return 200, json.dumps({"jsonrpc": "2.0", "id": payload.get("id"),
                                "error": {"code": -32601,
                                          "message": "not found"}}).encode()

    conn._post = fake_post
    await conn.start()
    assert conn.status == "error"
    assert "not found" in conn.error
    ok("M5 HTTP JSON-RPC 错误→error 状态")


def test_http_parse_body():
    plain = MCPHttpConnection._parse_body(b'{"jsonrpc":"2.0","id":1,"result":{}}')
    assert plain and plain[0]["id"] == 1
    sse = MCPHttpConnection._parse_body(_sse({"jsonrpc": "2.0", "id": 2, "result": {}}))
    assert sse and sse[0]["id"] == 2
    conn = MCPHttpConnection({"name": "x", "url": "http://a"})
    conn._capture_session({"mcp-session-id": "s2"})
    assert conn._session_id == "s2"
    ok("M6 HTTP 响应解析/会话头捕获")


def test_manager_transport_dispatch():
    manager = MCPManager({"mcp_servers": [
        {"name": "a", "transport": "stdio", "command": "true"},
        {"name": "b", "transport": "http", "url": "http://x"},
        {"name": "c", "transport": "streamable-http", "url": "http://y"},
    ]})
    kinds = [type(c).__name__ for c in manager.connections]
    assert kinds == ["MCPConnection", "MCPHttpConnection", "MCPHttpConnection"]
    ok("M7 按 transport 分发连接类型")


async def main():
    await test_stdio_lifecycle()
    await test_stdio_bad_command()
    await test_manager_routing()
    await test_http_framing()
    await test_http_error_surface()
    test_http_parse_body()
    test_manager_transport_dispatch()
    print(f"mcp host: ALL PASS ({len(PASS)})")


if __name__ == "__main__":
    asyncio.run(main())

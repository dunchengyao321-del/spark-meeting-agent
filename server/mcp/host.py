"""Minimal MCP host for servers declared in config.json.

Supports initialize / tools/list / tools/call over two transports:
- stdio: the common `npx some-mcp-server` / local binary case;
- http: Streamable-HTTP endpoints (JSON or SSE replies, Mcp-Session-Id).
"""

import asyncio
import itertools
import json
import os
import urllib.request

from server.http_client import build_opener


class MCPConnection:
    def __init__(self, spec: dict):
        self.spec = spec
        self.name = spec.get("name", "mcp")
        self.status = "stopped"
        self.error = ""
        self.tools: list[dict] = []
        self._proc: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._seq = itertools.count(1)
        self._reader_task: asyncio.Task | None = None

    async def start(self) -> None:
        if self.spec.get("transport", "stdio") != "stdio":
            self.status = "unsupported"
            self.error = "当前仅支持 stdio 传输"
            return
        command = self.spec.get("command")
        args = self.spec.get("args", [])
        if not command:
            self.status = "error"
            self.error = "缺少 command"
            return
        env = None
        extra_env = self.spec.get("env")
        if isinstance(extra_env, dict) and extra_env:
            env = {**os.environ,
                   **{str(k): str(v) for k, v in extra_env.items()}}
        cwd = self.spec.get("cwd") or None
        try:
            self._proc = await asyncio.create_subprocess_exec(
                command, *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env, cwd=cwd,
            )
        except FileNotFoundError as exc:
            self.status = "error"
            self.error = str(exc)
            return
        self._reader_task = asyncio.create_task(self._read_loop())
        try:
            await asyncio.wait_for(self._request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "spark-meeting-agent", "version": "0.1"},
            }), timeout=10)
            self._notify("notifications/initialized", {})
            result = await asyncio.wait_for(self._request("tools/list", {}), timeout=10)
            self.tools = result.get("tools", []) if isinstance(result, dict) else []
            self.status = "connected"
            self.error = ""
        except Exception as exc:  # noqa: BLE001
            self.status = "error"
            self.error = f"{type(exc).__name__}: {exc}"
            await self.stop()

    async def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg_id = message.get("id")
            future = self._pending.pop(msg_id, None) if msg_id is not None else None
            if future and not future.done():
                if "error" in message:
                    future.set_exception(RuntimeError(str(message["error"])))
                else:
                    future.set_result(message.get("result"))
        for future in self._pending.values():
            if not future.done():
                future.set_exception(RuntimeError("MCP 连接已断开"))
        self._pending.clear()
        if self.status == "connected":
            self.status = "stopped"

    def _send(self, payload: dict) -> None:
        if self._proc and self._proc.stdin:
            self._proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())

    def _notify(self, method: str, params: dict) -> None:
        try:
            self._send({"jsonrpc": "2.0", "method": method, "params": params})
        except Exception:  # noqa: BLE001
            pass

    async def _request(self, method: str, params: dict, timeout: float = 15.0):
        msg_id = next(self._seq)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        self._send({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})
        return await asyncio.wait_for(future, timeout=timeout)

    async def call_tool(self, name: str, arguments: dict):
        result = await self._request("tools/call", {"name": name, "arguments": arguments}, timeout=30)
        if isinstance(result, dict) and "content" in result:
            texts = [item.get("text", "") for item in result["content"]
                     if isinstance(item, dict) and item.get("type") == "text"]
            return "\n".join(texts)
        return json.dumps(result, ensure_ascii=False)

    async def stop(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=3)
            except (asyncio.TimeoutError, ProcessLookupError):
                self._proc.kill()
        self._proc = None


class MCPHttpConnection:
    """Streamable-HTTP MCP client: JSON-RPC over POST to a single endpoint.

    Handles both plain JSON responses and SSE (text/event-stream) replies,
    and carries the server-issued Mcp-Session-Id across requests.
    """

    def __init__(self, spec: dict, config: dict | None = None):
        self.spec = spec
        self.config = config or {}
        self.name = spec.get("name", "mcp")
        self.url = str(spec.get("url", "")).strip()
        self.status = "stopped"
        self.error = ""
        self.tools: list[dict] = []
        self._session_id = ""
        self._seq = itertools.count(1)

    async def start(self) -> None:
        if not self.url:
            self.status = "error"
            self.error = "缺少 url"
            return
        try:
            await self._request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "spark-meeting-agent", "version": "0.1"},
            }, timeout=10)
            await self._notify("notifications/initialized", {})
            result = await self._request("tools/list", {}, timeout=10)
            self.tools = result.get("tools", []) if isinstance(result, dict) else []
            self.status = "connected"
            self.error = ""
        except Exception as exc:  # noqa: BLE001
            self.status = "error"
            self.error = f"{type(exc).__name__}: {exc}"

    async def call_tool(self, name: str, arguments: dict):
        result = await self._request("tools/call",
                                     {"name": name, "arguments": arguments}, timeout=30)
        if isinstance(result, dict) and "content" in result:
            texts = [item.get("text", "") for item in result["content"]
                     if isinstance(item, dict) and item.get("type") == "text"]
            return "\n".join(texts)
        return json.dumps(result, ensure_ascii=False)

    async def stop(self) -> None:
        self.status = "stopped"

    # ------------------------------------------------------------- transport
    def _capture_session(self, headers: dict) -> None:
        session_id = headers.get("Mcp-Session-Id") or headers.get("mcp-session-id")
        if session_id:
            self._session_id = session_id

    def _post_sync(self, payload: dict) -> tuple[int, bytes]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers, method="POST")
        with build_opener(self.config, self.url).open(request, timeout=30) as response:
            self._capture_session(dict(response.headers))
            return response.status, response.read()

    async def _post(self, payload: dict) -> tuple[int, bytes]:
        return await asyncio.to_thread(self._post_sync, payload)

    @staticmethod
    def _parse_body(body: bytes) -> list[dict]:
        text = body.decode("utf-8", errors="replace").strip()
        messages: list[dict] = []
        if text.startswith("{"):
            try:
                messages.append(json.loads(text))
            except json.JSONDecodeError:
                pass
            return messages
        for line in text.splitlines():  # SSE stream
            line = line.strip()
            if not line.startswith("data:"):
                continue
            try:
                messages.append(json.loads(line[5:].strip()))
            except json.JSONDecodeError:
                continue
        return messages

    async def _request(self, method: str, params: dict, timeout: float = 15.0):
        msg_id = next(self._seq)
        payload = {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}
        status, body = await asyncio.wait_for(self._post(payload), timeout=timeout)
        for message in self._parse_body(body):
            if message.get("id") != msg_id:
                continue
            if "error" in message:
                raise RuntimeError(str(message["error"]))
            return message.get("result")
        raise RuntimeError(f"MCP HTTP 响应缺少结果（status={status}）")

    async def _notify(self, method: str, params: dict) -> None:
        try:
            await self._post({"jsonrpc": "2.0", "method": method, "params": params})
        except Exception:  # noqa: BLE001
            pass


class MCPManager:
    def __init__(self, config: dict):
        self.config = config
        self.connections = []
        for spec in config.get("mcp_servers", []):
            transport = str(spec.get("transport", "stdio")).strip().lower()
            if transport in {"http", "streamable-http", "streamable_http", "sse"}:
                self.connections.append(MCPHttpConnection(spec, config))
            else:
                self.connections.append(MCPConnection(spec))

    async def start_all(self) -> None:
        for conn in self.connections:
            await conn.start()

    def status(self) -> list[dict]:
        return [{
            "name": conn.name,
            "status": conn.status,
            "error": conn.error,
            "tools": [{"name": t.get("name"), "description": t.get("description", "")}
                      for t in conn.tools],
        } for conn in self.connections]

    def as_llm_tools(self) -> list[dict]:
        tools = []
        for conn in self.connections:
            if conn.status != "connected":
                continue
            for tool in conn.tools:
                tools.append({
                    "type": "function",
                    "function": {
                        "name": f"{conn.name}__{tool.get('name')}",
                        "description": tool.get("description", ""),
                        "parameters": tool.get("inputSchema", {"type": "object"}),
                    },
                })
        return tools

    async def call(self, qualified_name: str, arguments: dict) -> str:
        server_name, _, tool_name = qualified_name.partition("__")
        for conn in self.connections:
            if conn.name == server_name and conn.status == "connected":
                return await conn.call_tool(tool_name, arguments)
        raise RuntimeError(f"MCP 工具不可用：{qualified_name}")

    async def stop_all(self) -> None:
        for conn in self.connections:
            await conn.stop()

"""Connects every MCP server listed in hub.yaml and exposes all their tools to the agent.
Tokens live in .env; hub.yaml references them as ${VAR}. Reload from the Tools page after editing."""
import asyncio
import json
import logging
import re
import ssl
from contextlib import AsyncExitStack

import httpx2
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, PaginatedRequestParams

from config import HUB_CONFIG as CONFIG_PATH
from config import MAX_RESULT_CHARS, expand_env, read_hub_yaml
from config import MCP_CONNECT_TIMEOUT as CONNECT_TIMEOUT
from config import MCP_TOOL_TIMEOUT as TOOL_TIMEOUT

log = logging.getLogger("mcp-hub")


def _reason(exc: BaseException) -> str:
    """Short, readable cause, unwrapping anyio exception groups."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    while exc.__cause__ is not None and not str(exc):
        exc = exc.__cause__
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


def _http_client_factory(ca_bundle: str | None):
    verify = ssl.create_default_context(cafile=ca_bundle) if ca_bundle else True

    def factory(headers=None, timeout=None, auth=None) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            headers=headers, auth=auth, verify=verify,
            timeout=timeout or httpx2.Timeout(30.0, read=300.0),
        )
    return factory


def _transport(cfg: dict) -> str:
    return "stdio" if "command" in cfg else cfg.get("transport", "http")


class MCPHub:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._reset()

    def _reset(self) -> None:
        self.schemas: list[dict] = []                 # OpenAI tool schemas for every MCP tool
        self.status: dict[str, dict] = {}             # per server, for the Tools page
        self.config_error = ""                        # hub.yaml problem, for the Tools page
        self._routes: dict[str, tuple[ClientSession, str]] = {}
        self._by_server: dict[str, list[str]] = {}
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._started = False

    async def ensure_started(self) -> None:
        """Connect every server once. Each server lives in its own task, so one bad server
        can't take down the others, and its sessions open and close in the same task."""
        async with self._lock:
            if self._started:
                return
            self._started = True
            ready = []
            for name, cfg in self._load_config().items():
                cfg = cfg or {}
                if not isinstance(cfg, dict):
                    self.status[name] = {"state": "failed", "transport": "?", "tools": 0,
                                         "error": "hub.yaml entry must be a mapping (url: ... or command: ...)"}
                    continue
                self.status[name] = {"state": "connecting", "transport": _transport(cfg), "tools": 0, "error": ""}
                ev = asyncio.Event()
                self._tasks.append(asyncio.create_task(self._run_server(name, cfg, ev), name=f"mcp-{name}"))
                ready.append(ev)
            await asyncio.gather(*(ev.wait() for ev in ready))

    async def stop(self) -> None:
        """Close every server (stdio subprocesses exit too)."""
        self._stop.set()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def reload(self) -> None:
        """Re-read hub.yaml and reconnect every server (call config.reload_env() first for new tokens)."""
        async with self._lock:
            await self.stop()
            self._reset()
        await self.ensure_started()

    async def _run_server(self, name: str, cfg: dict, ready: asyncio.Event) -> None:
        try:
            async with AsyncExitStack() as stack:
                async with asyncio.timeout(CONNECT_TIMEOUT):
                    session = await self._connect(stack, expand_env(cfg or {}))
                    count = await self._register(name, session)
                log.info("MCP %s: connected, %d tools", name, count)
                self.status[name].update(state="connected", tools=count)
                ready.set()
                await self._stop.wait()
        except BaseException as exc:  # transport failures can surface as cancellation
            self._unregister(name)
            if not self._stop.is_set():
                reason = _reason(exc)
                if reason == "CancelledError" and not ready.is_set():
                    reason = f"timed out after {CONNECT_TIMEOUT:.0f}s"
                log.error("MCP %s: NOT connected or disconnected: %s", name, reason)
                self.status[name].update(state="failed", tools=0, error=reason)
        finally:
            ready.set()

    def server_of(self, tool: str) -> str | None:
        """Which server a tool name belongs to (names are sanitized, so don't split them)."""
        for server, names in list(self._by_server.items()):
            if tool in names:
                return server
        return None

    def _unregister(self, server: str) -> None:
        for name in self._by_server.pop(server, []):
            self._routes.pop(name, None)
        self.schemas = [s for s in self.schemas if s["function"]["name"] in self._routes]

    def _load_config(self) -> dict:
        if not CONFIG_PATH.exists():
            log.warning("%s not found, no MCP servers loaded", CONFIG_PATH)
            return {}
        data, self.config_error = read_hub_yaml()
        servers = data.get("mcp_servers") or {}
        if not isinstance(servers, dict):
            self.config_error = f"{CONFIG_PATH.name}: mcp_servers must be a mapping of server names"
            servers = {}
        if self.config_error:
            log.error("Can't read %s", self.config_error)
        return servers

    async def _connect(self, stack: AsyncExitStack, cfg: dict) -> ClientSession:
        if "command" in cfg:  # local server launched as a subprocess
            env = {k: str(v) for k, v in (cfg.get("env") or {}).items()}
            params = StdioServerParameters(command=cfg["command"], args=[str(a) for a in cfg.get("args", [])], env=env)
            read, write = await stack.enter_async_context(stdio_client(params))
        elif cfg.get("transport") == "sse":  # older remote servers
            factory = _http_client_factory(cfg.get("ca_bundle"))
            read, write = await stack.enter_async_context(
                sse_client(cfg["url"], headers=cfg.get("headers"), httpx_client_factory=factory)
            )
        else:  # remote server over Streamable HTTP (default)
            client = _http_client_factory(cfg.get("ca_bundle"))(headers=cfg.get("headers"))
            await stack.enter_async_context(client)
            read, write = await stack.enter_async_context(streamable_http_client(cfg["url"], http_client=client))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return session

    async def _register(self, server: str, session: ClientSession) -> int:
        tools, cursor = [], None
        while True:
            res = await session.list_tools(params=PaginatedRequestParams(cursor=cursor) if cursor else None)
            tools.extend(res.tools)
            cursor = res.next_cursor
            if not cursor:
                break
        for t in tools:
            name = re.sub(r"[^A-Za-z0-9_-]", "_", f"{server}__{t.name}")[:64]
            self._routes[name] = (session, t.name)
            self._by_server.setdefault(server, []).append(name)
            self.schemas.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": (t.description or t.name)[:1024],
                    "parameters": t.input_schema or {"type": "object", "properties": {}},
                },
            })
        return len(tools)

    async def call(self, name: str, args: dict) -> str:
        route = self._routes.get(name)
        if route is None:
            return f"Unknown tool: {name}"
        session, real_name = route
        try:
            result = await session.call_tool(real_name, args, read_timeout_seconds=TOOL_TIMEOUT)
        except Exception as exc:
            return f"Tool error: {exc}"
        if not isinstance(result, CallToolResult):
            return f"Tool asked for input this hub can't provide ({type(result).__name__})."
        parts = [c.text if getattr(c, "type", None) == "text" else f"[{c.type} content omitted]" for c in result.content]
        if not parts and result.structured_content is not None:
            parts.append(json.dumps(result.structured_content, default=str))
        text = "\n".join(parts) or "(empty result)"
        if result.is_error:
            text = "Tool error: " + text
        return text if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS] + " ...[truncated]"


hub = MCPHub()

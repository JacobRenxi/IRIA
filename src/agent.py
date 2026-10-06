"""Tool-calling loop against Ollama's OpenAI-compatible API. Tools = built-in Elastic tools
(if ELASTIC_URL is set) + every tool from every MCP server in hub.yaml."""
import json
import os
import ssl
from collections.abc import Awaitable, Callable

import httpx
from openai import AsyncOpenAI

from mcp_hub import hub

SYSTEM_PROMPT = """You are the user's AI hub. Your tools connect to their real systems
(Elasticsearch and every MCP server they configured; MCP tool names start with the server name).

Rules:
- Use tools to get real data. Never invent results.
- Only call tools that change something (create, update, delete, restart, send, block) when the user
  explicitly asks for that action. Say exactly what you changed.
- Elasticsearch: check fields with get_fields before querying; ES|QL must include LIMIT (max 100).

Reply in Markdown: the direct answer first, then key details, then which tools/queries you used.
If the tools can't answer the question, say so plainly."""

# Built-in Elastic tools are optional: leave ELASTIC_URL unset to run on MCP servers only.
BUILTIN_SCHEMAS: list[dict] = []
_builtin_call = None
if os.getenv("ELASTIC_URL"):
    from elastic_tools import TOOL_SCHEMAS as BUILTIN_SCHEMAS, call_tool as _builtin_call
BUILTIN_NAMES = {s["function"]["name"] for s in BUILTIN_SCHEMAS}


def _http_client() -> httpx.AsyncClient:
    ca_bundle = os.getenv("LLM_CA_BUNDLE")  # only if the endpoint uses HTTPS with an internal CA
    verify = ssl.create_default_context(cafile=ca_bundle) if ca_bundle else True
    return httpx.AsyncClient(verify=verify, timeout=httpx.Timeout(300.0))


llm = AsyncOpenAI(
    base_url=os.getenv("LLM_BASE_URL", "http://localhost:11434/v1/"),  # Ollama's OpenAI-compatible API
    api_key=os.getenv("LLM_API_KEY", "ollama"),  # the client requires a value; Ollama ignores it
    http_client=_http_client(),
)
MODEL = os.environ["LLM_MODEL"]  # an Ollama model tag, e.g. from `ollama list`
MAX_STEPS = int(os.getenv("AGENT_MAX_STEPS", "10"))


async def _call_tool(name: str, args: dict) -> str:
    if name in BUILTIN_NAMES:
        return await _builtin_call(name, args)
    return await hub.call(name, args)


async def run_agent(
    question: str,
    history: list[dict] | None = None,
    on_tool: Callable[[str, dict], Awaitable[None]] | None = None,
) -> str:
    """history: earlier {"role": "user"|"assistant", "content": str} turns of this chat.
    on_tool: called before each tool runs, so a front end can show what the hub is doing."""
    await hub.ensure_started()
    tools = BUILTIN_SCHEMAS + hub.schemas
    tool_kwargs = {"tools": tools} if tools else {}  # Ollama doesn't support tool_choice; auto is the default

    messages = [{"role": "system", "content": SYSTEM_PROMPT}, *(history or []), {"role": "user", "content": question}]
    for _ in range(MAX_STEPS):
        resp = await llm.chat.completions.create(model=MODEL, messages=messages, **tool_kwargs)
        msg = resp.choices[0].message
        if not msg.tool_calls:
            return (msg.content or "").strip() or "The model returned no answer."

        messages.append(msg.model_dump(exclude_none=True))
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            if on_tool:
                await on_tool(tc.function.name, args)
            result = await _call_tool(tc.function.name, args)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    messages.append({"role": "user", "content": "Stop calling tools. Summarize what you found so far."})
    resp = await llm.chat.completions.create(model=MODEL, messages=messages)
    return (resp.choices[0].message.content or "").strip()

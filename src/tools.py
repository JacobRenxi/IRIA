"""Every tool the model can call, grouped into toolsets you switch on and off on the Tools page:
built-in (memory, skills, past chats, attached files, web pages) + Elasticsearch (if configured)
+ one toolset per MCP server.

Switches live in data/tools.json. Only what's switched off is stored, so new MCP servers and
new tools start switched on."""
import asyncio
import json
import threading

import attachments
import elastic_tools
import memory
import sessions
import skills
import web
from config import DATA_DIR, MAX_RESULT_CHARS
from mcp_hub import hub

SESSION_SEARCH = {
    "type": "function",
    "function": {
        "name": "session_search",
        "description": "Search all past conversations with the user. Use when they refer to something discussed before.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Keywords to look for."}},
            "required": ["query"],
        },
    },
}


def session_search(query: str) -> str:
    hits = sessions.search(query)
    if not hits:
        return "No past conversations match."
    return json.dumps(hits, default=str)


BUILTIN = {"memory": memory.memory_tool, "skill_view": skills.skill_view,
           "skill_manage": skills.skill_manage, "session_search": session_search}
# Built-in toolsets: id -> (label, what it's for, tool schemas)
BUILTIN_TOOLSETS = {
    "memory": ("Memory", "Lets the AI save and edit notes in MEMORY.md and USER.md.", [memory.SCHEMA]),
    "skills": ("Skills", "Lets the AI load skills, and write new ones.", skills.SCHEMAS),
    "past_chats": ("Past chats", "Lets the AI search your earlier conversations.", [SESSION_SEARCH]),
    "files": ("Files", "Lets the AI read and search the whole of long files and pages attached to a chat.",
              attachments.SCHEMAS),
    "web": ("Web", "Lets the AI open more pages on websites you shared in a chat.", [web.SCHEMA]),
}
# Tools that work on the current chat's attachments: called with the chat's id first.
CHAT_TOOLS = {"file_read": attachments.file_read, "file_search": attachments.file_search, "web_fetch": web.web_fetch}
ELASTIC_NAMES = {s["function"]["name"] for s in elastic_tools.TOOL_SCHEMAS}


def toolset_of(name: str) -> str:
    if name in ELASTIC_NAMES:
        return "elastic"
    for ts, (_, _, schemas) in BUILTIN_TOOLSETS.items():
        if any(s["function"]["name"] == name for s in schemas):
            return ts
    return "mcp:" + (hub.server_of(name) or name.split("__", 1)[0])


def source(name: str) -> str:
    """Short label for the tool trail in the chat."""
    ts = toolset_of(name)
    return ts.removeprefix("mcp:").replace("_", " ")


# ---------- switches ----------

SWITCHES_PATH = DATA_DIR / "tools.json"
_lock = threading.Lock()
_off: dict[str, frozenset] | None = None  # replaced whole on every change, so readers never need the lock


def _switches() -> dict[str, frozenset]:
    global _off
    if _off is None:
        try:
            data = json.loads(SWITCHES_PATH.read_text())
        except (FileNotFoundError, ValueError):
            data = {}
        _off = {"toolsets": frozenset(data.get("off_toolsets", [])), "tools": frozenset(data.get("off_tools", []))}
    return _off


def _set(kind: str, ident: str, on: bool) -> None:
    global _off
    with _lock:
        current = _switches()
        changed = current[kind] - {ident} if on else current[kind] | {ident}
        _off = {**current, kind: changed}
        SWITCHES_PATH.parent.mkdir(parents=True, exist_ok=True)
        SWITCHES_PATH.write_text(json.dumps(
            {"off_toolsets": sorted(_off["toolsets"]), "off_tools": sorted(_off["tools"])}, indent=2) + "\n")


def toolset_ids() -> list[str]:
    ids = list(BUILTIN_TOOLSETS)
    if elastic_tools.ENABLED:
        ids.append("elastic")
    return ids + [f"mcp:{name}" for name in list(hub.status)]


def set_toolset(ident: str, on: bool) -> None:
    if ident not in toolset_ids():
        raise ValueError(f"No toolset {ident!r}.")
    _set("toolsets", ident, on)


def set_tool(name: str, on: bool) -> None:
    if name not in {s["function"]["name"] for s in all_schemas()}:
        raise ValueError(f"No tool {name!r}.")
    _set("tools", name, on)


def is_on(name: str) -> bool:
    off = _switches()
    return name not in off["tools"] and toolset_of(name) not in off["toolsets"]


# ---------- what the model gets ----------

def all_schemas() -> list[dict]:
    builtin = [s for _, _, schemas in BUILTIN_TOOLSETS.values() for s in schemas]
    return builtin + elastic_tools.TOOL_SCHEMAS + hub.schemas


def schemas() -> list[dict]:
    """Only the tools that are switched on."""
    return [s for s in all_schemas() if is_on(s["function"]["name"])]


def switched_off_toolsets() -> list[str]:
    off = _switches()["toolsets"]
    return [ts.removeprefix("mcp:") for ts in toolset_ids() if ts in off]


def catalog() -> list[dict]:
    """Every toolset with its tools and switches, for the Tools page."""
    off = _switches()
    by_set: dict[str, list[dict]] = {ts: [] for ts in toolset_ids()}
    for s in all_schemas():
        fn = s["function"]
        by_set.setdefault(toolset_of(fn["name"]), []).append(
            {"name": fn["name"], "description": fn.get("description", ""), "on": fn["name"] not in off["tools"]})
    groups = []
    for ts, tool_list in by_set.items():
        if ts in BUILTIN_TOOLSETS:
            label, about, kind, status = *BUILTIN_TOOLSETS[ts][:2], "built-in", None
        elif ts == "elastic":
            label, about, kind, status = "Elasticsearch", "Read-only search of your Elastic cluster.", "elastic", None
        else:
            server = ts.removeprefix("mcp:")
            st = hub.status.get(server, {})
            label, about, kind, status = server, "", f"MCP · {st.get('transport', '?')}", st
        groups.append({"id": ts, "label": label, "about": about, "kind": kind, "status": status,
                       "on": ts not in off["toolsets"], "tools": tool_list,
                       "tools_on": sum(t["on"] for t in tool_list)})
    return groups


async def call(name: str, args: dict, sid: str = "") -> str:
    if not is_on(name):
        return f"Tool error: {name} is switched off. The user can switch it on in Tools & status."
    try:
        if name in CHAT_TOOLS:
            result = CHAT_TOOLS[name](sid, **args)
            if asyncio.iscoroutine(result):
                result = await result
        elif name in BUILTIN:
            result = BUILTIN[name](**args)
        elif name in ELASTIC_NAMES:
            result = await elastic_tools.call_tool(name, args)
        else:
            result = await hub.call(name, args)
    except (TypeError, ValueError) as exc:  # wrong or missing arguments: tell the model so it can retry
        return f"Tool error: bad arguments for {name}: {exc}"
    return result if len(result) <= MAX_RESULT_CHARS else result[:MAX_RESULT_CHARS] + " ...[truncated]"

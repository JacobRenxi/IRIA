"""The agent: a streaming tool-calling loop against any AI connection (see providers.py).

run_agent() yields events as it works: "delta" (answer text as it's written), "tool" (a tool is
about to run), "tool_result", and finally "done". start_chat() runs it in the background with the
model picked for that chat, saves the question and answer, and keeps the events so the browser
can follow along."""
import asyncio
import json
import logging
import threading
import uuid
from collections.abc import AsyncIterator
from datetime import datetime

import openai

import elastic_tools
import memory
import providers
import runtime
import sessions
import skills
import tools
from config import AGENT_MAX_STEPS, MAX_HISTORY
from mcp_hub import hub

log = logging.getLogger("agent")

RULES = """## How you work
- Use tools to get real data. Never invent results. If the tools can't answer, say so plainly.
- Only call tools that change something (create, update, delete, restart, send, block) when the user
  explicitly asks for that action. Say exactly what you changed."""
# Each rule is added only when the tools it mentions are switched on.
TOOL_RULES = [
    (lambda names: any("__" in n for n in names), "- MCP tool names start with their server name (server__tool)."),
    (lambda names: "run_esql" in names,
     "- Elasticsearch: check fields with get_fields before querying; ES|QL must include LIMIT (max 100)."),
    (lambda names: "memory" in names,
     "- Memory: when you learn a lasting fact about the user's systems or preferences, save it with the\n"
     "  memory tool. Keep notes short. Never save passwords, tokens or one-off details."),
    (lambda names: "skill_view" in names, "- Skills: if a task matches a skill below, load it with skill_view first and follow it."),
    (lambda names: "skill_manage" in names,
     "- After you finish a multi-step task that is likely to come up again, save what worked as a skill\n"
     "  with skill_manage (or fix a skill that turned out wrong), and tell the user."),
    (lambda names: "session_search" in names,
     "- Past conversations: use session_search when the user refers to something discussed before."),
]
NO_TOOLS = {
    "model": "Your tools are off because the current model can't call them.",
    "user": "The user has switched off all your tools on the Tools & status page.",
}
NO_TOOLS_REST = ("Answer from your own knowledge, and say clearly when a question needs live data "
                 "from the user's systems.")
STYLE = "Reply in Markdown: the direct answer first, then key details, then which tools or queries you used."

def system_prompt(tool_list: list[dict], model_can_call_tools: bool) -> str:
    names = {t["function"]["name"] for t in tool_list}
    parts = [memory.read("soul")]
    if names:
        rules = [RULES] + [text for applies, text in TOOL_RULES if applies(names)]
        off = tools.switched_off_toolsets()
        if off:
            rules.append(f"- Switched off by the user: {', '.join(off)}. If a question needs one of these, "
                         "say so; the user can switch it on in Tools & status.")
        parts.append("\n".join(rules))
    else:
        parts.append("## How you work\n" + NO_TOOLS["user" if model_can_call_tools else "model"] + " " + NO_TOOLS_REST)
    parts += [
        STYLE,
        "## Your memory (notes you saved in past sessions)\n" + (memory.read("memory") or "(empty)"),
        "## About the user\n" + (memory.read("user") or "(empty)"),
    ]
    if "skill_view" in names:
        parts.append("## Skills (load with skill_view before using)\n" + (skills.prompt_index() or "(none yet)"))
    parts.append(f"Current date and time: {datetime.now().astimezone():%A %Y-%m-%d %H:%M %Z}")
    return "\n\n".join(parts)


async def _stream(p: providers.Provider, model: str, messages: list[dict],
                  tool_list: list[dict]) -> AsyncIterator[tuple[str, object]]:
    """One model call. Yields ("delta", text) while it writes, then ("calls", [tool calls])."""
    kwargs = {"tools": tool_list} if tool_list else {}  # no tool_choice: auto is the default, and Ollama rejects it
    stream = await p.client.chat.completions.create(model=model, messages=messages, stream=True, **kwargs)
    calls: list[dict] = []
    by_index: dict[int, dict] = {}
    try:
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                yield "delta", delta.content
            for tc in delta.tool_calls or []:
                slot = by_index.get(tc.index)
                if slot is None or (tc.id and slot["given_id"] and tc.id != slot["given_id"]):
                    slot = {"given_id": tc.id, "id": tc.id or f"call_{uuid.uuid4().hex[:12]}", "name": "", "arguments": ""}
                    by_index[tc.index] = slot
                    calls.append(slot)
                if tc.function:
                    slot["name"] += tc.function.name or ""
                    slot["arguments"] += tc.function.arguments or ""
    finally:
        await stream.close()
    yield "calls", calls


def _parse_args(raw: str) -> dict | None:
    try:
        args = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return None
    return args if isinstance(args, dict) else None


async def run_agent(question: str, history: list[dict], p: providers.Provider, model: str,
                    use_tools: bool = True) -> AsyncIterator[tuple[str, dict]]:
    tool_list = tools.schemas() if use_tools else []  # only the tools switched on
    messages = [{"role": "system", "content": system_prompt(tool_list, use_tools)}, *history,
                {"role": "user", "content": question}]

    for _ in range(AGENT_MAX_STEPS):
        text, calls = "", []
        async for kind, value in _stream(p, model, messages, tool_list):
            if kind == "delta":
                text += value
                yield "delta", {"text": value}
            else:
                calls = value
        if not calls:
            yield "done", {"text": text.strip() or "The model returned an empty answer."}
            return

        messages.append({"role": "assistant", "content": text, "tool_calls": [
            {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
            for c in calls
        ]})
        for c in calls:
            args = _parse_args(c["arguments"])
            ui_id = uuid.uuid4().hex[:10]  # the model's call ids aren't always unique across steps
            yield "tool", {"id": ui_id, "name": c["name"], "source": tools.source(c["name"]), "args": args or {}}
            if args is None:
                result = f"Tool error: arguments must be a JSON object, got: {c['arguments'][:200]}"
            else:
                result = await tools.call(c["name"], args)
            ok = not result.startswith(("Tool error", "Unknown tool", "Rejected"))
            yield "tool_result", {"id": ui_id, "ok": ok, "preview": result[:800]}
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": result})

    messages.append({"role": "user", "content": "Stop calling tools now. Answer with what you found so far."})
    text = ""
    async for kind, value in _stream(p, model, messages, []):
        if kind == "delta":
            text += value
            yield "delta", {"text": value}
    yield "done", {"text": text.strip() or "I ran out of tool steps before finding an answer."}


# ---------- chat runs: one answer being generated, followed by the browser ----------

class ChatRun:
    """Keeps every event, so a reloaded page can re-attach and replay the answer so far."""

    def __init__(self, sid: str) -> None:
        self.sid = sid
        self.events: list[tuple[str, dict]] = []
        self.finished = False
        self.future = None
        self._cond = threading.Condition()

    def emit(self, event: str, data: dict) -> None:
        with self._cond:
            self.events.append((event, data))
            self._cond.notify_all()

    def finish(self) -> None:
        with self._cond:
            self.finished = True
            self._cond.notify_all()

    def follow(self, keepalive: float = 15.0):
        """Yield every event from the start until the run ends; None when idle (send a keepalive)."""
        i = 0
        while True:
            with self._cond:
                if i >= len(self.events) and not self.finished:
                    self._cond.wait(keepalive)
                new, finished = self.events[i:], self.finished
            i += len(new)
            if not new and not finished:
                yield None
            yield from new
            if finished and i >= len(self.events):
                return


_runs: dict[str, ChatRun] = {}
_runs_lock = threading.Lock()


def active_run(sid: str) -> ChatRun | None:
    with _runs_lock:
        return _runs.get(sid)


def start_chat(sid: str, message: str, spec: str) -> ChatRun | None:
    """Start answering in the background with model `spec` ("<connection>/<model>").
    None if this chat is already busy."""
    with _runs_lock:
        if sid in _runs:
            return None
        run = _runs[sid] = ChatRun(sid)
    run.future = runtime.submit(_execute(run, message, spec))
    return run


def stop_chat(sid: str) -> bool:
    run = active_run(sid)
    return bool(run and run.future and run.future.cancel())


class _Unanswerable(Exception):
    """A problem to show the user as is."""


async def _execute(run: ChatRun, message: str, spec: str) -> None:
    trail, answer, error, p = [], None, None, None
    try:
        history = sessions.history(run.sid, MAX_HISTORY)
        sessions.add_message(run.sid, "user", message)
        sessions.set_model(run.sid, spec)
        try:
            p, model = providers.resolve(spec)
        except ValueError as exc:
            raise _Unanswerable(str(exc)) from None
        if not p.ok:
            await providers.check(p)  # maybe it was started since the last check
        use_tools = providers.tools_for(p, model)
        run.emit("model", {"connection": p.name, "model": model})
        while True:
            started = False
            try:
                async for event, data in run_agent(message, history, p, model, use_tools):
                    started = True
                    if event == "tool":
                        trail.append({**data, "ok": None, "preview": ""})
                    elif event == "tool_result":
                        for t in trail:
                            if t["id"] == data["id"]:
                                t.update(ok=data["ok"], preview=data["preview"][:300])
                    elif event == "done":
                        answer = data["text"]
                    run.emit(event, data)
                break
            except openai.APIStatusError as exc:
                if not (use_tools and not started and providers.rejects_tools(exc)):
                    raise
                providers.mark_no_tools(p, model)  # remembered until the next check
                use_tools = False
                run.emit("notice", {"text": f"{model} can't call tools here, so this answer uses none."})
    except asyncio.CancelledError:
        error = "Stopped."
    except _Unanswerable as exc:
        error = str(exc)
    except openai.APIConnectionError:
        error = f"Can't reach the AI connection {p.name} at {p.url}. Is it running?"
        p.ok = False
    except openai.APIStatusError as exc:
        error = providers.explain(exc, p, model)
    except Exception:
        log.exception("agent failed for: %s", message)
        error = "Something went wrong. The hub's terminal output has the details."
    finally:
        try:
            if answer is not None:
                sessions.add_message(run.sid, "assistant", answer, trail, model=spec)
            else:
                sessions.add_message(run.sid, "error", error or "No answer.", trail, model=spec)
                run.emit("error", {"text": error or "No answer."})
        finally:
            with _runs_lock:
                _runs.pop(run.sid, None)
            run.finish()


async def startup() -> None:
    sessions.init()
    await asyncio.gather(hub.ensure_started(), providers.load())


async def reload() -> None:
    """Re-read hub.yaml: AI connections and MCP servers."""
    await asyncio.gather(hub.reload(), providers.load())


async def shutdown() -> None:
    await hub.stop()
    await elastic_tools.close()
    await providers.close()

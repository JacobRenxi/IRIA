"""Persistent memory, Hermes-style: small Markdown files in data/ that go into every prompt.

  SOUL.md    who the agent is (name, tone). Only you edit it, on the Memory page.
  MEMORY.md  what it learned about your systems and environment.
  USER.md    what it learned about you: preferences, role, how you like answers.

The model edits MEMORY.md and USER.md with the `memory` tool; you can edit all three on the
Memory page. Each file is capped so the prompt stays small enough for local models."""
from config import DATA_DIR, HUB_NAME

FILES = {"soul": "SOUL.md", "memory": "MEMORY.md", "user": "USER.md"}
LIMITS = {"soul": 3000, "memory": 2500, "user": 1500}  # characters

DEFAULT_SOUL = (
    f"You are {HUB_NAME}, the user's personal AI hub.\n"
    "You are direct, practical and honest. You work through tools that connect to the user's real systems, "
    "and you remember what matters between conversations."
)

SCHEMA = {
    "type": "function",
    "function": {
        "name": "memory",
        "description": (
            "Save, change or remove a lasting note that you'll see in every future conversation. "
            "target 'memory' = facts about the user's systems and environment; "
            "target 'user' = facts about the user (preferences, role, how they like answers). "
            "Never store passwords, tokens or one-off details."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "replace", "remove"]},
                "target": {"type": "string", "enum": ["memory", "user"]},
                "content": {"type": "string", "description": "The note (one line). For add and replace."},
                "old_text": {"type": "string", "description": "Part of the existing note. For replace and remove."},
            },
            "required": ["action", "target"],
        },
    },
}


def read(name: str) -> str:
    path = DATA_DIR / FILES[name]
    text = path.read_text(encoding="utf-8").strip() if path.exists() else ""
    return text or (DEFAULT_SOUL if name == "soul" else "")


def write(name: str, text: str) -> None:
    text = text.replace("\r\n", "\n").strip()
    if len(text) > LIMITS[name]:
        raise ValueError(f"{FILES[name]} is {len(text)} characters; the limit is {LIMITS[name]}.")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / FILES[name]).write_text(text + "\n" if text else "", encoding="utf-8")


def memory_tool(action: str, target: str, content: str = "", old_text: str = "") -> str:
    if target not in ("memory", "user"):
        return "Tool error: target must be 'memory' or 'user'."
    lines = [ln for ln in read(target).splitlines() if ln.strip()]
    content = " ".join(content.split())  # one note per line
    old_text = old_text.strip()

    if action == "add":
        if not content:
            return "Tool error: content is required to add a note."
        if any(content in ln for ln in lines):
            return "Already saved."
        lines.append(f"- {content}")
    elif action in ("replace", "remove"):
        if not old_text:
            return f"Tool error: old_text is required to {action} a note."
        hits = [i for i, ln in enumerate(lines) if old_text in ln]
        if not hits:
            return f"Tool error: no note contains {old_text!r}."
        if len(hits) > 1:
            return f"Tool error: {len(hits)} notes contain {old_text!r}; use a longer, unique part."
        if action == "replace":
            if not content:
                return "Tool error: content is required to replace a note."
            lines[hits[0]] = f"- {content}"
        else:
            del lines[hits[0]]
    else:
        return "Tool error: action must be add, replace or remove."

    try:
        write(target, "\n".join(lines))
    except ValueError:
        return (f"Tool error: {FILES[target]} is full ({LIMITS[target]} characters). "
                "Remove or merge outdated notes first, then try again.")
    used = len(read(target))
    return f"Saved to {FILES[target]} ({used}/{LIMITS[target]} characters used)."

"""Skills, Hermes-style: reusable procedures in data/skills/<name>/SKILL.md.

The format is the open SKILL.md format (agentskills.io): YAML front matter with a name and a
one-line description, then Markdown steps. Only names and descriptions go into the prompt; the
model loads a whole skill with skill_view when a task matches, and writes new skills with
skill_manage after it works out how to do something, so the hub gets better with use."""
import re
import shutil

import yaml

from config import DATA_DIR

SKILLS_DIR = DATA_DIR / "skills"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")  # also keeps paths inside SKILLS_DIR
MAX_BODY = 8000  # characters

SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "skill_view",
            "description": "Load the full instructions of a skill listed in your system prompt.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_manage",
            "description": (
                "Create, update or delete a skill: a reusable, step-by-step procedure for a task that "
                "will come up again. Write steps that worked, including exact tool names and queries."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["create", "update", "delete"]},
                    "name": {"type": "string", "description": "lowercase-with-dashes, e.g. 'triage-failed-logins'"},
                    "description": {"type": "string", "description": "One line: when to use this skill."},
                    "content": {"type": "string", "description": "Markdown instructions (steps, tools, pitfalls)."},
                },
                "required": ["action", "name"],
            },
        },
    },
]


def _path(name: str):
    if not NAME_RE.match(name or ""):
        raise ValueError("Skill names use lowercase letters, digits and dashes, e.g. 'triage-failed-logins'.")
    return SKILLS_DIR / name / "SKILL.md"


def _parse(text: str) -> tuple[dict, str]:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not m:
        return {}, text.strip()
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    return (meta if isinstance(meta, dict) else {}), m.group(2).strip()


def get(name: str) -> dict | None:
    path = _path(name)
    if not path.is_file():
        return None
    meta, body = _parse(path.read_text(encoding="utf-8"))
    return {"name": name, "description": str(meta.get("description", "")).strip(), "body": body}


def list_skills() -> list[dict]:
    if not SKILLS_DIR.is_dir():
        return []
    found = []
    for d in sorted(SKILLS_DIR.iterdir()):
        if d.is_dir() and NAME_RE.match(d.name) and (d / "SKILL.md").is_file():
            found.append(get(d.name))
    return found


def save(name: str, description: str, body: str) -> None:
    path = _path(name)
    description = " ".join(description.split())
    body = body.replace("\r\n", "\n").strip()
    if not description:
        raise ValueError("A skill needs a one-line description of when to use it.")
    if len(body) > MAX_BODY:
        raise ValueError(f"Skill is {len(body)} characters; the limit is {MAX_BODY}.")
    front = yaml.safe_dump({"name": name, "description": description}, sort_keys=False, allow_unicode=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{front}---\n\n{body}\n", encoding="utf-8")


def delete(name: str) -> None:
    path = _path(name)
    if path.parent.is_dir():
        shutil.rmtree(path.parent)


def prompt_index() -> str:
    return "\n".join(f"- {s['name']}: {s['description']}" for s in list_skills())


def skill_view(name: str) -> str:
    try:
        skill = get(name)
    except ValueError as exc:
        return f"Tool error: {exc}"
    if skill is None:
        names = ", ".join(s["name"] for s in list_skills()) or "none yet"
        return f"Tool error: no skill named {name!r}. Available: {names}."
    return f"# Skill: {name}\n{skill['description']}\n\n{skill['body']}"


def skill_manage(action: str, name: str, description: str = "", content: str = "") -> str:
    try:
        existing = get(name)
        if action == "delete":
            if existing is None:
                return f"Tool error: no skill named {name!r}."
            delete(name)
            return f"Deleted skill {name}."
        if action == "create" and existing is not None:
            return f"Tool error: skill {name!r} already exists; use action 'update'."
        if action == "update" and existing is None:
            return f"Tool error: no skill named {name!r}; use action 'create'."
        if action not in ("create", "update"):
            return "Tool error: action must be create, update or delete."
        description = description or (existing or {}).get("description", "")
        content = content or (existing or {}).get("body", "")
        if not content.strip():
            return "Tool error: content (the skill's instructions) is required."
        save(name, description, content)
    except ValueError as exc:
        return f"Tool error: {exc}"
    return f"Skill {name} {'created' if action == 'create' else 'updated'}. It's listed in every future prompt."

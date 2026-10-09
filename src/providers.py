"""AI connections: everywhere the hub can get a model from.

Every connection is an OpenAI-compatible API, which is what Ollama, LiteLLM, vLLM, LM Studio,
llama.cpp, OpenAI, OpenRouter, Groq, Mistral and most others speak. For providers that don't
(Anthropic, Bedrock, Vertex, Azure...), put LiteLLM in front of them and connect to LiteLLM.

Connections are listed in hub.yaml under `providers:`, with API keys in .env referenced as ${VAR}.
Without that section, LLM_BASE_URL / LLM_API_KEY / LLM_MODEL from .env make one connection,
so older setups keep working. A model is named "<connection>/<model>", e.g. "ollama/qwen3:8b".

Models are found automatically. Models added by hand in the web UI (for keys whose models the server
doesn't list) and the default model picked there are kept in data/models.json."""
import asyncio
import json
import logging
import os
import re
import ssl
import threading
from dataclasses import dataclass, field

import httpx
import openai
from openai import AsyncOpenAI

import config

log = logging.getLogger("providers")
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")  # no "/": it separates connection and model
KIND_LABELS = {"ollama": "Ollama", "litellm": "LiteLLM", "openai": "OpenAI-compatible"}


@dataclass
class Provider:
    name: str
    url: str = ""
    listed: list[str] = field(default_factory=list)  # models named in hub.yaml; empty = all the server lists
    client: AsyncOpenAI | None = None
    http: httpx.AsyncClient | None = None
    kind: str = "openai"                          # ollama | litellm | openai (any other compatible API)
    models: dict[str, dict] = field(default_factory=dict)  # name -> {"tools": bool | None, "available": bool}
    no_tools: set[str] = field(default_factory=set)  # models that refused tools; kept until hub.yaml is reloaded
    ok: bool = False
    error: str = ""

    @property
    def root(self) -> str:
        """Server address without the /v1 API suffix (Ollama's and LiteLLM's own endpoints live there)."""
        return self.url.rstrip("/").removesuffix("/v1")

    @property
    def kind_label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)


providers: dict[str, Provider] = {}  # replaced whole on reload, so readers never see half a config
default_spec = ""                    # "<connection>/<model>" new chats start with
config_error = ""


# ---------- loading hub.yaml ----------

def _env_fallback() -> tuple[dict, str]:
    """One connection from LLM_* in .env, for setups without `providers:` in hub.yaml."""
    cfg = {"url": os.getenv("LLM_BASE_URL") or "http://localhost:11434/v1", "api_key": os.getenv("LLM_API_KEY"),
           "ca_bundle": os.getenv("LLM_CA_BUNDLE")}
    model = os.getenv("LLM_MODEL", "")
    return {"default": cfg}, f"default/{model}" if model else ""


def _build(name: str, raw) -> Provider:
    p = Provider(name=name)
    try:
        if not NAME_RE.match(name):
            raise ValueError("connection names use letters, digits, '-', '_' and '.' only")
        if not isinstance(raw, dict) or not raw.get("url"):
            raise ValueError("needs a url, e.g. url: http://localhost:11434/v1")
        cfg = config.expand_env(raw)
        p.url = str(cfg["url"]).strip()
        models = cfg.get("models") or []
        p.listed = [str(m) for m in ([models] if isinstance(models, str) else models)]
        ca = cfg.get("ca_bundle")
        verify = ssl.create_default_context(cafile=str(ca)) if ca else True
        api_key = str(cfg.get("api_key") or "")
        headers = {str(k): str(v) for k, v in (cfg.get("headers") or {}).items()}
        if api_key:
            headers.setdefault("Authorization", f"Bearer {api_key}")
        p.http = httpx.AsyncClient(verify=verify, headers=headers,
                                   timeout=httpx.Timeout(float(cfg.get("timeout", 300)), connect=10.0))
        p.client = AsyncOpenAI(base_url=p.url, api_key=api_key or "none", http_client=p.http, max_retries=1)
    except (ValueError, TypeError, OSError, config.MissingToken) as exc:
        p.error = str(exc)
    return p


async def load() -> None:
    """(Re)read the AI connections from hub.yaml and check every one of them."""
    global providers, default_spec, config_error
    data, config_error = config.read_hub_yaml()
    if data.get("providers") is None:
        cfgs, spec = _env_fallback()
    else:
        cfgs, spec = data["providers"], str(data.get("default_model") or "")
    if not isinstance(cfgs, dict):
        config_error = f"{config.HUB_CONFIG.name}: providers must be a mapping of connection names"
        cfgs = {}

    old = list(providers.values())
    providers, default_spec = {str(name): _build(str(name), raw) for name, raw in cfgs.items()}, spec
    await check_all()
    if old:  # a chat may still be using the old clients: close them a little later
        asyncio.get_running_loop().call_later(600, lambda: asyncio.ensure_future(_close(old)))


async def _close(items: list[Provider]) -> None:
    for p in items:
        if p.http is not None:
            await p.http.aclose()


async def close() -> None:
    await _close(list(providers.values()))


# ---------- checking connections and finding models ----------

async def _get_json(p: Provider, method: str, path: str, **kw):
    try:
        r = await p.http.request(method, p.root + path, timeout=10, **kw)
        return r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


async def _detect(p: Provider) -> str:
    version = await _get_json(p, "GET", "/api/version")
    if isinstance(version, dict) and "version" in version:
        return "ollama"
    alive = await _get_json(p, "GET", "/health/liveliness")
    if isinstance(alive, str) and "alive" in alive.lower():
        return "litellm"
    return "openai"


async def _ollama_models(p: Provider) -> dict[str, dict]:
    tags = await _get_json(p, "GET", "/api/tags") or {}
    found = {}
    for m in tags.get("models", []):
        show = await _get_json(p, "POST", "/api/show", json={"model": m["name"]}) or {}
        caps = show.get("capabilities")
        if caps is not None and "completion" not in caps:
            continue  # embedding-only models can't chat
        found[m["name"]] = {"tools": caps is None or "tools" in caps, "available": True}
    return found


async def _openai_models(p: Provider) -> dict[str, dict]:
    info = {}
    if p.kind == "litellm":  # LiteLLM knows which of its models can call tools
        for d in (await _get_json(p, "GET", "/model/info") or {}).get("data", []):
            info[d.get("model_name")] = d.get("model_info") or {}
    try:
        names = [m.id async for m in p.client.models.list()]
    except openai.APIStatusError:
        # Some keys may chat but not list models. LiteLLM can still say what this key may use.
        key = (await _get_json(p, "GET", "/key/info") or {}).get("info") or {} if p.kind == "litellm" else {}
        names = [m for m in key.get("models") or [] if isinstance(m, str)] or [n for n in info if n]
        if not names:
            raise
    found = {}
    for name in names:
        meta = info.get(name, {})
        if "*" in name or name == "all-proxy-models" or meta.get("mode") not in (None, "chat", "completion", "responses"):
            continue  # wildcard routes, embeddings, image models...
        found[name] = {"tools": meta.get("supports_function_calling"), "available": True}
    return found


async def check(p: Provider) -> None:
    if p.client is None:
        return  # bad config: p.error says why
    note = ""
    try:
        p.kind = await _detect(p)
        found = await (_ollama_models(p) if p.kind == "ollama" else _openai_models(p))
    except openai.APIConnectionError:
        p.ok, p.error = False, f"Can't reach {p.url}. Is it running?"
        return
    except openai.APIStatusError as exc:
        # The server answered, so the connection works; it just won't list models for this key.
        found = {}
        if exc.status_code in (401, 403):
            note = (f"The server won't list models for this key ({exc.status_code}). If chats fail too, the key "
                    "is wrong; otherwise add the models your key can use below.")
        else:
            note = f"Couldn't list models ({exc.status_code}). Add the models your key can use below."
    except Exception as exc:  # an odd server must not stop the hub from starting
        log.exception("checking AI connection %s failed", p.name)
        p.ok, p.error = False, f"{type(exc).__name__}: {exc}"[:300]
        return
    if p.listed:  # only the models named in hub.yaml, in that order
        found = {m: found.get(m, {"tools": None, "available": not found}) for m in p.listed}
    for m in added_models(p.name):  # added by hand in the web UI
        found.setdefault(m, {"tools": None, "available": True, "only_added": True})["added"] = True
    for m in p.no_tools & found.keys():
        found[m]["tools"] = False
    if not found and not note:
        note = ("No chat models found. Install one with `ollama pull qwen3:8b`." if p.kind == "ollama"
                else "No chat models found. Add the models your key can use below.")
    p.models, p.ok, p.error = found, True, note


async def check_all() -> None:
    await asyncio.gather(*(check(p) for p in providers.values()))


# ---------- choosing a model ----------

def split(spec: str) -> tuple[str, str]:
    """"ollama/qwen3:8b" -> ("ollama", "qwen3:8b"). Model names may contain "/" themselves."""
    name, _, model = (spec or "").partition("/")
    return name, model


def default() -> str:
    """The model new chats start with: the one picked in the web UI, else default_model from hub.yaml,
    else the first one available."""
    for spec in (_saved().get("default", ""), default_spec):
        name, model = split(spec)
        p = providers.get(name)
        if p and model and (model in p.models or not p.models):
            return spec
    for p in providers.values():
        for m, info in p.models.items():
            if p.ok and info["available"]:
                return f"{p.name}/{m}"
    return default_spec


def resolve(spec: str) -> tuple[Provider, str]:
    """The connection and model for "<connection>/<model>", or ValueError with a readable reason."""
    name, model = split(spec)
    p = providers.get(name)
    if p is None or not model:
        raise ValueError(f"Unknown model {spec!r}. Pick one from the list.")
    if p.client is None:
        raise ValueError(f"The AI connection {name} isn't set up right: {p.error}")
    return p, model


def tools_for(p: Provider, model: str) -> bool:
    return p.models.get(model, {}).get("tools") is not False  # unknown counts as yes


def mark_no_tools(p: Provider, model: str) -> None:
    p.no_tools.add(model)
    p.models = {**p.models, model: {**p.models.get(model, {"available": True}), "tools": False}}


TOOLS_REJECTED = ("does not support tools", "does not support parameters: ['tools'", "tool choice requires",
                  "tools is not supported", "tools are not supported", "function calling is not supported",
                  "does not support function calling", "tool use is not supported")


def rejects_tools(exc: openai.APIStatusError) -> bool:
    """Did the server refuse because this model can't call tools?"""
    return exc.status_code in (400, 404, 422) and any(t in str(exc.message).lower() for t in TOOLS_REJECTED)


def explain(exc: openai.APIStatusError, p: Provider, model: str) -> str:
    msg = str(exc.message)
    if exc.status_code == 404 or "not found" in msg.lower():
        if p.kind == "ollama":
            return f"Model {model!r} isn't installed on {p.name}. Run `ollama pull {model}`, or pick another model."
        return f"{p.name} has no model {model!r}. Pick another model, or check `models:` in hub.yaml."
    if exc.status_code in (401, 403):
        low = msg.lower()
        if "model" in low and ("allowed" in low or "access" in low):
            return (f"Your API key on {p.name} isn't allowed to use {model!r}. Pick another model "
                    "(Tools & status lists what this connection offers).")
        return f"{p.name} rejected the API key ({exc.status_code}). Check its api_key in hub.yaml and .env."
    if exc.status_code == 429:
        return f"{p.name} says too many requests or out of quota (429). Wait a bit, or pick another model."
    return f"{p.name} returned an error ({exc.status_code}): {msg}"[:500]


# ---------- choices made in the web UI (data/models.json) ----------

SAVED_PATH = config.DATA_DIR / "models.json"
_saved_lock = threading.Lock()
_saved_cache: dict | None = None  # {"added": {connection: [models]}, "default": "conn/model"}; replaced whole


def _saved() -> dict:
    global _saved_cache
    if _saved_cache is None:
        try:
            data = json.loads(SAVED_PATH.read_text())
        except (FileNotFoundError, ValueError):
            data = {}
        _saved_cache = {"added": data.get("added") or {}, "default": data.get("default") or ""}
    return _saved_cache


def _update(change) -> None:
    """Read, change and write data/models.json as one step."""
    global _saved_cache
    with _saved_lock:
        _saved_cache = change(dict(_saved()))
        SAVED_PATH.parent.mkdir(parents=True, exist_ok=True)
        SAVED_PATH.write_text(json.dumps(_saved_cache, indent=2) + "\n")


def added_models(connection: str) -> list[str]:
    return list(_saved()["added"].get(connection, []))


def _valid_model_name(model: str) -> str:
    model = model.strip()
    if not model or len(model) > 200 or "*" in model or any(ch.isspace() for ch in model):
        raise ValueError("Type the model's exact name as the server knows it, e.g. gpt-4o (no spaces).")
    return model


def add_model(connection: str, model: str) -> str:
    """Offer a model the server doesn't list (e.g. one your API key may use). Returns its spec."""
    p = providers.get(connection)
    if p is None:
        raise ValueError(f"No AI connection named {connection!r}.")
    model = _valid_model_name(model)
    _update(lambda d: {**d, "added": {**d["added"], connection: sorted({*d["added"].get(connection, []), model})}})
    p.models = {**p.models, model: {**p.models.get(model, {"tools": None, "available": True, "only_added": True}),
                                    "added": True}}
    return f"{connection}/{model}"


def remove_model(connection: str, model: str) -> None:
    def change(d):
        added = {**d["added"], connection: [m for m in d["added"].get(connection, []) if m != model]}
        return {**d, "added": {k: v for k, v in added.items() if v}}
    _update(change)
    p = providers.get(connection)
    if p and p.models.get(model, {}).get("added"):
        if p.models[model].get("only_added"):
            p.models = {k: v for k, v in p.models.items() if k != model}
        else:  # the server lists it too: keep it, just not as hand-added
            p.models = {**p.models, model: {k: v for k, v in p.models[model].items() if k != "added"}}


def set_default(spec: str) -> None:
    resolve(spec)  # ValueError if it's not a usable connection
    _update(lambda d: {**d, "default": spec})


# ---------- for the pages ----------

def summary() -> dict:
    """The default model's state, for the banner and the sidebar."""
    spec = default()
    name, model = split(spec)
    p = providers.get(name)
    st = {"model": model or "no model", "provider": name, "spec": spec, "ok": False, "tools": True, "error": ""}
    if not providers:
        st["error"] = f"No AI connection. Add one under `providers:` in {config.HUB_CONFIG.name}."
    elif not spec:
        down = "; ".join(f"{c.name}: {c.error or 'no models'}" for c in providers.values())
        st["error"] = f"No model available yet. {down}"
    elif p is None:
        st["error"] = f"default_model {spec!r} doesn't match a connection in {config.HUB_CONFIG.name}."
    elif not p.ok:
        st["error"] = f"AI connection {p.name}: {p.error or 'not checked yet'}"
    elif model not in p.models:
        if p.kind == "ollama":
            st["error"] = f"Model {model!r} isn't installed on {p.name}. Run `ollama pull {model}`, or change default_model."
        else:
            st["error"] = f"{p.name} has no model {model!r}. Change default_model in {config.HUB_CONFIG.name}."
    else:
        st["ok"] = True
        st["tools"] = p.models[model]["tools"] is not False
        if not p.models[model]["available"]:
            st["error"] = f"{p.name} doesn't list {model!r}, so it may not work. Check `models:` in {config.HUB_CONFIG.name}."
        elif not st["tools"]:
            st["error"] = (f"{model} can't call tools, so new chats are chat-only. Pick a model with tool support "
                           "in the chat, or change default_model.")
    return st


def picker() -> list[dict]:
    """Every connection and its models, for the model picker and the Tools page."""
    return [{"name": p.name, "url": p.url, "kind": p.kind_label, "ok": p.ok, "error": p.error,
             "models": [{"name": m, "spec": f"{p.name}/{m}", **info} for m, info in p.models.items()]}
            for p in providers.values()]

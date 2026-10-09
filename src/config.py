"""Every setting in one place, read from .env in the project root, plus helpers for hub.yaml.
Paths are resolved from the project root, so the hub runs from any working directory."""
import os
import re
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")  # real environment variables win over .env


def _path(value: str) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else ROOT / p


HUB_NAME = os.getenv("HUB_NAME", "IRIA")
DATA_DIR = _path(os.getenv("HUB_DATA_DIR", "data"))  # chats, memory, skills
HUB_CONFIG = _path(os.getenv("HUB_CONFIG", "hub.yaml"))  # AI connections + MCP servers

# --- Web (Flask) ---
WEB_USER = os.getenv("WEB_USER", "admin")
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "")
WEB_HOST = os.getenv("WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.getenv("WEB_PORT", "8090"))
WEB_TLS_CERT = os.getenv("WEB_TLS_CERT")
WEB_TLS_KEY = os.getenv("WEB_TLS_KEY")
WEB_SECRET_KEY = os.getenv("WEB_SECRET_KEY")  # signs login cookies; generated into data/ if unset

# --- Agent (AI connections themselves are in hub.yaml; see providers.py) ---
AGENT_MAX_STEPS = int(os.getenv("AGENT_MAX_STEPS", "10"))  # tool rounds per answer
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "20"))  # earlier messages sent back to the model

# --- Built-in Elasticsearch tools (off unless both are set) ---
ELASTIC_URL = os.getenv("ELASTIC_URL")
ELASTIC_API_KEY = os.getenv("ELASTIC_API_KEY")
ELASTIC_CA_CERTS = os.getenv("ELASTIC_CA_CERTS")

# --- MCP servers ---
MCP_TOOL_TIMEOUT = float(os.getenv("MCP_TOOL_TIMEOUT", "120"))
MCP_CONNECT_TIMEOUT = float(os.getenv("MCP_CONNECT_TIMEOUT", "30"))

MAX_RESULT_CHARS = int(os.getenv("TOOL_RESULT_MAX_CHARS", "12000"))  # per tool result, keeps context small

# --- Files and web pages attached to a chat ---
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "25"))
# Characters of attached text put in front of the AI with every message (all attachments together).
# Bigger files are cut there and the AI reads the rest with tools. Lower it for small local models.
ATTACH_CONTEXT_CHARS = int(os.getenv("ATTACH_CONTEXT_CHARS", "30000"))
# Links to addresses on your own network (localhost, 10.x, 192.168.x, ...) are not fetched unless this is 1.
WEB_ALLOW_PRIVATE = os.getenv("WEB_ALLOW_PRIVATE", "0") == "1"


def check() -> None:
    """Stop at startup with a clear message instead of failing later."""
    if len(WEB_PASSWORD) < 12:
        raise SystemExit("Set WEB_PASSWORD (12+ characters) in .env: this hub can reach every connected system.")
    if bool(WEB_TLS_CERT) != bool(WEB_TLS_KEY):
        raise SystemExit("Set both WEB_TLS_CERT and WEB_TLS_KEY to serve HTTPS, or neither.")
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def reload_env() -> None:
    """Re-read .env, so tokens added since startup work after "Reload hub.yaml"."""
    load_dotenv(ROOT / ".env", override=True)


# ---------- hub.yaml ----------

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class MissingToken(Exception):
    pass


def expand_env(value):
    """Replace ${VAR} in hub.yaml values with the value from .env / the environment."""
    if isinstance(value, str):
        def sub(m):
            if m.group(1) not in os.environ:
                raise MissingToken(f"${{{m.group(1)}}} is not set in .env")
            return os.environ[m.group(1)]
        return _VAR.sub(sub, value)
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    return value


def read_hub_yaml() -> tuple[dict, str]:
    """hub.yaml as a dict, and a readable error ('' if it's fine). Read fresh on every reload."""
    if not HUB_CONFIG.exists():
        return {}, ""
    try:
        with open(HUB_CONFIG) as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            raise ValueError("the top level must be a mapping (providers: / mcp_servers:)")
    except (yaml.YAMLError, ValueError) as exc:
        return {}, f"{HUB_CONFIG.name}: {exc}"
    return data, ""

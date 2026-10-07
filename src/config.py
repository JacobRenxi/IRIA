"""Every setting in one place, read from .env in the project root.
Paths are resolved from the project root, so the hub runs from any working directory."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")  # real environment variables win over .env


def _path(value: str) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else ROOT / p


HUB_NAME = os.getenv("HUB_NAME", "IRIA")
DATA_DIR = _path(os.getenv("HUB_DATA_DIR", "data"))  # chats, memory, skills
HUB_CONFIG = _path(os.getenv("HUB_CONFIG", "hub.yaml"))  # MCP servers

# --- Web (Flask) ---
WEB_USER = os.getenv("WEB_USER", "admin")
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "")
WEB_HOST = os.getenv("WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.getenv("WEB_PORT", "8090"))
WEB_TLS_CERT = os.getenv("WEB_TLS_CERT")
WEB_TLS_KEY = os.getenv("WEB_TLS_KEY")
WEB_SECRET_KEY = os.getenv("WEB_SECRET_KEY")  # signs login cookies; generated into data/ if unset

# --- Model (any OpenAI-compatible API; Ollama by default) ---
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:11434/v1/")
LLM_MODEL = os.getenv("LLM_MODEL", "")
LLM_API_KEY = os.getenv("LLM_API_KEY", "ollama")  # the client needs a value; Ollama ignores it
LLM_CA_BUNDLE = os.getenv("LLM_CA_BUNDLE")
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


def check() -> None:
    """Stop at startup with a clear message instead of failing later."""
    if len(WEB_PASSWORD) < 12:
        raise SystemExit("Set WEB_PASSWORD (12+ characters) in .env: this hub can reach every connected system.")
    if not LLM_MODEL:
        raise SystemExit("Set LLM_MODEL in .env to a model from `ollama list` (e.g. qwen3:8b).")
    if bool(WEB_TLS_CERT) != bool(WEB_TLS_KEY):
        raise SystemExit("Set both WEB_TLS_CERT and WEB_TLS_KEY to serve HTTPS, or neither.")
    DATA_DIR.mkdir(parents=True, exist_ok=True)

"""Web chat front end for the AI hub. Open http(s)://<host>:8090 in a browser.
Login is HTTP Basic auth with WEB_USER / WEB_PASSWORD from .env."""
import base64
import hmac
import json
import logging
import os
import ssl
from pathlib import Path

from aiohttp import web
from dotenv import find_dotenv, load_dotenv

# Load .env before agent.py / elastic_tools.py read os.environ at import.
load_dotenv(find_dotenv(usecwd=True))

from agent import run_agent  # noqa: E402
from mcp_hub import hub  # noqa: E402

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ai-hub-web")

WEB_USER = os.getenv("WEB_USER", "admin")
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "")
if len(WEB_PASSWORD) < 12:
    raise SystemExit("Set WEB_PASSWORD (12+ characters) in .env: this page can reach every system in hub.yaml.")
HOST = os.getenv("WEB_HOST", "0.0.0.0")
PORT = int(os.getenv("WEB_PORT", "8090"))
TLS_CERT = os.getenv("WEB_TLS_CERT")  # set both to serve HTTPS (recommended: Basic auth over HTTP is readable on the wire)
TLS_KEY = os.getenv("WEB_TLS_KEY")
MAX_HISTORY = 20  # past messages sent back to the model for context

STATIC = Path(__file__).parent / "static"
for _f in ("index.html", "app.js"):
    if not (STATIC / _f).is_file():
        raise SystemExit(f"Missing {STATIC / _f}. index.html and app.js must be in src/static/ next to web.py.")
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


@web.middleware
async def basic_auth(request: web.Request, handler):
    header = request.headers.get("Authorization", "")
    if header.startswith("Basic "):
        try:
            user, _, password = base64.b64decode(header[6:]).decode().partition(":")
        except Exception:
            user, password = "", ""
        user_ok = hmac.compare_digest(user.encode(), WEB_USER.encode())
        pass_ok = hmac.compare_digest(password.encode(), WEB_PASSWORD.encode())
        if user_ok and pass_ok:
            return await handler(request)
        log.warning("failed login from %s", request.remote)
    return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="ai-hub", charset="UTF-8"'})


async def add_security_headers(request: web.Request, response: web.StreamResponse) -> None:
    response.headers.update(SECURITY_HEADERS)


async def index(request: web.Request) -> web.Response:
    return web.FileResponse(STATIC / "index.html")


async def app_js(request: web.Request) -> web.Response:
    return web.FileResponse(STATIC / "app.js", headers={"Content-Type": "text/javascript"})


async def chat(request: web.Request) -> web.StreamResponse:
    # Custom header + JSON content type: a page on another site can't send this without a CORS preflight we never allow.
    if request.headers.get("X-Requested-With") != "ai-hub" or request.content_type != "application/json":
        return web.json_response({"error": "forbidden"}, status=403)
    body = await request.json()
    message = str(body.get("message", "")).strip()
    if not message:
        return web.json_response({"error": "empty message"}, status=400)
    history = [
        {"role": m["role"], "content": str(m.get("content", ""))}
        for m in (body.get("history") or [])[-MAX_HISTORY:]
        if isinstance(m, dict) and m.get("role") in ("user", "assistant")
    ]

    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
    await resp.prepare(request)

    async def send(event: str, data: dict) -> None:
        try:
            await resp.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode())
        except ConnectionResetError:
            pass  # browser closed the tab; the agent run still finishes

    async def on_tool(name: str, args: dict) -> None:
        await send("tool", {"name": name, "args": args})

    try:
        answer = await run_agent(message, history=history, on_tool=on_tool)
        await send("answer", {"text": answer})
    except Exception:
        log.exception("agent failed for: %s", message)
        await send("error", {"text": "The hub couldn't finish this request. Check the hub logs for details."})
    await resp.write_eof()
    return resp


async def start_hub(app: web.Application) -> None:
    await hub.ensure_started()  # connect MCP servers now so config errors show at startup
    log.info("MCP tools loaded: %d", len(hub.schemas))


async def stop_hub(app: web.Application) -> None:
    hub._stop.set()


def make_app() -> web.Application:
    app = web.Application(middlewares=[basic_auth], client_max_size=1024 * 1024)
    app.router.add_get("/", index)
    app.router.add_get("/app.js", app_js)
    app.router.add_post("/api/chat", chat)
    app.on_response_prepare.append(add_security_headers)
    app.on_startup.append(start_hub)
    app.on_cleanup.append(stop_hub)
    return app


if __name__ == "__main__":
    ssl_ctx = None
    if TLS_CERT and TLS_KEY:
        ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_ctx.load_cert_chain(TLS_CERT, TLS_KEY)
    web.run_app(make_app(), host=HOST, port=PORT, ssl_context=ssl_ctx)

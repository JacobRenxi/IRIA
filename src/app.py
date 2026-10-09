"""The hub's web app (Flask). Start it from anywhere with:

    python src/app.py

then open http://localhost:8090 and log in with WEB_USER / WEB_PASSWORD from .env."""
import hmac
import json
import logging
import re
import secrets
import signal
import time
from datetime import timedelta

from flask import Flask, Response, abort, flash, jsonify, redirect, render_template, request, session, url_for
from markupsafe import Markup, escape

import config

config.check()

import agent  # noqa: E402  (config.check() first: it creates data/)
import elastic_tools  # noqa: E402
import memory  # noqa: E402
import providers  # noqa: E402
import runtime  # noqa: E402
import sessions  # noqa: E402
import skills  # noqa: E402
import tools  # noqa: E402
from mcp_hub import hub  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per model/MCP request is too noisy
logging.getLogger("openai").setLevel(logging.WARNING)  # "retrying request" lines
logging.getLogger("elastic_transport").setLevel(logging.ERROR)  # a traceback per retry; errors show on the Tools page
log = logging.getLogger("web")


def _secret_key() -> str:
    """Signs the login cookie. Generated once and kept in data/, so logins survive restarts."""
    if config.WEB_SECRET_KEY:
        return config.WEB_SECRET_KEY
    path = config.DATA_DIR / "secret_key"
    if not path.exists():
        path.write_text(secrets.token_hex(32))
        path.chmod(0o600)
    return path.read_text().strip()


app = Flask(__name__)  # templates/ and static/ sit next to this file
app.config.update(
    SECRET_KEY=_secret_key(),
    SESSION_COOKIE_NAME="hub_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(config.WEB_TLS_CERT),
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    MAX_CONTENT_LENGTH=1024 * 1024,
)

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


# ---------- login + CSRF ----------

def csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


@app.before_request
def guard():
    if request.method == "POST":
        # Every form and API call carries this token, so another site can't post here on your behalf.
        sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
        expected = session.get("csrf", "")
        if not expected or not hmac.compare_digest(sent.encode(), expected.encode()):
            if request.path.startswith("/api/"):
                return jsonify(error="Your session expired. Reload the page."), 403
            flash("That form expired. Please try again.", "error")
            return redirect(request.path if request.endpoint == "login" else url_for("index"))
    if request.endpoint in ("login", "static"):
        return None
    if not session.get("user"):
        if request.path.startswith("/api/"):
            return jsonify(error="You're logged out. Reload the page to log in."), 401
        return redirect(url_for("login"))
    return None


@app.after_request
def add_headers(resp: Response) -> Response:
    resp.headers.update(SECURITY_HEADERS)
    if request.endpoint != "static":
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.template_filter("ticks")
def ticks(text: str) -> Markup:
    """Show `backticked` parts of status messages as code."""
    return Markup(re.sub(r"`([^`]+)`", r"<code>\1</code>", str(escape(text))))


@app.context_processor
def page_globals():
    return {"hub_name": config.HUB_NAME, "csrf_token": csrf_token, "model": providers.summary()}


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        user_ok = hmac.compare_digest(request.form.get("username", "").encode(), config.WEB_USER.encode())
        pass_ok = hmac.compare_digest(request.form.get("password", "").encode(), config.WEB_PASSWORD.encode())
        if user_ok and pass_ok:
            session.clear()
            session["user"] = config.WEB_USER
            session.permanent = True
            return redirect(url_for("index"))
        log.warning("failed login from %s", request.remote_addr)
        time.sleep(1)  # slows down password guessing
        error = "Wrong username or password."
    return render_template("login.html", error=error), 401 if error else 200


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def page(template: str, **kw) -> str:
    """Render a page with the chat list in the sidebar."""
    return render_template(template, chats=sessions.list_sessions(limit=60), **kw)


# ---------- chat ----------

def chat_page(current: dict | None):
    """The chat, with the model picker set to this chat's model (or the default for a new chat)."""
    connections = providers.picker()
    specs = {m["spec"] for c in connections for m in c["models"]}
    selected = current["model"] if current and current.get("model") in specs else providers.default()
    return page("chat.html", nav="chat", current=current, connections=connections, selected=selected,
                selected_unlisted=selected if selected and selected not in specs else "",
                messages=sessions.messages(current["id"]) if current else [],
                running=bool(current) and agent.active_run(current["id"]) is not None,
                tool_count=len(tools.schemas()))


@app.get("/")
def index():
    return chat_page(None)


@app.get("/c/<sid>")
def chat(sid: str):
    current = sessions.get(sid)
    if current is None:
        abort(404)
    return chat_page(current)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _follow(run: agent.ChatRun, chat_row: dict) -> Response:
    """Stream a chat run to the browser as server-sent events. If the browser goes away,
    the answer still finishes and is saved to the chat."""
    def stream():
        yield _sse("session", {"id": chat_row["id"], "title": chat_row["title"]})
        for item in run.follow():
            yield ": keepalive\n\n" if item is None else _sse(*item)
    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/chat")
def api_chat():
    body = request.get_json(silent=True) or {}
    message = str(body.get("message", "")).strip()
    if not message:
        return jsonify(error="Type a message first."), 400
    sid = body.get("session_id")
    if sid and sessions.get(sid) is None:
        return jsonify(error="That chat was deleted."), 404
    spec = str(body.get("model") or providers.default())
    try:
        conn, model = providers.resolve(spec)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    if conn.models and model not in conn.models:
        return jsonify(error=f"{conn.name} has no model {model!r}. Pick one from the list."), 400
    sid = sid or sessions.create(message)
    run = agent.start_chat(sid, message, spec)
    if run is None:
        return jsonify(error="This chat is still answering. Wait for it, or press Stop."), 409
    return _follow(run, sessions.get(sid))


@app.get("/api/chat/<sid>/stream")
def api_chat_follow(sid: str):
    run = agent.active_run(sid)
    if run is None:
        return "", 204
    return _follow(run, sessions.get(sid))


@app.post("/api/chat/<sid>/stop")
def api_chat_stop(sid: str):
    return jsonify(stopped=agent.stop_chat(sid))


@app.get("/api/sessions")
def api_sessions():
    return jsonify(sessions.list_sessions(request.args.get("q", ""), limit=60))


@app.post("/api/sessions/<sid>/delete")
def api_session_delete(sid: str):
    if agent.active_run(sid):
        return jsonify(error="Stop this chat before deleting it."), 409
    sessions.delete(sid)
    return jsonify(ok=True)


# ---------- memory ----------

@app.route("/memory", methods=["GET", "POST"])
def memory_page():
    files = {name: memory.read(name) for name in memory.FILES}
    if request.method == "POST":
        files = {name: request.form.get(name, "") for name in memory.FILES}
        too_long = [f"{memory.FILES[n]} is {len(t.strip())} characters (limit {memory.LIMITS[n]})."
                    for n, t in files.items() if len(t.strip()) > memory.LIMITS[n]]
        if not too_long:
            for name, text in files.items():
                memory.write(name, text)
            flash("Memory saved. It applies from the next message.")
            return redirect(url_for("memory_page"))
        for msg in too_long:
            flash(msg, "error")
    return page("memory.html", nav="memory", files=files, limits=memory.LIMITS, names=memory.FILES)


# ---------- skills ----------

@app.get("/skills")
def skills_page():
    return page("skills.html", nav="skills", skills=skills.list_skills())


@app.get("/skills/new")
def skill_new():
    return page("skill_edit.html", nav="skills", skill={"name": "", "description": "", "body": ""}, is_new=True)


@app.get("/skills/<name>")
def skill_edit(name: str):
    try:
        skill = skills.get(name)
    except ValueError:
        skill = None
    if skill is None:
        abort(404)
    return page("skill_edit.html", nav="skills", skill=skill, is_new=False)


@app.post("/skills/save")
def skill_save():
    form = request.form
    name, original = form.get("name", "").strip(), form.get("original", "").strip()
    skill = {"name": name, "description": form.get("description", ""), "body": form.get("body", "")}
    try:
        if name != original and skills.get(name) is not None:
            raise ValueError(f"A skill named {name!r} already exists.")
        skills.save(name, skill["description"], skill["body"])
        if original and original != name:
            skills.delete(original)
    except ValueError as exc:
        flash(str(exc), "error")
        return page("skill_edit.html", nav="skills", skill=skill, is_new=not original, original=original)
    flash(f"Skill {name} saved.")
    return redirect(url_for("skill_edit", name=name))


@app.post("/skills/<name>/delete")
def skill_delete(name: str):
    try:
        skills.delete(name)
    except ValueError:
        abort(404)
    flash(f"Skill {name} deleted.")
    return redirect(url_for("skills_page"))


# ---------- tools / status ----------

@app.get("/tools")
def tools_page():
    runtime.run(providers.check_all(), timeout=60)
    return page(
        "tools.html", nav="tools", toolsets=tools.catalog(), tools_on=len(tools.schemas()),
        tools_total=len(tools.all_schemas()), config_error=providers.config_error or hub.config_error,
        hub_config=config.HUB_CONFIG, connections=providers.picker(), default_spec=providers.default(),
        elastic=runtime.run(elastic_tools.status(), timeout=70),
    )


@app.post("/api/tools/switch")
def api_tools_switch():
    """Switch a whole toolset (kind "toolset") or one tool (kind "tool") on or off."""
    body = request.get_json(silent=True) or {}
    kind, ident, on = body.get("kind"), str(body.get("id", "")), body.get("on") is True
    try:
        if kind == "toolset":
            tools.set_toolset(ident, on)
        elif kind == "tool":
            tools.set_tool(ident, on)
        else:
            return jsonify(error='kind must be "toolset" or "tool".'), 400
    except ValueError as exc:
        return jsonify(error=str(exc)), 404
    return jsonify(tools_on=len(tools.schemas()), tools_total=len(tools.all_schemas()))


@app.post("/models/add")
def models_add():
    try:
        spec = providers.add_model(request.form.get("connection", ""), request.form.get("model", ""))
        flash(f"Added {spec}. It's in the chat's model list now.")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("tools_page"))


@app.post("/models/remove")
def models_remove():
    providers.remove_model(request.form.get("connection", ""), request.form.get("model", ""))
    flash("Model removed from the list.")
    return redirect(url_for("tools_page"))


@app.post("/models/default")
def models_default():
    try:
        providers.set_default(request.form.get("spec", ""))
        flash(f"New chats now start with {request.form['spec']}.")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("tools_page"))


@app.post("/api/models/add")
def api_models_add():
    """"Other model…" in the chat's model list."""
    body = request.get_json(silent=True) or {}
    try:
        spec = providers.add_model(str(body.get("connection", "")), str(body.get("model", "")))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(spec=spec)


@app.post("/tools/reload")
def tools_reload():
    config.reload_env()  # new tokens in .env count too
    runtime.run(agent.reload(), timeout=config.MCP_CONNECT_TIMEOUT + 60)
    models = sum(len(c["models"]) for c in providers.picker())
    ok = sum(1 for s in hub.status.values() if s["state"] == "connected")
    flash(f"Reloaded hub.yaml: {len(providers.providers)} AI connections with {models} models; "
          f"{ok} of {len(hub.status)} MCP servers connected.")
    return redirect(url_for("tools_page"))


@app.errorhandler(404)
def not_found(_):
    if request.path.startswith("/api/"):
        return jsonify(error="Not found."), 404
    return page("notfound.html", nav=None), 404


def _stop_on_term(signum, frame):
    raise KeyboardInterrupt


def main() -> None:
    runtime.start()
    runtime.run(agent.startup())
    for c in providers.picker():
        state = f"{len(c['models'])} models" if c["ok"] else f"NOT ready: {c['error']}"
        log.info("AI connection %s (%s, %s): %s", c["name"], c["kind"], c["url"], state)
    st = providers.summary()
    if st["error"]:
        log.warning("Default model: %s", st["error"])
    else:
        log.info("Default model: %s", st["spec"])
    log.info("Tools: %d switched on (%d from MCP servers)", len(tools.schemas()), len(hub.schemas))

    ssl_ctx = (config.WEB_TLS_CERT, config.WEB_TLS_KEY) if config.WEB_TLS_CERT else None
    shown = "localhost" if config.WEB_HOST in ("127.0.0.1", "0.0.0.0") else config.WEB_HOST
    log.info("Open %s://%s:%d", "https" if ssl_ctx else "http", shown, config.WEB_PORT)
    signal.signal(signal.SIGTERM, _stop_on_term)  # `kill` shuts down as cleanly as Ctrl+C
    try:
        app.run(host=config.WEB_HOST, port=config.WEB_PORT, threaded=True, ssl_context=ssl_ctx)
    finally:
        runtime.run(agent.shutdown(), timeout=15)
        runtime.stop()


if __name__ == "__main__":
    main()

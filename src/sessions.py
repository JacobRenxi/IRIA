"""Saved chats in SQLite (data/hub.db), with full-text search over every message.
The model can search them too (the session_search tool), so it can recall past conversations."""
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager

from config import DATA_DIR

DB_PATH = DATA_DIR / "hub.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    model TEXT                   -- "<connection>/<model>" this chat last used
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,          -- user | assistant | error
    content TEXT NOT NULL,
    tools TEXT,                  -- JSON list of tool calls made for this answer (shown in the chat)
    created REAL NOT NULL,
    model TEXT                   -- "<connection>/<model>" that wrote this answer
);
CREATE INDEX IF NOT EXISTS messages_by_session ON messages(session_id, id);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(content, content='messages', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;
"""


@contextmanager
def _db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        with con:  # commit on success, roll back on error
            yield con
    finally:
        con.close()


def init() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _db() as con:
        con.execute("PRAGMA journal_mode = WAL")
        con.executescript(SCHEMA)
        for table in ("sessions", "messages"):  # databases made before chats remembered their model
            if "model" not in {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}:
                con.execute(f"ALTER TABLE {table} ADD COLUMN model TEXT")


def _fts_query(text: str, match_all: bool) -> str | None:
    """Turn free text into a safe FTS5 query: quoted words, prefix-matched."""
    words = re.findall(r"\w+", text)[:12]
    if not words:
        return None
    return (" AND " if match_all else " OR ").join(f'"{w}"*' for w in words)


def create(title: str) -> str:
    sid = uuid.uuid4().hex[:12]
    now = time.time()
    title = " ".join(title.split())
    title = title[:60] + ("…" if len(title) > 60 else "")
    with _db() as con:
        con.execute("INSERT INTO sessions (id, title, created, updated) VALUES (?, ?, ?, ?)",
                    (sid, title or "New chat", now, now))
    return sid


def get(sid: str) -> dict | None:
    with _db() as con:
        row = con.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
    return dict(row) if row else None


def list_sessions(query: str = "", limit: int = 100) -> list[dict]:
    """Newest first. With a query: only chats whose messages or title match every word."""
    fts = _fts_query(query, match_all=True)
    with _db() as con:
        if fts is None:
            rows = con.execute("SELECT * FROM sessions ORDER BY updated DESC LIMIT ?", (limit,)).fetchall()
        else:
            like = f"%{query.strip()}%"
            rows = con.execute(
                """SELECT * FROM sessions WHERE title LIKE ? OR id IN (
                       SELECT m.session_id FROM messages_fts f JOIN messages m ON m.id = f.rowid
                       WHERE messages_fts MATCH ?)
                   ORDER BY updated DESC LIMIT ?""",
                (like, fts, limit),
            ).fetchall()
    return [dict(r) for r in rows]


def messages(sid: str) -> list[dict]:
    with _db() as con:
        rows = con.execute(
            "SELECT role, content, tools, created, model FROM messages WHERE session_id = ? ORDER BY id", (sid,)
        ).fetchall()
    return [{**dict(r), "tools": json.loads(r["tools"]) if r["tools"] else []} for r in rows]


def history(sid: str, limit: int) -> list[dict]:
    """The last `limit` messages as model input. Tool results and errors are left out to save context."""
    with _db() as con:
        rows = con.execute(
            """SELECT role, content FROM messages WHERE session_id = ? AND role IN ('user', 'assistant')
               ORDER BY id DESC LIMIT ?""",
            (sid, limit),
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def add_message(sid: str, role: str, content: str, tools: list | None = None, model: str | None = None) -> None:
    now = time.time()
    with _db() as con:
        con.execute(
            "INSERT INTO messages (session_id, role, content, tools, created, model) VALUES (?, ?, ?, ?, ?, ?)",
            (sid, role, content, json.dumps(tools) if tools else None, now, model),
        )
        con.execute("UPDATE sessions SET updated = ? WHERE id = ?", (now, sid))


def set_model(sid: str, spec: str) -> None:
    with _db() as con:
        con.execute("UPDATE sessions SET model = ? WHERE id = ?", (spec, sid))


def delete(sid: str) -> None:
    with _db() as con:
        con.execute("DELETE FROM sessions WHERE id = ?", (sid,))


def search(query: str, limit: int = 8) -> list[dict]:
    """Best-matching messages across all chats, with a short snippet around the match."""
    fts = _fts_query(query, match_all=False)
    if fts is None:
        return []
    with _db() as con:
        rows = con.execute(
            """SELECT s.id, s.title, m.role, m.created,
                      snippet(messages_fts, 0, '**', '**', ' … ', 24) AS snippet
               FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid
               JOIN sessions s ON s.id = m.session_id
               WHERE messages_fts MATCH ? AND m.role != 'error' ORDER BY rank LIMIT ?""",
            (fts, limit),
        ).fetchall()
    return [dict(r) for r in rows]

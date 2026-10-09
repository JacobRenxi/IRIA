"""Reading web pages.

Links you paste into a message are opened, their text is attached to the chat (like a file), and
the AI sees it with every message. The web_fetch tool lets the AI open more pages, but only on
sites you shared in that chat, so a page can't talk the AI into sending your data elsewhere.
Addresses on your own network (localhost, 10.x, 192.168.x...) are refused unless
WEB_ALLOW_PRIVATE=1 is set in .env."""
import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlparse

import httpx

import attachments
import sessions
from attachments import Unreadable
from config import MAX_RESULT_CHARS, WEB_ALLOW_PRIVATE

URL_RE = re.compile(r"https?://[^\s<>\"'`\]]+", re.I)
MAX_LINKS_PER_MESSAGE = 3
MAX_PAGE_BYTES = 5 * 1024 * 1024
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; IRIA-hub/1.0; reading a page its user shared)",
    "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
}

SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_fetch",
        "description": ("Open a web page and get its text and links. Only works for sites the user shared in this "
                        "chat: use it to read other pages of those sites (e.g. pages linked from an attached page)."),
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "Full address, starting with https://"}},
            "required": ["url"],
        },
    },
}


@dataclass
class Page:
    url: str
    title: str
    text: str
    size: int
    links: list[str] = field(default_factory=list)
    note: str = ""


def urls_in(text: str) -> list[str]:
    """The links in a message, without trailing punctuation, at most MAX_LINKS_PER_MESSAGE."""
    found = []
    for url in URL_RE.findall(text):
        url = url.rstrip(".,;:!?")
        while url.endswith(")") and url.count(")") > url.count("("):
            url = url[:-1]
        if url not in found:
            found.append(url)
    return found[:MAX_LINKS_PER_MESSAGE]


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


# ---------- fetching ----------

async def _check_address(url: str) -> None:
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise Unreadable("Only http:// and https:// links can be read.")
    if WEB_ALLOW_PRIVATE:
        return
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise Unreadable(f"Can't find the site {parts.hostname}.") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            raise Unreadable(f"{parts.hostname} is on your own network ({ip}), and those pages aren't read. "
                             "To allow them, set WEB_ALLOW_PRIVATE=1 in .env and restart.")


async def _download(url: str) -> tuple[str, str, bytes, str]:
    """(final address, content type, body, charset). Each redirect is checked like the first address."""
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=False,
                                 timeout=httpx.Timeout(20.0, connect=10.0)) as client:
        for _ in range(6):
            await _check_address(url)
            async with client.stream("GET", url) as r:
                if r.is_redirect and "location" in r.headers:
                    url = urljoin(url, r.headers["location"])
                    continue
                if r.status_code >= 400:
                    raise Unreadable(f"The site answered {r.status_code} {r.reason_phrase}.")
                body = bytearray()
                async for chunk in r.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_PAGE_BYTES:
                        raise Unreadable("The page is too big (over 5 MB).")
                return url, r.headers.get("content-type", "").lower(), bytes(body), r.charset_encoding or "utf-8"
    raise Unreadable("The page redirects too many times.")


async def read(url: str) -> Page:
    try:
        final, ctype, body, charset = await _download(url)
    except httpx.HTTPError as exc:
        raise Unreadable(f"Couldn't open the page ({type(exc).__name__}: {exc})."[:300]) from None
    looks_html = body.lstrip()[:200].lower().startswith((b"<!doctype html", b"<html"))
    if "html" in ctype or (not ctype and looks_html):
        parser = _PageText(final)
        parser.feed(body.decode(charset, errors="replace"))
        parser.close()
        text = parser.text()
        heavy = len(text) < 300 and len(body) > 20_000  # lots of page, little text: built by JavaScript
        note = ("This page has very little text. It probably builds its content with JavaScript, which the "
                "hub can't run.") if heavy else ""
        return Page(final, parser.title or final, text or "(no text)", len(body), parser.links, note)
    if "pdf" in ctype or final.lower().endswith(".pdf"):
        return Page(final, final.rsplit("/", 1)[-1] or final, attachments.extract("page.pdf", body), len(body))
    if ctype.startswith("text/") or "json" in ctype or "xml" in ctype or not ctype:
        return Page(final, final.rsplit("/", 1)[-1] or final, attachments.extract("page.txt", body), len(body))
    raise Unreadable(f"Can't read this kind of content ({ctype.split(';')[0]}).")


# ---------- HTML to readable text ----------

SKIP = {"script", "style", "noscript", "template", "svg", "iframe", "canvas", "head", "nav", "footer", "aside",
        "dialog", "button", "select"}
SKIP_ROLES = {"navigation", "banner", "contentinfo", "complementary", "search", "dialog", "alert"}
SKIP_NAMES = {"sidebar", "sphinxsidebar", "navbar", "breadcrumb", "breadcrumbs", "site-header", "site-footer",
              "cookie", "cookies", "cookie-banner", "skip-link", "related"}
BLOCK = {"p", "div", "section", "article", "main", "header", "ul", "ol", "table", "tr", "pre", "blockquote",
         "dl", "dt", "dd", "figure", "figcaption", "li", "h1", "h2", "h3", "h4", "h5", "h6", "form", "details", "summary"}
VOID = {"br", "hr", "img", "meta", "link", "input", "source", "wbr", "area", "col", "embed", "param", "track", "base"}


class _PageText(HTMLParser):
    """Keeps the readable text (headings as #, list items as -, table cells with |) and every link.
    Menus, sidebars and footers are left out, and when the page marks its main part (<main>,
    <article>, role="main") only that part is kept."""

    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base, self.title, self.links = base, "", []
        self._all: list[str] = []
        self._main: list[str] = []
        self._stack: list[str] = []   # open elements
        self._skip_at = None          # depth of the element being left out, if any
        self._main_at = None          # depth of the main part, if inside it
        self._in_title = False

    def _put(self, s: str) -> None:
        self._all.append(s)
        if self._main_at is not None:
            self._main.append(s)

    @staticmethod
    def _boilerplate(tag: str, a: dict) -> bool:
        if tag in SKIP or a.get("role") in SKIP_ROLES or a.get("aria-hidden") == "true":
            return True
        names = set((a.get("class") or "").lower().split()) | {(a.get("id") or "").lower()}
        return bool(names & SKIP_NAMES)

    def handle_starttag(self, tag, attrs):
        a = {k: v or "" for k, v in attrs}
        if tag == "title":
            self._in_title = True
        if tag == "a":
            link = urldefrag(urljoin(self.base, a.get("href", "")))[0]
            if link.startswith(("http://", "https://")) and link not in self.links and len(self.links) < 300:
                self.links.append(link)
        if tag == "body":  # a page may leave <head> unclosed: start fresh
            self._stack, self._skip_at, self._main_at = ["body"], None, None
            return
        if tag in VOID:
            if tag in ("br", "hr") and self._skip_at is None:
                self._put("\n")
            return
        self._stack.append(tag)
        depth = len(self._stack)
        if self._skip_at is None and self._boilerplate(tag, a):
            self._skip_at = depth
        if self._skip_at is not None:
            return
        if self._main_at is None and (tag in ("main", "article") or a.get("role") == "main"):
            self._main_at = depth
        if tag in BLOCK:
            self._put("\n")
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._put("#" * int(tag[1]) + " ")
        elif tag == "li":
            self._put("- ")
        elif tag in ("td", "th"):
            self._put(" | ")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in VOID or tag not in self._stack:
            return
        was_skipping = self._skip_at is not None
        while self._stack:  # close this element, and any it left open
            top = self._stack.pop()
            if self._skip_at is not None and len(self._stack) < self._skip_at:
                self._skip_at = None
            if self._main_at is not None and len(self._stack) < self._main_at:
                self._main_at = None
                if not was_skipping:
                    self._main.append("\n")
            if top == tag:
                break
        if not was_skipping and tag in BLOCK and tag not in ("li", "tr"):  # list items, rows: one line each
            self._put("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += " ".join(data.split())
            return
        if self._skip_at is not None:
            return
        self._put(data if "pre" in self._stack else re.sub(r"\s+", " ", data))

    def text(self) -> str:
        main = "".join(self._main)
        raw = main if len(main.strip()) >= 400 else "".join(self._all)
        lines = [line.strip() for line in raw.split("\n")]
        lines = [line for line in lines if line not in ("-", "|")]  # empty list items and cells
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


# ---------- links in a message, and the web_fetch tool ----------

def shared_sites(sid: str) -> set[str]:
    return {_host(r["source"]) for r in sessions.attachments(sid) if r["source"]}


async def attach_links(sid: str, message: str, emit) -> None:
    """Open every new link in the user's message and attach the page to the chat.
    emit(event, data) reports progress in the chat's tool trail."""
    already = {r["source"] for r in sessions.attachments(sid) if r["source"]}
    for i, url in enumerate(urls_in(message)):
        if url in already:
            continue
        step = f"link{i}"
        emit("tool", {"id": step, "name": "read page", "source": "web", "args": {"url": url}})
        try:
            page = await read(url)
            row = attachments.add(sid, page.title, page.text, page.size, source=url)
        except Unreadable as exc:
            emit("tool_result", {"id": step, "ok": False, "preview": f"Couldn't read {url}: {exc}"})
            continue
        summary = f"{page.title}: {row['chars']:,} characters attached to this chat."
        emit("tool_result", {"id": step, "ok": True, "preview": summary + (" " + page.note if page.note else "")})
        emit("attachment", attachments.public(row))


async def web_fetch(sid: str, url: str) -> str:
    sites = shared_sites(sid)
    if _host(url) not in sites:
        return (f"Tool error: you can only open pages on sites the user shared in this chat "
                f"({', '.join(sorted(sites)) or 'none yet'}). Ask the user to paste the link if they want it read.")
    try:
        page = await read(url)
    except Unreadable as exc:
        return f"Tool error: {exc}"
    same_site = [link for link in page.links if _host(link) in sites][:60]
    room = MAX_RESULT_CHARS - 300 - sum(len(link) + 1 for link in same_site)
    text = page.text if len(page.text) <= room else page.text[:room] + " ...[page cut short]"
    links = "\n".join(same_site) or "(none)"
    note = f"{page.note}\n" if page.note else ""
    return f"# {page.title}\n{page.url}\n{note}\n{text}\n\nLinks on this page to the same site:\n{links}"

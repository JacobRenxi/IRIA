// Chat page: renders saved messages, streams new answers, shows every tool call as it happens.
const log = document.getElementById("log");
const form = document.getElementById("composer");
const input = document.getElementById("input");
const sendBtn = document.getElementById("send");
const modelSelect = document.getElementById("model-select");
const initial = JSON.parse(document.getElementById("chat-data").textContent);
let sessionId = initial.session;
let busy = false;

// ---------- Markdown (safe subset: everything is escaped first) ----------
function inline(s) {
  const codes = [];
  s = s.replace(/`([^`\n]+)`/g, (_, c) => "\u0000" + (codes.push(c) - 1) + "\u0000");
  s = s
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*\w])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>")
    .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  return s.replace(/\u0000(\d+)\u0000/g, (_, i) => "<code>" + codes[i] + "</code>").replace(/\n/g, "<br>");
}

const cells = row => row.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map(c => inline(c.trim()));

function blocks(md) {
  const lines = md.split("\n");
  let html = "", para = [], list = null, quote = [];
  const flushPara = () => { if (para.length) html += "<p>" + inline(para.join("\n")) + "</p>"; para = []; };
  const flushList = () => {
    if (list) html += "<" + list.tag + ">" + list.items.map(i => "<li>" + inline(i) + "</li>").join("") + "</" + list.tag + ">";
    list = null;
  };
  const flushQuote = () => { if (quote.length) html += "<blockquote>" + inline(quote.join("\n")) + "</blockquote>"; quote = []; };
  const flush = () => { flushPara(); flushList(); flushQuote(); };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    let m;
    if (!line.trim()) { flush(); continue; }
    if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
      flush();
      const level = Math.min(4, m[1].length + 1);
      html += "<h" + level + ">" + inline(m[2]) + "</h" + level + ">";
    } else if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
      flush(); html += "<hr>";
    } else if (line.trim().startsWith("|") && /^\s*\|?[\s:|-]*-[\s:|-]*$/.test(lines[i + 1] || "")) {
      flush();
      const head = cells(line);
      const body = [];
      for (i += 2; i < lines.length && lines[i].trim().startsWith("|"); i++) body.push(cells(lines[i]));
      i--;
      html += '<div class="table-wrap"><table><thead><tr>' + head.map(c => "<th>" + c + "</th>").join("") +
        "</tr></thead><tbody>" + body.map(r => "<tr>" + r.map(c => "<td>" + c + "</td>").join("") + "</tr>").join("") +
        "</tbody></table></div>";
    } else if ((m = line.match(/^\s*[-*+]\s+(.*)$/)) || (m = line.match(/^\s*\d+[.)]\s+(.*)$/))) {
      const tag = /^\s*\d/.test(line) ? "ol" : "ul";
      flushPara(); flushQuote();
      if (!list || list.tag !== tag) { flushList(); list = { tag, items: [] }; }
      list.items.push(m[1]);
    } else if ((m = line.match(/^&gt;\s?(.*)$/))) {
      flushPara(); flushList(); quote.push(m[1]);
    } else if (list && /^\s{2,}\S/.test(line)) {
      list.items[list.items.length - 1] += " " + line.trim();
    } else {
      flushList(); flushQuote(); para.push(line);
    }
  }
  flush();
  return html;
}

function renderMarkdown(md) {
  return md.split("```").map((part, i) =>
    i % 2 ? "<pre><code>" + esc(part.replace(/^[\w+-]*\n/, "")) + "</code></pre>" : blocks(esc(part))
  ).join("");
}

// ---------- building the chat ----------
function el(tag, cls, html) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html !== undefined) n.innerHTML = html;
  return n;
}

const nearBottom = () => log.scrollHeight - log.scrollTop - log.clientHeight < 120;
const scrollDown = (force) => { if (force || nearBottom()) log.scrollTop = log.scrollHeight; };

function newTurn(question) {
  document.getElementById("empty")?.remove();
  const turn = el("section", "turn");
  turn.append(el("div", "ask", esc(question)), el("ul", "trail"));
  log.append(turn);
  return turn;
}

function toolItem(t) {
  const state = t.ok === true ? "ok" : t.ok === false ? "fail" : "pending";
  const li = el("li", state);
  li.dataset.id = t.id;
  let action = t.name.includes("__") ? t.name.split("__").slice(1).join("__") : t.name;
  if (action === t.source) action = "";  // e.g. the built-in "memory" tool: don't say "memory memory"
  const args = t.args && Object.keys(t.args).length ? " <code>" + esc(JSON.stringify(t.args).slice(0, 240)) + "</code>" : "";
  li.innerHTML = '<details><summary><span class="status"></span><span><span class="sys">' + esc(t.source || "?") +
    "</span> " + esc(action) + args + "</span></summary></details>";
  if (t.preview) li.querySelector("details").append(el("pre", null, esc(t.preview)));
  return li;
}

function finishTool(trail, data) {
  const li = [...trail.querySelectorAll("li[data-id]")].find(n => n.dataset.id === data.id);
  if (!li) return;
  li.className = data.ok ? "ok" : "fail";
  li.querySelector("details").append(el("pre", null, esc(data.preview || "(empty result)")));
}

// "ollama/qwen3:8b" -> "qwen3:8b · ollama", shown under each answer
function modelLine(spec) {
  const cut = spec.indexOf("/");
  return el("div", "answer-meta", esc(cut < 0 ? spec : spec.slice(cut + 1) + " · " + spec.slice(0, cut)));
}

function showSaved(messages) {
  let turn = null;
  for (const m of messages) {
    if (m.role === "user") { turn = newTurn(m.content); continue; }
    if (!turn) turn = newTurn("");
    const trail = turn.querySelector(".trail");
    for (const t of m.tools || []) trail.append(toolItem(t));
    turn.append(m.role === "error" ? el("p", "error-msg", esc(m.content)) : el("div", "answer", renderMarkdown(m.content)));
    if (m.model && m.role === "assistant") turn.append(modelLine(m.model));
  }
  scrollDown(true);
}

// ---------- streaming ----------
// Reads the server-sent events of one answer into `turn`.
async function follow(res, turn) {
  const trail = turn.querySelector(".trail");
  const working = el("div", "working", "<span></span><span></span><span></span>");
  turn.append(working);
  let answer = null, text = "", pending = false, finished = false, model = null;

  const paint = () => {
    pending = false;
    if (!answer) return;
    const stick = nearBottom();
    answer.innerHTML = renderMarkdown(text);
    if (stick) scrollDown(true);
  };
  const handle = (event, data) => {
    if (event === "session") return onSession(data);
    if (event === "model") { model = data.connection + "/" + data.model; return; }
    if (event === "notice") { trail.append(el("li", "interim", esc(data.text))); return; }
    if (event === "attachment") { addChip(data); return; }
    if (event === "delta") {
      text += data.text;
      if (!answer) { answer = el("div", "answer streaming"); working.before(answer); }
      if (!pending) { pending = true; requestAnimationFrame(paint); }
    } else if (event === "tool") {
      // Text written before a tool call is the model thinking aloud: keep it in the trail.
      if (text.trim()) trail.append(el("li", "interim", esc(text.trim())));
      answer?.remove(); answer = null; text = "";
      trail.append(toolItem(data));
    } else if (event === "tool_result") {
      finishTool(trail, data);
    } else if (event === "done") {
      finished = true;
      if (!answer) { answer = el("div", "answer"); working.before(answer); }
      text = data.text; paint(); answer.classList.remove("streaming");
      if (model) working.before(modelLine(model));
    } else if (event === "error") {
      finished = true;
      answer?.classList.remove("streaming");
      working.before(el("p", "error-msg", esc(data.text)));
    }
    scrollDown();
  };

  try {
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let cut;
      while ((cut = buf.indexOf("\n\n")) !== -1) {
        const block = buf.slice(0, cut);
        buf = buf.slice(cut + 2);
        const event = (block.match(/^event: (.*)$/m) || [])[1];
        const data = (block.match(/^data: (.*)$/m) || [])[1];
        if (event && data) handle(event, JSON.parse(data));
      }
    }
    if (!finished) throw new Error("The connection closed early. Reload the page in a moment to see the answer.");
  } catch (err) {
    if (!finished) working.before(el("p", "error-msg", esc(err.message || String(err))));
  } finally {
    working.remove();
    answer?.classList.remove("streaming");
    scrollDown();
  }
}

function onSession(chat) {
  if (sessionId) return;
  sessionId = chat.id;
  history.replaceState(null, "", "/c/" + encodeURIComponent(chat.id));
  document.title = chat.title + " · " + document.title;
  document.querySelector("#chat-list .chat-list-empty")?.remove();
  document.querySelectorAll("#chat-list li.active").forEach(li => li.classList.remove("active"));
  document.getElementById("chat-list").prepend(chatItem(chat, true));
}

function setBusy(on) {
  busy = on;
  sendBtn.textContent = on ? "Stop" : "Send";
  sendBtn.classList.toggle("btn-primary", !on);
  sendBtn.classList.toggle("btn-stop", on);
  sendBtn.type = on ? "button" : "submit";
}

async function send(message) {
  setBusy(true);
  const turn = newTurn(message);
  scrollDown(true);
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": CSRF },
      body: JSON.stringify({ session_id: sessionId, message, model: modelSelect?.value }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.error || "The hub returned HTTP " + res.status + ".");
    }
    await follow(res, turn);
  } catch (err) {
    turn.append(el("p", "error-msg", esc(err.message)));
  }
  setBusy(false);
  input.focus();
}

// ---------- files and pages attached to this chat ----------
const chips = document.getElementById("chips");
const fileInput = document.getElementById("file-input");
const chatBox = document.querySelector(".chat");
const dropOverlay = document.getElementById("drop-overlay");

function sizeLabel(chars) {
  if (chars >= 1e6) return (chars / 1e6).toFixed(1) + "M chars";
  if (chars >= 1e3) return Math.round(chars / 1e3) + "k chars";
  return chars + " chars";
}

function addChip(a) {
  const li = el("li", "chip" + (a.source ? " chip-web" : ""));
  li.dataset.id = a.id;
  const name = a.source
    ? '<a href="' + esc(a.source) + '" target="_blank" rel="noopener noreferrer">' + esc(a.name) + "</a>"
    : esc(a.name);
  li.title = (a.source || a.name) + "\nThe AI sees this with every message in this chat.";
  li.innerHTML = '<span class="chip-icon" aria-hidden="true">' + (a.source ? "🌐" : "📄") + "</span>" +
    '<span class="chip-name">' + name + '</span><span class="chip-size">' + sizeLabel(a.chars) + "</span>" +
    '<button type="button" class="chip-x" aria-label="Remove ' + esc(a.name) + ' from this chat">&times;</button>';
  chips.append(li);
}

function addNote(cls, text) {
  const li = el("li", "chip " + cls, '<span class="chip-name">' + esc(text) + "</span>" +
    '<button type="button" class="chip-x" aria-label="Dismiss">&times;</button>');
  chips.append(li);
  return li;
}

chips.addEventListener("click", async e => {
  const x = e.target.closest(".chip-x");
  if (!x) return;
  const li = x.closest(".chip");
  if (li.dataset.id && sessionId) {
    try { await api("/api/files/" + encodeURIComponent(sessionId) + "/" + encodeURIComponent(li.dataset.id) + "/delete", {}); }
    catch (err) { alert(err.message); return; }
  }
  li.remove();
});

async function upload(files) {
  if (!files.length) return;
  const pending = addNote("chip-pending", "Reading " + (files.length === 1 ? files[0].name : files.length + " files") + "…");
  const form = new FormData();
  for (const f of files) form.append("files", f);
  if (sessionId) form.append("session_id", sessionId);
  try {
    const res = await fetch("/api/files", { method: "POST", headers: { "X-CSRF-Token": CSRF }, body: form });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "Upload failed (HTTP " + res.status + ").");
    if (data.session) onSession(data.session);
    data.files.forEach(addChip);
    data.errors.forEach(e => addNote("chip-error", e.name + ": " + e.error));
  } catch (err) {
    addNote("chip-error", err.message);
  } finally {
    pending.remove();
    input.focus();
  }
}

document.getElementById("attach").addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => { upload([...fileInput.files]); fileInput.value = ""; });

// Drop files anywhere on the chat. Elsewhere on the page, a dropped file must not replace the page.
let dragDepth = 0;
const hasFiles = e => [...(e.dataTransfer?.types || [])].includes("Files");
chatBox.addEventListener("dragenter", e => { if (hasFiles(e)) { dragDepth++; dropOverlay.hidden = false; } });
chatBox.addEventListener("dragleave", e => { if (hasFiles(e) && --dragDepth <= 0) { dragDepth = 0; dropOverlay.hidden = true; } });
chatBox.addEventListener("dragover", e => { if (hasFiles(e)) e.preventDefault(); });
chatBox.addEventListener("drop", e => {
  if (!hasFiles(e)) return;
  e.preventDefault();
  dragDepth = 0;
  dropOverlay.hidden = true;
  upload([...e.dataTransfer.files]);
});
window.addEventListener("dragover", e => { if (hasFiles(e)) e.preventDefault(); });
window.addEventListener("drop", e => { if (hasFiles(e)) e.preventDefault(); });

(initial.attached || []).forEach(addChip);

// ---------- wiring ----------
form.addEventListener("submit", e => {
  e.preventDefault();
  const message = input.value.trim();
  if (!message || busy) return;
  input.value = "";
  input.style.height = "";
  send(message);
});

sendBtn.addEventListener("click", async () => {
  if (!busy || !sessionId) return;
  sendBtn.disabled = true;
  try { await api("/api/chat/" + encodeURIComponent(sessionId) + "/stop", {}); } catch (err) { console.error(err); }
  sendBtn.disabled = false;
});

input.addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); }
});
input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 192) + "px";
});

document.querySelectorAll(".suggestion").forEach(b =>
  b.addEventListener("click", () => { if (!busy) send(b.textContent.trim()); }));

showSaved(initial.messages);

// New chats start with the model you picked last time (if it's still there).
const MODEL_KEY = "hub.model";
if (modelSelect && !sessionId) {
  try {
    const last = localStorage.getItem(MODEL_KEY);
    if (last && [...modelSelect.options].some(o => o.value === last && !o.disabled && !o.dataset.add)) modelSelect.value = last;
  } catch { /* storage blocked: keep the default */ }
}

// Show which connection the picked model is on (two connections can offer the same model name).
const modelConn = document.getElementById("model-conn");
const showConnection = () => {
  const v = modelSelect?.value || "";
  if (modelConn) modelConn.textContent = v.includes("/") ? "on " + v.slice(0, v.indexOf("/")) : "";
};
showConnection();

let lastModel = modelSelect?.value;
modelSelect?.addEventListener("change", async () => {
  const chosen = modelSelect.selectedOptions[0];
  if (chosen?.dataset.add) {
    // "Other model on <connection>…": a model the server doesn't list but your key can use.
    const connection = chosen.dataset.add;
    const name = (prompt("Model name on " + connection + ", exactly as the server knows it (e.g. gpt-4o):") || "").trim();
    modelSelect.value = lastModel;
    if (!name) return;
    try {
      const { spec } = await api("/api/models/add", { connection, model: name });
      if (![...modelSelect.options].some(o => o.value === spec)) {
        const opt = document.createElement("option");
        opt.value = spec;
        opt.textContent = name;
        chosen.before(opt);
      }
      modelSelect.value = spec;
    } catch (err) { alert(err.message); return; }
  }
  lastModel = modelSelect.value;
  showConnection();
  try { localStorage.setItem(MODEL_KEY, lastModel); } catch { /* not remembered, still used */ }
});

// The page was reloaded while an answer was still being written: pick it up where it is.
if (initial.running && sessionId) {
  (async () => {
    setBusy(true);
    const res = await fetch("/api/chat/" + encodeURIComponent(sessionId) + "/stream");
    if (res.status === 200) {
      const turns = log.querySelectorAll(".turn");
      await follow(res, turns[turns.length - 1] || newTurn(""));
    } else {
      location.reload();
    }
    setBusy(false);
  })();
}

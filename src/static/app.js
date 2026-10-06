// Web chat client for the AI hub. History lives in this page only; "New chat" or a reload clears it.
const log = document.getElementById("log");
const form = document.getElementById("composer");
const input = document.getElementById("input");
const sendBtn = document.getElementById("send");
let history = [];
let busy = false;

function esc(s) {
  return s.replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Small, safe Markdown subset: everything is escaped first, then code fences, inline code and bold.
function render(md) {
  return md.split("```").map((part, i) => {
    if (i % 2 === 1) return "<pre><code>" + esc(part.replace(/^[\w-]*\n/, "")) + "</code></pre>";
    const html = esc(part)
      .replace(/`([^`\n]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
    return html.trim() ? '<div class="md">' + html.trim() + "</div>" : "";
  }).join("");
}

function el(tag, cls, html) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html !== undefined) n.innerHTML = html;
  return n;
}

function toolLine(name, args) {
  const [system, ...rest] = name.split("__");
  const action = rest.length ? rest.join("__") : system;
  const label = rest.length ? '<span class="sys">' + esc(system) + "</span> " + esc(action) : '<span class="sys">elastic</span> ' + esc(action);
  const detail = Object.keys(args || {}).length ? " <code>" + esc(JSON.stringify(args).slice(0, 300)) + "</code>" : "";
  return el("li", null, label + detail);
}

async function ask(message) {
  document.getElementById("empty")?.remove();
  const turn = el("section", "turn");
  turn.append(el("div", "ask", esc(message)));
  const trail = el("ul", "trail");
  turn.append(trail);
  const working = el("p", "working", "Working…");
  turn.append(working);
  log.append(turn);
  log.scrollTop = log.scrollHeight;

  let answer = null;
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Requested-With": "ai-hub" },
      body: JSON.stringify({ message, history }),
    });
    if (!res.ok) throw new Error("The hub returned HTTP " + res.status + ".");
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
        const data = JSON.parse((block.match(/^data: (.*)$/m) || [, "{}"])[1]);
        if (event === "tool") trail.append(toolLine(data.name, data.args));
        if (event === "answer") answer = data.text;
        if (event === "error") throw new Error(data.text);
        log.scrollTop = log.scrollHeight;
      }
    }
    if (answer === null) throw new Error("The connection closed before the hub sent an answer.");
    working.replaceWith(el("div", "answer", render(answer)));
    history.push({ role: "user", content: message }, { role: "assistant", content: answer });
  } catch (err) {
    working.replaceWith(el("p", "error", esc(err.message)));
  }
  log.scrollTop = log.scrollHeight;
}

form.addEventListener("submit", async e => {
  e.preventDefault();
  const message = input.value.trim();
  if (!message || busy) return;
  busy = true; sendBtn.disabled = true;
  input.value = ""; input.style.height = "";
  await ask(message);
  busy = false; sendBtn.disabled = false; input.focus();
});

input.addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
});
input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 192) + "px";
});

document.getElementById("new-chat").addEventListener("click", () => {
  if (busy) return;
  history = [];
  log.innerHTML = '<div class="empty" id="empty"><strong>Ask about your systems</strong>The hub queries Elasticsearch and every connected MCP server, and lists each tool it uses under your question.</div>';
  input.focus();
});

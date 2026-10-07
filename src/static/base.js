// Shared by every page: sidebar (chat search, delete, phone menu), confirmations, character counters.
const CSRF = document.querySelector('meta[name="csrf-token"]').content;

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function api(path, body) {
  const res = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": CSRF },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "The hub returned HTTP " + res.status + ".");
  return data;
}

// ----- phone menu -----
document.getElementById("menu-btn")?.addEventListener("click", () => document.body.classList.add("menu-open"));
document.getElementById("scrim")?.addEventListener("click", () => document.body.classList.remove("menu-open"));

// ----- chat list -----
const chatList = document.getElementById("chat-list");
const currentChat = () => document.querySelector("#chat-list li.active")?.dataset.id || null;

function chatItem(chat, active) {
  const li = document.createElement("li");
  li.dataset.id = chat.id;
  if (active) li.className = "active";
  li.innerHTML = '<a href="/c/' + encodeURIComponent(chat.id) + '">' + esc(chat.title) + "</a>" +
    '<button class="chat-delete" type="button" title="Delete chat" aria-label="Delete chat">&times;</button>';
  return li;
}

function renderChatList(chats) {
  const active = currentChat();
  chatList.replaceChildren(...chats.map(c => chatItem(c, c.id === active)));
  if (!chats.length) chatList.innerHTML = '<li class="chat-list-empty">No chats found</li>';
}

let searchTimer;
document.getElementById("chat-search")?.addEventListener("input", e => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(async () => {
    try {
      renderChatList(await api("/api/sessions?q=" + encodeURIComponent(e.target.value)));
    } catch (err) { console.error(err); }
  }, 200);
});

chatList?.addEventListener("click", async e => {
  const btn = e.target.closest(".chat-delete");
  if (!btn) return;
  const li = btn.closest("li");
  const title = li.querySelector("a").textContent;
  if (!confirm('Delete the chat "' + title + '"? This can\'t be undone.')) return;
  try {
    await api("/api/sessions/" + encodeURIComponent(li.dataset.id) + "/delete", {});
    if (li.classList.contains("active")) location.href = "/";
    else li.remove();
  } catch (err) { alert(err.message); }
});

// ----- forms that need a confirmation (e.g. delete skill) -----
document.querySelectorAll("form[data-confirm]").forEach(f =>
  f.addEventListener("submit", e => { if (!confirm(f.dataset.confirm)) e.preventDefault(); }));

// ----- character counters on the Memory page -----
document.querySelectorAll(".counter[data-for]").forEach(counter => {
  const area = document.getElementById(counter.dataset.for);
  const limit = Number(counter.dataset.limit);
  const update = () => {
    const n = area.value.trim().length;
    counter.textContent = n + " / " + limit;
    counter.classList.toggle("bad-text", n > limit);
  };
  area.addEventListener("input", update);
  update();
});

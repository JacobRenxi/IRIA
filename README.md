# IRIA: your AI hub

A self-hosted AI tool hub: a Flask web chat over your own model in Ollama, connected to your systems
through tools you switch on and off. Like [Hermes Agent](https://github.com/NousResearch/hermes-agent),
it also remembers what matters between chats and saves reusable skills as it works things out.

- **Chat**: answers stream in, with a live trail of every tool call (click a step to see its result).
  Chats are saved and searchable from the sidebar.
- **Tools**: every tool from every MCP server in `hub.yaml`, plus optional read-only Elasticsearch tools.
- **Memory**: `SOUL.md` (personality), `MEMORY.md` (your systems), `USER.md` (you). These go into every
  prompt. The assistant updates the last two itself; you can edit all three on the **Memory** page.
- **Skills**: step-by-step procedures in `data/skills/<name>/SKILL.md`. The assistant loads one when a
  task matches, and writes new ones after it solves something that will come up again.
- **Past chats**: the assistant can search your earlier conversations when you refer back to them.
- **Tools & status** page: every toolset (Memory, Skills, Past chats, Elasticsearch, and one per MCP
  server) with an **on/off switch**, and switches for single tools inside each one. Switched-off tools are
  hidden from the AI and refused if it tries one anyway. The page also shows whether the model is ready,
  which MCP servers connected (and why not), and has a **Reload MCP servers** button so you don't
  have to restart after editing `hub.yaml`.

## Run

```
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
cp .env.example .env          # set WEB_PASSWORD (12+ chars) and LLM_MODEL
ollama pull qwen3:8b          # or any model with "tools" under Capabilities in `ollama show <model>`
python src/app.py
```

Open http://localhost:8090 and log in with `WEB_USER` / `WEB_PASSWORD`. Stop it with Ctrl+C.

If the model can't call tools (e.g. `llama3`), the hub still works as a plain chat and shows a banner
telling you how to fix it. To reach the hub from other devices, set `WEB_HOST=0.0.0.0` and
`WEB_TLS_CERT` / `WEB_TLS_KEY`. Without HTTPS, your password can be read on the network.

## Add a system (MCP server)

1. Put its token in `.env`, e.g. `SPLUNK_TOKEN=...`.
2. Add the server to `hub.yaml` and reference the token as `${SPLUNK_TOKEN}` (examples are in the file).
3. On the **Tools & status** page click **Reload MCP servers**. It shows `connected` with a tool count, or why it failed.
4. New servers start switched on. Switch off the server, or single tools you don't want the AI to use.

## Files

```
.env / .env.example     all settings and tokens
hub.yaml                MCP servers to connect
data/                   created on first run: chats (hub.db), memory files, skills, tool switches
                        (tools.json). Back this up.
src/
├── app.py              Flask web app: login, pages, streaming chat API   <- start here
├── config.py           reads .env
├── agent.py            the tool-calling loop (streams from the model, runs tools, saves answers)
├── tools.py            every tool, grouped into toolsets with on/off switches
├── memory.py           SOUL.md / MEMORY.md / USER.md and the `memory` tool
├── skills.py           SKILL.md files and the `skill_view` / `skill_manage` tools
├── sessions.py         saved chats in SQLite with full-text search
├── mcp_hub.py          connects every MCP server in hub.yaml
├── elastic_tools.py    built-in read-only Elasticsearch tools
├── runtime.py          background event loop that keeps MCP connections open between requests
├── templates/          the pages (Jinja)
└── static/             style.css, base.js, chat.js, tools.js
```

## Notes

- The assistant can call every switched-on tool, including ones that change things. It's told to only do
  that when you ask. Switch off tools you don't want it to have, and give each token the least privilege
  that works: the token is the real limit.
- Small local models do better with fewer tools. If answers get confused, switch off toolsets you don't need.
- Review the **Memory** page now and then. Anything there is in every prompt.
- **Context size.** Every tool description, memory note and tool result counts against the model's
  context window. If answers ignore tools or forget the question, raise it. On a Mac use Ollama's
  settings (Context length), or run `launchctl setenv OLLAMA_CONTEXT_LENGTH 32768` and restart Ollama.
  On Linux run `sudo systemctl edit ollama`, add `[Service]` and `Environment="OLLAMA_CONTEXT_LENGTH=32768"`, then restart.
- Ollama has no authentication. Keep it bound to localhost and run the hub on the same machine.
- `python src/app.py` uses Flask's built-in server (it prints a "development server" warning). That's
  fine for personal use. Run a single process: MCP connections and running chats live in memory.

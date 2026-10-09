# IRIA: your AI hub

A self-hosted AI tool hub: a Flask web chat over any AI you connect (Ollama, LiteLLM, vLLM, OpenAI,
OpenRouter, anything OpenAI-compatible), wired to your systems through tools you switch on and off. Like [Hermes Agent](https://github.com/NousResearch/hermes-agent),
it also remembers what matters between chats and saves reusable skills as it works things out.

- **Chat**: answers stream in, with a live trail of every tool call (click a step to see its result).
  Chats are saved and searchable from the sidebar.
- **AI connections**: as many as you like, in `hub.yaml`. Switch models any time from the **Model** list
  at the top of the chat; each answer shows which model wrote it. Models that can't call tools are
  detected and used as plain chat.
- **Files and links**: drop files on the chat (or click the paperclip) and paste links in your message.
  Their text is given to the AI with every message in that chat; they show as chips above the message
  box (× removes one). See [Files and links](#files-and-links).
- **Tools**: every tool from every MCP server in `hub.yaml`, plus optional read-only Elasticsearch tools.
- **Memory**: `SOUL.md` (personality), `MEMORY.md` (your systems), `USER.md` (you). These go into every
  prompt. The assistant updates the last two itself; you can edit all three on the **Memory** page.
- **Skills**: step-by-step procedures in `data/skills/<name>/SKILL.md`. The assistant loads one when a
  task matches, and writes new ones after it solves something that will come up again.
- **Past chats**: the assistant can search your earlier conversations when you refer back to them.
- **Tools & status** page: every toolset (Memory, Skills, Past chats, Elasticsearch, and one per MCP
  server) with an **on/off switch**, and switches for single tools inside each one. Switched-off tools are
  hidden from the AI and refused if it tries one anyway. The page also shows every AI connection and its
  models, which MCP servers connected (and why not), and has a **Reload hub.yaml** button so you don't
  have to restart after editing `hub.yaml` or `.env`.

## Run

```
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
cp .env.example .env          # set WEB_PASSWORD (12+ chars)
# edit hub.yaml: your AI connections under providers: (local Ollama is there already)
python src/app.py
```

Open http://localhost:8090 and log in with `WEB_USER` / `WEB_PASSWORD`. Stop it with Ctrl+C.

For tools to be used, pick a model that can call them. The Tools & status page marks each model
`tools`, `chat only`, or `tools?` (the server doesn't say; the hub tries and falls back). To reach the hub from other devices, set `WEB_HOST=0.0.0.0` and
`WEB_TLS_CERT` / `WEB_TLS_KEY`. Without HTTPS, your password can be read on the network.

## Add an AI connection

Anything that speaks the OpenAI chat API works: Ollama, LiteLLM, vLLM, LM Studio, llama.cpp, OpenAI,
OpenRouter, Groq and more. For providers that don't (Anthropic, Bedrock, Vertex, Azure...), connect
them to a LiteLLM proxy and add LiteLLM here.

1. Put its key in `.env`, e.g. `LITELLM_API_KEY=sk-...`.
2. Add it under `providers:` in `hub.yaml` (examples are in the file):
   ```yaml
   providers:
     litellm:
       url: https://litellm.example.internal:4000/v1
       api_key: ${LITELLM_API_KEY}
   default_model: litellm/gpt-4o     # optional: what new chats start with
   ```
3. Click **Reload hub.yaml** on the **Tools & status** page. The connection shows `connected` with its
   models, or why not. Its models appear in the chat's model list.

Every model the server offers for your key is listed unless you add `models: [a, b]`. Do that for big
catalogs like OpenRouter or OpenAI.

**Switching and managing models in the web UI**
- **In a chat**: use the **Model** list at the top. The chat remembers its model, and new chats start
  with the one you picked last.
- **A model is missing** (some API keys may use models the server won't list): pick
  **Other model on <connection>…** in that list, or use **Add model** on Tools & status, and type the
  model's exact name. It's kept in the list from then on (remove it there).
- **Default for new chats**: **Make default** next to any model on Tools & status. This overrides
  `default_model` in `hub.yaml`. Choices made in the UI are saved in `data/models.json`.

## Files and links

- **Files**: text of any kind (txt, md, csv, json, log, yaml, code...), PDF and Word (`.docx`), up to
  `MAX_UPLOAD_MB` (25 MB). Excel and PowerPoint: save as CSV or PDF first. Scanned PDFs are pictures,
  not text, so they can't be read.
- **Links**: any link in your message is opened when you send it, and the page's text is attached to the
  chat (menus and footers left out). The AI can then open more pages **on the same site** with the
  `web_fetch` tool, for example the next page of a document, but no other sites, so a page can't
  talk it into sending your data somewhere. Pages that build their content with JavaScript give
  little text.
- **How much the AI sees**: all attachments of a chat together get `ATTACH_CONTEXT_CHARS` characters
  (30,000) in front of the AI with every message. Longer ones are cut there, and the AI reads the rest
  with the `file_read` and `file_search` tools (a model that can't call tools only sees the start).
  For small local models, lower `ATTACH_CONTEXT_CHARS` in `.env`.
- **Your own network**: links to `localhost`, `10.x`, `192.168.x` and similar addresses aren't opened.
  To read intranet pages, set `WEB_ALLOW_PRIVATE=1` in `.env` and restart.
- **Where it goes**: the text is stored in `data/files/` (deleted with the chat) and sent to the model
  the chat uses. If that's a cloud model (through LiteLLM, OpenAI...), the file leaves your network.

## Add a system (MCP server)

1. Put its token in `.env`, e.g. `SPLUNK_TOKEN=...`.
2. Add the server under `mcp_servers:` in `hub.yaml` and reference the token as `${SPLUNK_TOKEN}`.
3. Click **Reload hub.yaml** on the **Tools & status** page. It shows `connected` with a tool count, or why it failed.
4. New servers start switched on. Switch off the server, or single tools you don't want the AI to use.

## Files

```
.env / .env.example     all settings and tokens
hub.yaml                AI connections (providers:) and MCP servers (mcp_servers:)
data/                   created on first run: chats (hub.db), attached files' text (files/), memory, skills, tool switches
                        (tools.json), models added and default picked in the UI (models.json). Back this up.
src/
├── app.py              Flask web app: login, pages, streaming chat API   <- start here
├── config.py           reads .env and hub.yaml
├── agent.py            the tool-calling loop (streams from the model, runs tools, saves answers)
├── providers.py        AI connections: finds their models and whether they can call tools
├── tools.py            every tool, grouped into toolsets with on/off switches
├── memory.py           SOUL.md / MEMORY.md / USER.md and the `memory` tool
├── skills.py           SKILL.md files and the `skill_view` / `skill_manage` tools
├── sessions.py         saved chats in SQLite with full-text search
├── attachments.py      files attached to a chat: reading text out of them, file_read / file_search
├── web.py              links: reading web pages, web_fetch
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
- Older setups with `LLM_BASE_URL` / `LLM_MODEL` in `.env` and no `providers:` in `hub.yaml` still
  work: that becomes one connection named `default`.
- `python src/app.py` uses Flask's built-in server (it prints a "development server" warning). That's
  fine for personal use. Run a single process: MCP connections and running chats live in memory.

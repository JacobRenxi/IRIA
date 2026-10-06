# ai-hub

Web chat -> Ollama on your box -> any tools (built-in Elastic + every MCP server in `hub.yaml`) -> answer back in the chat.

```
ai-hub/
├── .env.example          # ALL tokens: web login, LiteLLM, Elastic, MCP servers
├── hub.yaml              # which MCP servers to connect (tokens referenced as ${VAR})
├── requirements.txt
└── src/
    ├── web.py            # web chat server (http(s)://<host>:8090)
    ├── static/
    │   ├── index.html    # chat page
    │   └── app.js        # chat client: history, tool trail, answers
    ├── agent.py          # Ollama tool-calling loop over all tools
    ├── mcp_hub.py        # connects every MCP server in hub.yaml, routes tool calls
    └── elastic_tools.py  # built-in read-only Elastic tools
```

## Run

```
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
cp .env.example .env    # fill it in; WEB_PASSWORD and LLM_MODEL are required
python src/web.py
```

Open `http://<host>:8090` and log in with `WEB_USER` / `WEB_PASSWORD`.
Run from the project root (`.env` and `hub.yaml` are read from the current directory).

Each answer shows the tools the hub called under your question. The chat keeps context until
you click **New chat** or reload the page.

## Add a new system

1. Put its token in `.env` (e.g. `SPLUNK_TOKEN=...`).
2. Add the server to `hub.yaml` and reference the token as `${SPLUNK_TOKEN}`.
3. Restart `src/web.py`. The log prints `MCP <name>: connected, N tools` or why it failed.

## Notes

- The AI can call every tool every configured server exposes, including ones that change things.
  Give each token the least privilege that's enough; a token is the real limit.
- Set `WEB_TLS_CERT` / `WEB_TLS_KEY` to serve HTTPS. Over plain HTTP, the Basic auth password
  can be read by anyone on the network path. Failed logins are logged with the source IP.
- `LLM_MODEL` must support tool calling: `ollama show <model>` lists `tools` under Capabilities.
- Context size: the OpenAI-style API can't set it per request. Every tool schema and tool result
  counts against it, so check `ollama ps` (CONTEXT column) and raise it on the Ollama server if needed:
  `sudo systemctl edit ollama` -> `[Service]` + `Environment="OLLAMA_CONTEXT_LENGTH=65536"`, then restart Ollama.
- Ollama has no authentication. Keep it bound to localhost and run the hub on the same box.

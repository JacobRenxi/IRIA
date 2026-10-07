"""Read-only Elasticsearch tools the model can call. On only when ELASTIC_URL and ELASTIC_API_KEY are set."""
import json

from elasticsearch import AsyncElasticsearch

from config import ELASTIC_API_KEY, ELASTIC_CA_CERTS, ELASTIC_URL, MAX_RESULT_CHARS

ENABLED = bool(ELASTIC_URL and ELASTIC_API_KEY)
MAX_ROWS = 200
_es: AsyncElasticsearch | None = None


def _client() -> AsyncElasticsearch:
    global _es
    if _es is None:  # created on the hub loop, the first time a tool runs
        _es = AsyncElasticsearch(
            ELASTIC_URL,
            api_key=ELASTIC_API_KEY,
            ca_certs=ELASTIC_CA_CERTS or None,
            request_timeout=60,
            node_class="httpxasync",  # uses httpx, so no aiohttp needed
        )
    return _es


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_indices",
            "description": "List Elasticsearch index, alias and data stream names matching a pattern.",
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string", "description": "e.g. 'logs-*'. Default '*'."}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_fields",
            "description": "List field names and their types for an Elasticsearch index, alias, data stream or pattern.",
            "parameters": {
                "type": "object",
                "properties": {"index": {"type": "string"}},
                "required": ["index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_esql",
            "description": "Run a read-only Elasticsearch ES|QL query. Must start with FROM and include LIMIT.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
] if ENABLED else []


def _clip(obj) -> str:
    text = json.dumps(obj, default=str)
    return text if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS] + " ...[truncated]"


async def list_indices(pattern: str = "*") -> str:
    resp = await _client().indices.resolve_index(name=pattern)
    return _clip({
        "indices": [i["name"] for i in resp.get("indices", [])],
        "aliases": [a["name"] for a in resp.get("aliases", [])],
        "data_streams": [d["name"] for d in resp.get("data_streams", [])],
    })


async def get_fields(index: str) -> str:
    resp = await _client().field_caps(index=index, fields="*")
    fields = {name: sorted(types) for name, types in resp["fields"].items() if not name.startswith("_")}
    return _clip(fields)


async def run_esql(query: str) -> str:
    q = query.strip()
    if not q.upper().startswith("FROM"):
        return "Rejected: query must start with FROM."
    resp = await _client().esql.query(query=q)
    cols = [c["name"] for c in resp["columns"]]
    rows = [dict(zip(cols, row)) for row in resp["values"][:MAX_ROWS]]
    return _clip({"row_count": len(resp["values"]), "rows": rows})


_TOOLS = {"list_indices": list_indices, "get_fields": get_fields, "run_esql": run_esql}


async def call_tool(name: str, args: dict) -> str:
    fn = _TOOLS.get(name)
    if fn is None or not ENABLED:
        return f"Unknown tool: {name}"
    try:
        return await fn(**args)
    except Exception as exc:  # returned to the model so it can fix its query
        return f"Tool error: {exc}"


async def status() -> dict:
    if not ENABLED:
        return {"enabled": False}
    try:
        info = await _client().info()
        return {"enabled": True, "ok": True, "detail": f"cluster {info['cluster_name']}, version {info['version']['number']}"}
    except Exception as exc:
        return {"enabled": True, "ok": False, "detail": f"{type(exc).__name__}: {exc}"[:300]}


async def close() -> None:
    if _es is not None:
        await _es.close()

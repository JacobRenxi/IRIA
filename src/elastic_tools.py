"""Read-only Elasticsearch tools the model can call."""
import json
import os

from elasticsearch import AsyncElasticsearch

es = AsyncElasticsearch(
    os.environ["ELASTIC_URL"],
    api_key=os.environ["ELASTIC_API_KEY"],
    ca_certs=os.getenv("ELASTIC_CA_CERTS") or None,
    request_timeout=60,
)

MAX_RESULT_CHARS = 12000  # keeps tool output inside the model's context
MAX_ROWS = 200

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_indices",
            "description": "List index, alias and data stream names matching a pattern.",
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
            "description": "List field names and their types for an index, alias, data stream or pattern.",
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
            "description": "Run a read-only ES|QL query. Must start with FROM and include LIMIT.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
]


def _clip(obj) -> str:
    text = json.dumps(obj, default=str)
    return text if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS] + " ...[truncated]"


async def list_indices(pattern: str = "*") -> str:
    resp = await es.indices.resolve_index(name=pattern)
    return _clip({
        "indices": [i["name"] for i in resp.get("indices", [])],
        "aliases": [a["name"] for a in resp.get("aliases", [])],
        "data_streams": [d["name"] for d in resp.get("data_streams", [])],
    })


async def get_fields(index: str) -> str:
    resp = await es.field_caps(index=index, fields="*")
    fields = {name: sorted(types) for name, types in resp["fields"].items() if not name.startswith("_")}
    return _clip(fields)


async def run_esql(query: str) -> str:
    q = query.strip()
    if not q.upper().startswith("FROM"):
        return "Rejected: query must start with FROM."
    resp = await es.esql.query(query=q)
    cols = [c["name"] for c in resp["columns"]]
    rows = [dict(zip(cols, row)) for row in resp["values"][:MAX_ROWS]]
    return _clip({"row_count": len(resp["values"]), "rows": rows})


_TOOLS = {"list_indices": list_indices, "get_fields": get_fields, "run_esql": run_esql}


async def call_tool(name: str, args: dict) -> str:
    fn = _TOOLS.get(name)
    if fn is None:
        return f"Unknown tool: {name}"
    try:
        return await fn(**args)
    except Exception as exc:  # returned to the model so it can fix its query
        return f"Tool error: {exc}"

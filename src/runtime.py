"""One background asyncio loop for all async work: MCP sessions, the model, Elasticsearch.

Flask serves each request in its own thread and is not async. MCP connections must stay open
between requests on a single event loop, so they live here and Flask hands work over to it."""
import asyncio
import concurrent.futures
import threading
from collections.abc import Coroutine

_loop = asyncio.new_event_loop()
_thread = threading.Thread(target=_loop.run_forever, name="hub-loop", daemon=True)


def start() -> None:
    if not _thread.is_alive():
        _thread.start()


def submit(coro: Coroutine) -> concurrent.futures.Future:
    """Schedule coro on the hub loop and return at once."""
    return asyncio.run_coroutine_threadsafe(coro, _loop)


def run(coro: Coroutine, timeout: float | None = None):
    """Run coro on the hub loop and wait for its result (from a Flask thread)."""
    return submit(coro).result(timeout)


def stop() -> None:
    if _thread.is_alive():
        _loop.call_soon_threadsafe(_loop.stop)
        _thread.join(timeout=5)

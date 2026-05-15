"""In-memory registry of active developer pipelines, keyed by user_id.

Used by the HTTP ping endpoint to push events into a live WebSocket session
without going through the user-facing websocket itself.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Dict

if TYPE_CHECKING:
    from .pipeline import SpeechPipeline

log = logging.getLogger("developer_ws")

_pipelines: Dict[str, "SpeechPipeline"] = {}
_loops: Dict[str, asyncio.AbstractEventLoop] = {}


def register(user_id: str, pipeline: "SpeechPipeline") -> None:
    """Called by `developer_websocket_endpoint` after constructing the pipeline."""
    _pipelines[user_id] = pipeline
    _loops[user_id] = asyncio.get_running_loop()
    log.info("registered user_id=%s (active=%d)", user_id, len(_pipelines))


def unregister(user_id: str) -> None:
    """Called by `developer_websocket_endpoint` finally block; always runs."""
    _pipelines.pop(user_id, None)
    _loops.pop(user_id, None)
    log.info("unregistered user_id=%s (active=%d)", user_id, len(_pipelines))


def get(user_id: str) -> "SpeechPipeline | None":
    """Called by the HTTP `/developer/ping/{user_id}` route in `app/main.py`.

    Returns None if there's no live WS session for that user (the route then
    responds with `{"ok": false, "reason": "no active session"}`).
    """
    return _pipelines.get(user_id)


def get_loop(user_id: str) -> "asyncio.AbstractEventLoop | None":
    """The event loop the pipeline was registered on. Currently unused; kept for
    future cross-thread schedulers that want to `run_coroutine_threadsafe(...)`."""
    return _loops.get(user_id)

"""Meaning-based intent matching (decision A1, ORCHESTRATOR_V2_TOOL_CALLS.md §1.3).

* Each agent's routing text is embedded once, at registration or backfill,
  and stored in `agent_info.routing_embedding` (with the model id, so a model
  change triggers re-embedding).
* Only requests that name no agent are embedded at route time, within a
  budget (`AGENT_ROUTER_EMBED_TIMEOUT_S`, 0.3 s). On failure or timeout the
  router simply uses lexical matching.

The backend is Gemini's embedding API through google-genai. Tests inject a
fake `Embedder`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections import OrderedDict
from typing import Optional, Protocol, Sequence

log = logging.getLogger("agent_router")

MODEL = os.environ.get("GEMINI_EMBEDDING_MODEL", "gemini-embedding-001")
DIMENSIONS = int(os.environ.get("AGENT_ROUTER_EMBED_DIM", "768"))


def _timeout_s() -> float:
    try:
        return float(os.environ.get("AGENT_ROUTER_EMBED_TIMEOUT_S", "0.3"))
    except ValueError:
        return 0.3


def enabled() -> bool:
    return os.environ.get("AGENT_ROUTER_EMBEDDINGS", "1").strip().lower() not in ("0", "false", "no", "off")


class Embedder(Protocol):
    model: str

    def embed(self, texts: Sequence[str], *, query: bool) -> list[list[float]]: ...


class GeminiEmbedder:
    def __init__(self, model: str = MODEL, dimensions: int = DIMENSIONS) -> None:
        self.model = model
        self._dims = dimensions
        self._client = None
        self._lock = threading.Lock()

    def _get_client(self):
        with self._lock:
            if self._client is None:
                from google import genai

                key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
                if not key:
                    raise RuntimeError("no GEMINI_API_KEY for embeddings")
                self._client = genai.Client(api_key=key)
            return self._client

    def embed(self, texts: Sequence[str], *, query: bool) -> list[list[float]]:
        from google.genai import types

        resp = self._get_client().models.embed_content(
            model=self.model,
            contents=list(texts),
            config=types.EmbedContentConfig(
                task_type="RETRIEVAL_QUERY" if query else "RETRIEVAL_DOCUMENT",
                output_dimensionality=self._dims,
            ),
        )
        return [list(e.values or []) for e in (resp.embeddings or [])]


_embedder: Optional[Embedder] = None
_query_cache: "OrderedDict[str, list[float]]" = OrderedDict()
_CACHE_MAX = 256


def get_embedder() -> Optional[Embedder]:
    global _embedder
    if not enabled():
        return None
    if _embedder is None:
        _embedder = GeminiEmbedder()
    return _embedder


def set_embedder(embedder: Optional[Embedder]) -> None:
    """Test hook."""
    global _embedder
    _embedder = embedder
    _query_cache.clear()


def routing_text(agent: dict) -> str:
    """The text that describes what an agent is for (registry-normalized row)."""
    parts = [agent.get("name") or "", agent.get("description") or ""]
    for key in ("domains", "intent_aliases", "user_intents", "keywords", "capabilities"):
        vals = agent.get(key) or []
        if vals:
            parts.append(", ".join(str(v) for v in vals))
    return ". ".join(p for p in parts if p).strip()


def embed_agent(agent: dict) -> Optional[dict]:
    """Embed one agent's routing text. Returns the fields to merge into agent_info."""
    emb = get_embedder()
    text = routing_text(agent)
    if emb is None or not text:
        return None
    try:
        vec = emb.embed([text], query=False)[0]
    except Exception as e:
        log.warning("embedding agent %r failed: %s", agent.get("name"), e)
        return None
    if not vec:
        return None
    return {"routing_embedding": [round(x, 6) for x in vec], "routing_embedding_model": emb.model}


async def embed_query(text: str) -> Optional[list[float]]:
    """Embed a user request within the latency budget, or return None."""
    emb = get_embedder()
    text = (text or "").strip()
    if emb is None or not text:
        return None
    key = text.lower()
    if key in _query_cache:
        _query_cache.move_to_end(key)
        return _query_cache[key]
    try:
        vecs = await asyncio.wait_for(asyncio.to_thread(emb.embed, [text], query=True), timeout=_timeout_s())
    except asyncio.TimeoutError:
        log.info("query embedding timed out (%.2fs); lexical routing only", _timeout_s())
        return None
    except Exception as e:
        log.info("query embedding failed: %s; lexical routing only", e)
        return None
    if not vecs or not vecs[0]:
        return None
    _query_cache[key] = vecs[0]
    while len(_query_cache) > _CACHE_MAX:
        _query_cache.popitem(last=False)
    return vecs[0]

"""In-memory agent router: resolves what the user *said* to a registered agent.

Why this exists
---------------
The first version of the orchestrator routed by pasting every registered agent
into Gemini's system prompt and running `LIKE` + `difflib` over the whole
`agents` table on each call. That works for a dozen agents and falls over at a
thousand: the prompt blows past any sane token budget, every tool call pays a
full table scan plus a fresh DB connection, and one slow/unavailable database
turns into a hung voice turn.

This module replaces that with a small, dependency-free retrieval layer:

  * **Snapshot cache.** The active agents are loaded once into memory and
    refreshed on a TTL (`AGENT_ROUTER_REFRESH_S`, default 30s) or immediately
    when a registry write calls `invalidate()`. A failed refresh keeps serving
    the last good snapshot, so a DB blip never breaks routing.
  * **Indexes built at load time.** Per agent we precompute a normalized name,
    name tokens, a phonetic key (STT garbles out-of-vocabulary names — "Kairos"
    arrives as "cut in" / "cairo's" / "kai ross"), and a token inverted index
    over name + description + keywords + capabilities + user_intents with IDF
    weights for intent matching.
  * **Cheap scoring.** Candidate generation is mostly index-driven (exact ids,
    name tokens, phonetic keys, intent tokens). The only passes over all agents
    are plain string prefix/containment checks on names and difflib's
    `get_close_matches`; at a few thousand agents a lookup is a few ms. Nothing
    grows the LLM prompt with the registry size.
  * **Decisions, not just scores.** `resolve()` returns MATCHED / AMBIGUOUS /
    NONE so the caller can do the right thing for a voice UI: dial, ask "did
    you mean X or Y?", or admit no such agent exists — instead of silently
    dialing the wrong (or default) agent. (See When2Call, Ross et al. 2025:
    the hard part of tool use is deciding *whether* to act, ask, or decline.)
  * **Health.** A tiny circuit breaker per agent: after
    `AGENT_ROUTER_FAILURE_THRESHOLD` consecutive dial failures the agent is
    skipped for `AGENT_ROUTER_COOLDOWN_S` seconds, so a dead tunnel doesn't
    eat every call while a healthy sibling waits.

The module is pure Python and does not import the DB layer at import time, so
it is unit-testable with synthetic agents (see test/app/developer/test_agent_router.py).
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import math
import os
import re
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

log = logging.getLogger("agent_router")

# ---------------------------------------------------------------------------
# Tunables (env, read once at import; tests override via constructor kwargs)
# ---------------------------------------------------------------------------

REFRESH_S = float(os.environ.get("AGENT_ROUTER_REFRESH_S", "30"))
FAILURE_THRESHOLD = int(os.environ.get("AGENT_ROUTER_FAILURE_THRESHOLD", "3"))
COOLDOWN_S = float(os.environ.get("AGENT_ROUTER_COOLDOWN_S", "60"))
# Score floor below which a candidate is not considered a match at all.
MATCH_FLOOR = float(os.environ.get("AGENT_ROUTER_MATCH_FLOOR", "0.55"))
# If the runner-up is within this fraction of the winner, we call it ambiguous
# rather than guessing. 0 disables ambiguity detection.
AMBIGUITY_MARGIN = float(os.environ.get("AGENT_ROUTER_AMBIGUITY_MARGIN", "0.12"))
# A top score at or above this is confident regardless of the runner-up.
CONFIDENT_SCORE = float(os.environ.get("AGENT_ROUTER_CONFIDENT_SCORE", "0.92"))

MODE_BRIDGE = "bridge"
MODE_TASK = "task"

DECISION_MATCHED = "matched"
DECISION_AMBIGUOUS = "ambiguous"
DECISION_NONE = "none"
# The agent the user named exists but doesn't support the requested mode
# (e.g. a task-only agent asked to take a live call). `wrong_mode` holds it.
DECISION_WRONG_MODE = "wrong_mode"

_STOPWORDS = frozenset(
    "a an the to for of and or with my me i you your please can could would "
    "like want need call connect talk speak dial agent service bot the this "
    "that it is are be do does about on in at from up hey hi ok okay".split()
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """Lowercase alnum tokens with stopwords removed. Keeps order + duplicates."""
    if not text:
        return []
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS]


def normalize_name(name: str) -> str:
    """Canonical comparable form of an agent name: lowercase, alnum + single spaces."""
    return " ".join(_TOKEN_RE.findall((name or "").lower()))


def phonetic_key(text: str) -> str:
    """A compact consonant-skeleton key so STT near-misses collide.

    Not Metaphone — deliberately simple: strip vowels (except a leading one),
    collapse common confusions (c/k/q → k, z → s, ph → f, gh → g, ...), drop
    doubles. "kairos" → "krs", "cairo's" → "krs", "kai ross" → "krs".
    """
    s = re.sub(r"[^a-z]", "", (text or "").lower())
    if not s:
        return ""
    s = (
        s.replace("ph", "f").replace("gh", "g").replace("ck", "k")
        .replace("wr", "r").replace("kn", "n").replace("wh", "w")
        .replace("tch", "ch").replace("sch", "sk")
    )
    out = []
    for i, ch in enumerate(s):
        if ch in "aeiouyhw":
            if i == 0 and ch in "aeiou":
                out.append("a")
            continue
        if ch in "ckq":
            ch = "k"
        elif ch == "z":
            ch = "s"
        elif ch == "v":
            ch = "f"
        elif ch == "d":
            ch = "t"
        elif ch == "b":
            ch = "p"
        elif ch == "g":
            ch = "k"
        if out and out[-1] == ch:
            continue
        out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentRecord:
    """One routable agent, as the router sees it (normalized registry row)."""

    id: str
    name: str
    url: str
    description: str = ""
    keywords: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    user_intents: tuple[str, ...] = ()
    service_id: Optional[str] = None
    modes: tuple[str, ...] = (MODE_BRIDGE,)
    max_concurrency: int = 8
    version: str = "1"

    @staticmethod
    def from_registry_row(row: dict) -> "AgentRecord":
        modes = row.get("modes") or [MODE_BRIDGE]
        try:
            max_conc = int(row.get("max_concurrency") or 8)
        except (TypeError, ValueError):
            max_conc = 8
        return AgentRecord(
            id=str(row.get("id") or ""),
            name=str(row.get("name") or "").strip(),
            url=str(row.get("url") or "").strip(),
            description=str(row.get("description") or "").strip(),
            keywords=tuple(str(k) for k in (row.get("keywords") or []) if k),
            capabilities=tuple(str(c) for c in (row.get("capabilities") or []) if c),
            user_intents=tuple(str(u) for u in (row.get("user_intents") or []) if u),
            service_id=(str(row["service_id"]) if row.get("service_id") else None),
            modes=tuple(str(m).lower() for m in modes if m) or (MODE_BRIDGE,),
            max_concurrency=max(1, max_conc),
            version=str(row.get("version") or "1"),
        )

    def supports(self, mode: str) -> bool:
        return mode in self.modes

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "service_id": self.service_id,
            "modes": list(self.modes),
        }


@dataclass
class Candidate:
    agent: AgentRecord
    score: float
    reasons: tuple[str, ...] = ()

    def to_public(self) -> dict:
        d = self.agent.to_public()
        d["score"] = round(self.score, 3)
        d["reasons"] = list(self.reasons)
        return d


@dataclass
class RouteResult:
    """Outcome of `AgentRouter.resolve`.

    decision: MATCHED (use `best`), AMBIGUOUS (ask the user; `candidates` are
    the close contenders), NONE (nothing plausible; `candidates` may still hold
    weak suggestions for a "did you mean" hint).
    """

    decision: str
    query: str
    candidates: list[Candidate] = field(default_factory=list)
    wrong_mode: Optional[AgentRecord] = None

    @property
    def best(self) -> Optional[AgentRecord]:
        return self.candidates[0].agent if self.candidates else None

    @property
    def matched(self) -> bool:
        return self.decision == DECISION_MATCHED

    def names(self, n: int = 3) -> list[str]:
        return [c.agent.name for c in self.candidates[:n]]


@dataclass
class _Index:
    """Everything derived from one registry snapshot. Immutable after build."""

    agents: list[AgentRecord]
    by_id: dict[str, AgentRecord]
    by_service_id: dict[str, AgentRecord]
    by_norm_name: dict[str, list[AgentRecord]]
    by_phonetic: dict[str, list[AgentRecord]]
    name_tokens: dict[str, set[str]]           # token -> agent ids
    doc_tokens: dict[str, set[str]]            # token -> agent ids (all fields)
    doc_len: dict[str, int]                    # agent id -> token count
    idf: dict[str, float]
    norm_names: list[str]
    norm_name_to_ids: dict[str, list[str]]
    built_at: float

    @staticmethod
    def empty() -> "_Index":
        return _Index(
            agents=[], by_id={}, by_service_id={}, by_norm_name={}, by_phonetic={},
            name_tokens={}, doc_tokens={}, doc_len={}, idf={}, norm_names=[],
            norm_name_to_ids={}, built_at=time.monotonic(),
        )


def _build_index(agents: Iterable[AgentRecord]) -> _Index:
    agents = [a for a in agents if a.name and a.url]
    by_id: dict[str, AgentRecord] = {}
    by_service_id: dict[str, AgentRecord] = {}
    by_norm_name: dict[str, list[AgentRecord]] = defaultdict(list)
    by_phonetic: dict[str, list[AgentRecord]] = defaultdict(list)
    name_tokens: dict[str, set[str]] = defaultdict(set)
    doc_tokens: dict[str, set[str]] = defaultdict(set)
    doc_len: dict[str, int] = {}
    df: dict[str, int] = defaultdict(int)

    for a in agents:
        by_id[a.id] = a
        if a.service_id:
            by_service_id[a.service_id.lower()] = a
        norm = normalize_name(a.name)
        by_norm_name[norm].append(a)
        pk = phonetic_key(norm)
        if pk:
            by_phonetic[pk].append(a)
        for tok in set(tokenize(a.name)):
            name_tokens[tok].add(a.id)
            # Phonetic key of each name token, so "ross" ~ "rose" also collide.
            tpk = phonetic_key(tok)
            if tpk and tpk != pk:
                by_phonetic[tpk].append(a)
        doc = " ".join([
            a.name, a.name, a.description,  # name counted twice: it matters more
            " ".join(a.keywords), " ".join(a.capabilities), " ".join(a.user_intents),
        ])
        toks = tokenize(doc)
        doc_len[a.id] = max(1, len(toks))
        for tok in set(toks):
            doc_tokens[tok].add(a.id)
            df[tok] += 1

    n = max(1, len(agents))
    idf = {tok: math.log(1.0 + n / (1.0 + c)) for tok, c in df.items()}
    norm_names = sorted(by_norm_name.keys())
    return _Index(
        agents=agents, by_id=by_id, by_service_id=by_service_id,
        by_norm_name=dict(by_norm_name), by_phonetic=dict(by_phonetic),
        name_tokens=dict(name_tokens), doc_tokens=dict(doc_tokens),
        doc_len=doc_len, idf=idf, norm_names=norm_names,
        norm_name_to_ids={k: [a.id for a in v] for k, v in by_norm_name.items()},
        built_at=time.monotonic(),
    )


# ---------------------------------------------------------------------------
# Health (circuit breaker)
# ---------------------------------------------------------------------------


@dataclass
class _Health:
    consecutive_failures: int = 0
    open_until: float = 0.0
    last_failure_reason: str = ""
    last_success_at: float = 0.0


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

Loader = Callable[[], list[dict]]


class AgentRouter:
    """Thread-safe, lazily-refreshing router over the agent registry.

    `loader` returns normalized registry rows (the shape `agents_registry.list_agents`
    produces). The default loader imports it lazily so this module stays importable
    without a database.
    """

    def __init__(
        self,
        loader: Optional[Loader] = None,
        *,
        refresh_s: float = REFRESH_S,
        failure_threshold: int = FAILURE_THRESHOLD,
        cooldown_s: float = COOLDOWN_S,
        match_floor: float = MATCH_FLOOR,
        ambiguity_margin: float = AMBIGUITY_MARGIN,
        confident_score: float = CONFIDENT_SCORE,
    ) -> None:
        self._loader = loader or _default_loader
        self._refresh_s = refresh_s
        self._failure_threshold = max(1, failure_threshold)
        self._cooldown_s = cooldown_s
        self._match_floor = match_floor
        self._ambiguity_margin = ambiguity_margin
        self._confident_score = confident_score

        self._index: _Index = _Index.empty()
        self._loaded_once = False
        self._dirty = True
        # After a failed load, don't retry before this (monotonic) time — keeps
        # a down database from being hit (and blocking) on every lookup.
        self._retry_after = 0.0
        self._last_refresh_error: Optional[str] = None
        self._lock = threading.Lock()
        self._refreshing = False
        self._health: dict[str, _Health] = defaultdict(_Health)

    # ----- snapshot management ------------------------------------------

    def invalidate(self) -> None:
        """Mark the snapshot stale; the next lookup reloads. Called on registry writes."""
        self._dirty = True

    def load_now(self) -> int:
        """Synchronous (re)load. Returns the number of routable agents.

        Safe to call from a thread. On loader failure the previous snapshot is
        kept and the error is remembered for `stats()`.
        """
        with self._lock:
            if self._refreshing:
                return len(self._index.agents)
            self._refreshing = True
        try:
            rows = self._loader()
            records = []
            for r in rows or []:
                try:
                    rec = AgentRecord.from_registry_row(r)
                except Exception:
                    log.exception("router: skipping malformed registry row %r", r)
                    continue
                if rec.name and rec.url:
                    records.append(rec)
            new_index = _build_index(records)
            with self._lock:
                self._index = new_index
                self._loaded_once = True
                self._dirty = False
                self._last_refresh_error = None
                self._retry_after = 0.0
            log.info("router: snapshot loaded agents=%d", len(new_index.agents))
            return len(new_index.agents)
        except Exception as e:
            with self._lock:
                self._last_refresh_error = str(e)
                # Don't hammer a failing DB: back off before the next attempt.
                self._dirty = False
                self._retry_after = time.monotonic() + min(self._refresh_s, 5.0)
                self._index.built_at = time.monotonic()
            log.warning("router: refresh failed (serving stale snapshot): %s", e)
            return len(self._index.agents)
        finally:
            with self._lock:
                self._refreshing = False

    def _needs_refresh(self) -> bool:
        now = time.monotonic()
        if now < self._retry_after:
            return False
        if self._dirty or not self._loaded_once:
            return True
        return (now - self._index.built_at) > self._refresh_s

    def ensure_fresh_nonblocking(self) -> None:
        """For sync callers on the event loop: never block on a *re*load.

        The very first load is synchronous (there is nothing to serve yet);
        after that a stale snapshot is served while a daemon thread refreshes.
        """
        if not self._needs_refresh():
            return
        if not self._loaded_once:
            self.load_now()
            return
        threading.Thread(target=self.load_now, name="agent-router-refresh", daemon=True).start()

    def ensure_fresh(self) -> None:
        """Synchronously refresh if stale. Cheap when fresh."""
        if self._needs_refresh():
            self.load_now()

    async def ensure_fresh_async(self) -> None:
        """Refresh off the event loop if stale (DB I/O in a worker thread)."""
        if self._needs_refresh():
            await asyncio.to_thread(self.load_now)

    @property
    def index(self) -> _Index:
        return self._index

    def agents(self) -> list[AgentRecord]:
        return list(self._index.agents)

    def get(self, agent_id: str) -> Optional[AgentRecord]:
        return self._index.by_id.get(agent_id)

    def stats(self) -> dict:
        idx = self._index
        open_circuits = [
            aid for aid, h in self._health.items() if h.open_until > time.monotonic()
        ]
        return {
            "agents": len(idx.agents),
            "task_capable": sum(1 for a in idx.agents if a.supports(MODE_TASK)),
            "snapshot_age_s": round(time.monotonic() - idx.built_at, 1),
            "refresh_interval_s": self._refresh_s,
            "stale": self._needs_refresh(),
            "last_refresh_error": self._last_refresh_error,
            "open_circuits": open_circuits,
        }

    # ----- health -----------------------------------------------------------

    def report_failure(self, agent_id: str, reason: str = "") -> None:
        h = self._health[agent_id]
        h.consecutive_failures += 1
        h.last_failure_reason = reason
        if h.consecutive_failures >= self._failure_threshold:
            h.open_until = time.monotonic() + self._cooldown_s
            log.warning(
                "router: circuit OPEN agent_id=%s failures=%d cooldown=%.0fs reason=%s",
                agent_id, h.consecutive_failures, self._cooldown_s, reason,
            )

    def report_success(self, agent_id: str) -> None:
        h = self._health[agent_id]
        h.consecutive_failures = 0
        h.open_until = 0.0
        h.last_success_at = time.monotonic()

    def is_healthy(self, agent_id: str) -> bool:
        h = self._health.get(agent_id)
        return h is None or h.open_until <= time.monotonic()

    def health(self, agent_id: str) -> dict:
        h = self._health.get(agent_id) or _Health()
        return {
            "consecutive_failures": h.consecutive_failures,
            "circuit_open": h.open_until > time.monotonic(),
            "last_failure_reason": h.last_failure_reason,
        }

    # ----- resolution -------------------------------------------------------

    def resolve(
        self,
        selector: str = "",
        intent: str = "",
        *,
        mode: str = MODE_BRIDGE,
        k: int = 5,
        include_unhealthy: bool = False,
    ) -> RouteResult:
        """Resolve a spoken agent name and/or an intent to registered agents.

        `selector` is what the user called the agent (may be STT-garbled, may be
        a service_id). `intent` is what they want done (free text). Either may be
        empty. `mode` filters to agents supporting bridge or task.
        Never raises; on an empty registry returns DECISION_NONE.
        """
        self.ensure_fresh()
        idx = self._index
        sel_raw = (selector or "").strip()
        sel = normalize_name(sel_raw)
        query = sel_raw or (intent or "").strip()
        result = RouteResult(decision=DECISION_NONE, query=query)
        if not idx.agents:
            return result

        scores: dict[str, float] = defaultdict(float)
        reasons: dict[str, list[str]] = defaultdict(list)

        def bump(aid: str, s: float, why: str) -> None:
            if s > scores[aid]:
                scores[aid] = s
            reasons[aid].append(why)

        if sel_raw:
            # 1. Exact service_id / exact normalized name.
            a = idx.by_service_id.get(sel_raw.lower())
            if a:
                bump(a.id, 1.0, "service_id")
            for a in idx.by_norm_name.get(sel, []):
                bump(a.id, 1.0, "exact_name")

            # 2. Name prefix / containment ("weather" for "weather bot").
            if sel:
                for norm in idx.norm_names:
                    if norm == sel:
                        continue
                    if norm.startswith(sel + " ") or norm.startswith(sel):
                        for aid in idx.norm_name_to_ids[norm]:
                            bump(aid, 0.9, "name_prefix")
                    elif (" " + sel + " ") in (" " + norm + " "):
                        for aid in idx.norm_name_to_ids[norm]:
                            bump(aid, 0.85, "name_contains")
                    elif sel in norm:
                        for aid in idx.norm_name_to_ids[norm]:
                            bump(aid, 0.7, "name_substring")

            # 3. Phonetic collisions (whole selector and per-token).
            sel_pk = phonetic_key(sel)
            if sel_pk:
                for a in idx.by_phonetic.get(sel_pk, []):
                    bump(a.id, 0.75, "phonetic")
            sel_toks = tokenize(sel_raw)
            for tok in sel_toks:
                tpk = phonetic_key(tok)
                if tpk and len(tpk) >= 2:
                    for a in idx.by_phonetic.get(tpk, []):
                        bump(a.id, 0.5, "phonetic_token")
                for aid in idx.name_tokens.get(tok, ()):
                    bump(aid, 0.6, "name_token")

            # 4. Fuzzy similarity on names — over the candidate set first, then
            #    a bounded global pass (difflib's own ngram indexing is fast for
            #    a few thousand short strings).
            cand_norms = {normalize_name(idx.by_id[aid].name) for aid in list(scores)}
            for norm in cand_norms:
                r = difflib.SequenceMatcher(None, sel, norm).ratio()
                if r >= 0.5:
                    for aid in idx.norm_name_to_ids.get(norm, []):
                        bump(aid, max(0.5, r * 0.95), "fuzzy")
            if sel:
                for norm in difflib.get_close_matches(sel, idx.norm_names, n=8, cutoff=0.55):
                    r = difflib.SequenceMatcher(None, sel, norm).ratio()
                    for aid in idx.norm_name_to_ids.get(norm, []):
                        bump(aid, max(0.5, r * 0.95), "fuzzy_global")

        # 5. Intent match over descriptions/keywords/intents (TF-IDF-ish).
        intent_text = " ".join(x for x in (intent, sel_raw if not scores else "") if x)
        intent_toks = set(tokenize(intent_text))
        if intent_toks:
            hits: dict[str, float] = defaultdict(float)
            max_possible = sum(idx.idf.get(t, 0.0) for t in intent_toks) or 1.0
            for tok in intent_toks:
                w = idx.idf.get(tok)
                if not w:
                    continue
                for aid in idx.doc_tokens.get(tok, ()):
                    hits[aid] += w
            for aid, s in hits.items():
                frac = s / max_possible
                # Intent alone caps below "confident" so a pure description hit
                # never outranks an exact name; combined with a name hit it lifts.
                intent_score = 0.35 + 0.5 * min(1.0, frac)
                if aid in scores:
                    scores[aid] = min(1.0, scores[aid] + 0.1 * frac)
                    reasons[aid].append("intent_boost")
                else:
                    bump(aid, intent_score, "intent")

        # 6. Filter by mode + health, rank.
        ranked: list[Candidate] = []
        best_wrong_mode: Optional[tuple[float, AgentRecord]] = None
        for aid, s in scores.items():
            a = idx.by_id.get(aid)
            if a is None:
                continue
            if not a.supports(mode):
                if s >= self._confident_score and (
                    best_wrong_mode is None or s > best_wrong_mode[0]
                ):
                    best_wrong_mode = (s, a)
                continue
            if not include_unhealthy and not self.is_healthy(aid):
                s *= 0.5  # keep visible but deprioritised
                reasons[aid].append("unhealthy")
            ranked.append(Candidate(agent=a, score=min(1.0, s), reasons=tuple(dict.fromkeys(reasons[aid]))))
        ranked.sort(key=lambda c: (-c.score, c.agent.name.lower()))
        result.candidates = ranked[: max(1, k)]

        if best_wrong_mode is not None and (
            not ranked or ranked[0].score < self._confident_score
        ):
            # The user clearly named an agent; it just can't do this. Say so
            # rather than silently routing to a look-alike.
            result.decision = DECISION_WRONG_MODE
            result.wrong_mode = best_wrong_mode[1]
            return result

        if not ranked or ranked[0].score < self._match_floor:
            result.decision = DECISION_NONE
            return result

        top = ranked[0].score
        second = ranked[1].score if len(ranked) > 1 else 0.0
        if top >= self._confident_score and second < top - 1e-9:
            # A single exact/near-exact hit wins outright. Two equal exact hits
            # (duplicate names) fall through to the ambiguity check.
            result.decision = DECISION_MATCHED
        elif (
            self._ambiguity_margin > 0
            and second >= self._match_floor
            and (top - second) <= self._ambiguity_margin
        ):
            result.decision = DECISION_AMBIGUOUS
        else:
            result.decision = DECISION_MATCHED
        return result

    def search(self, query: str, *, mode: Optional[str] = None, k: int = 5) -> list[Candidate]:
        """Discovery: rank agents for a free-text query ("who can book flights?")."""
        res = self.resolve(selector=query, intent=query, mode=mode or MODE_BRIDGE, k=k)
        if mode is None:
            # Union across modes: resolve() filtered to bridge; add task-only agents.
            seen = {c.agent.id for c in res.candidates}
            extra = self.resolve(selector=query, intent=query, mode=MODE_TASK, k=k).candidates
            merged = res.candidates + [c for c in extra if c.agent.id not in seen]
            merged.sort(key=lambda c: -c.score)
            return merged[:k]
        return res.candidates

    def resolve_url(self, selector: str, *, mode: str = MODE_BRIDGE) -> Optional[str]:
        """Back-compat shim for `agents_registry.resolve_bridge_url`: URL of the
        best confident match, else None."""
        res = self.resolve(selector, mode=mode)
        if res.decision == DECISION_MATCHED and res.best:
            return res.best.url
        # Ambiguous with one clearly-best name still resolves (legacy behaviour
        # preferred *some* agent over the env default). Callers that want to ask
        # the user should use resolve() directly.
        if res.decision == DECISION_AMBIGUOUS and res.best:
            return res.best.url
        return None

    def prompt_summary(self, limit: int = 30) -> str:
        """Compact description of the registry for the system prompt.

        Lists up to `limit` agents (name + one-line description); beyond that
        only states the count so the prompt size is bounded no matter how many
        agents are registered. Called on the session-start path, so it never
        blocks on a reload (see `ensure_fresh_nonblocking`).
        """
        self.ensure_fresh_nonblocking()
        agents = self._index.agents
        if not agents:
            return ""
        n = len(agents)
        lines: list[str] = []
        if n <= limit:
            lines.append(
                f"Registered agents ({n}). Pass the agent's name as you heard it as "
                "`agent`; the orchestrator resolves it, including speech-to-text garbles:"
            )
            for a in sorted(agents, key=lambda x: x.name.lower()):
                desc = a.description.split("\n")[0][:120]
                modes = "" if a.modes == (MODE_BRIDGE,) else f" [{', '.join(a.modes)}]"
                lines.append(f"- {a.name}{modes}: {desc}" if desc else f"- {a.name}{modes}")
        else:
            lines.append(
                f"There are {n} registered agents — too many to list. Do NOT guess "
                "names. Pass what the user said as `agent` (and what they want as "
                "`intent`); the orchestrator resolves it. Use find_agents to look up "
                "which agents exist for a purpose before claiming one does or doesn't."
            )
        return "\n".join(lines)


def _default_loader() -> list[dict]:
    import agents_registry  # local import: keeps this module DB-free at import

    return agents_registry.list_agents(active_only=True)


# Process-wide singleton (one uvicorn worker per container; see Dockerfile).
_router: Optional[AgentRouter] = None
_router_lock = threading.Lock()


def get_router() -> AgentRouter:
    global _router
    if _router is None:
        with _router_lock:
            if _router is None:
                _router = AgentRouter()
    return _router


def set_router(router: Optional[AgentRouter]) -> None:
    """Test hook: swap the process-wide router."""
    global _router
    _router = router


def invalidate() -> None:
    """Registry write hook: force the next lookup to reload."""
    if _router is not None:
        _router.invalidate()

"""In-memory agent router: resolves what the user said to a registered agent.

Spec: ORCHESTRATOR_V2_TOOL_CALLS.md §1.3.

* **Snapshot.** Active agents are loaded into memory and refreshed every
  `AGENT_ROUTER_REFRESH_S` (30 s) or right after a registry write calls
  `invalidate()`. A failed refresh keeps serving the last good snapshot, so a
  database blip never blocks a voice turn.
* **Signals.** Exact name / service_id, name prefix and containment, a phonetic
  key (STT turns "Kairos" into "cairo's" or "kai ross"), name tokens, fuzzy
  name similarity, curated `intent_aliases`, lexical intent (IDF over the
  description, keywords, user_intents and domains), and, for requests that
  name no agent, embedding similarity. Each signal raises a candidate's score
  to at least its value; they don't add up.
* **Decisions, not guesses.** `resolve()` returns matched / ambiguous / none /
  wrong_mode so the caller can connect, ask, decline or offer the other mode.
* **Health.** A small circuit breaker per agent ranks failing agents lower.

Pure Python; the database loader is imported lazily so tests run without it.
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
from typing import Any, Callable, Iterable, Optional, Sequence

from orchestrator.tasks.protocol import DEFAULT_TASK_OPS, MODE_BRIDGE, MODE_TASK

log = logging.getLogger("agent_router")


def _env_f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


REFRESH_S = _env_f("AGENT_ROUTER_REFRESH_S", 30)
FAILURE_THRESHOLD = int(_env_f("AGENT_ROUTER_FAILURE_THRESHOLD", 3))
COOLDOWN_S = _env_f("AGENT_ROUTER_COOLDOWN_S", 60)
MATCH_FLOOR = _env_f("AGENT_ROUTER_MATCH_FLOOR", 0.55)
AMBIGUITY_MARGIN = _env_f("AGENT_ROUTER_AMBIGUITY_MARGIN", 0.12)
CONFIDENT_SCORE = _env_f("AGENT_ROUTER_CONFIDENT_SCORE", 0.92)
# Cosine similarity mapped onto the intent score: at or below LOW contributes
# nothing; at or above HIGH scores the intent maximum. Calibrated 2026-10-07 on
# gemini-embedding-001 (768 dims): correct agents 0.58–0.70, unrelated 0.48–0.57.
EMBED_LOW = _env_f("AGENT_ROUTER_EMBED_LOW", 0.55)
EMBED_HIGH = _env_f("AGENT_ROUTER_EMBED_HIGH", 0.75)

# Default reply budget for agents that don't register max_reply_latency_s.
DEFAULT_REPLY_LATENCY_S = _env_f("DISPATCH_REPLY_LATENCY_S", 5.0)

ALIAS_SCORE = 0.88
INTENT_MAX = 0.85

DECISION_MATCHED = "matched"
DECISION_AMBIGUOUS = "ambiguous"
DECISION_NONE = "none"
DECISION_WRONG_MODE = "wrong_mode"

POLICY_OWNS_DOMAIN = "owns_domain"
POLICY_ON_REQUEST = "on_request"

_STOPWORDS = frozenset(
    "a an the to for of and or with my me i you your please can could would "
    "like want need call connect talk speak dial agent service bot this "
    "that it is are be do does about on in at from up hey hi ok okay have has "
    "how what when where who much many get let".split()
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """Lowercase alnum tokens with stopwords removed. Keeps order and duplicates."""
    if not text:
        return []
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS]


def normalize_name(name: str) -> str:
    """Canonical comparable form: lowercase, alnum words, single spaces."""
    return " ".join(_TOKEN_RE.findall((name or "").lower()))


def phonetic_key(text: str) -> str:
    """A compact consonant skeleton so STT near-misses collide.

    "kairos" → "krs", "cairo's" → "krs", "kai ross" → "krs".
    """
    s = re.sub(r"[^a-z]", "", (text or "").lower())
    if not s:
        return ""
    s = (
        s.replace("ph", "f").replace("gh", "g").replace("ck", "k")
        .replace("wr", "r").replace("kn", "n").replace("wh", "w")
        .replace("tch", "ch").replace("sch", "sk")
    )
    out: list[str] = []
    for i, ch in enumerate(s):
        if ch in "aeiouyhw":
            if i == 0 and ch in "aeiou":
                out.append("a")
            continue
        ch = {"c": "k", "q": "k", "z": "s", "v": "f", "d": "t", "b": "p", "g": "k"}.get(ch, ch)
        if out and out[-1] == ch:
            continue
        out.append(ch)
    return "".join(out)


def _strs(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        value = [value]
    return tuple(str(v).strip() for v in value if str(v).strip())


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip():
        return value.strip().lower() not in ("0", "false", "no", "off")
    return default


@dataclass(frozen=True)
class AgentRecord:
    """One routable agent as the router sees it (a normalized registry row)."""

    id: str
    name: str
    url: str
    description: str = ""
    keywords: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    user_intents: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    intent_aliases: tuple[str, ...] = ()
    routing_policy: str = POLICY_ON_REQUEST
    user_data: bool = False
    side_effects: bool = True
    service_id: Optional[str] = None
    modes: tuple[str, ...] = (MODE_BRIDGE,)
    task_ops: tuple[str, ...] = DEFAULT_TASK_OPS
    events: tuple[str, ...] = ()
    binding: str = "ws"
    max_concurrency: int = 8
    max_reply_latency_s: float = DEFAULT_REPLY_LATENCY_S
    default_deadline_s: Optional[float] = None
    slots: tuple[dict, ...] = ()
    version: str = "1"
    embedding: tuple[float, ...] = ()

    @staticmethod
    def from_registry_row(row: dict) -> "AgentRecord":
        def num(key: str, default: float, lo: float, hi: float) -> float:
            try:
                v = float(row.get(key))
            except (TypeError, ValueError):
                return default
            return max(lo, min(hi, v))

        deadline = row.get("default_deadline_s")
        try:
            deadline = float(deadline) if deadline not in (None, "") else None
        except (TypeError, ValueError):
            deadline = None
        policy = str(row.get("routing_policy") or POLICY_ON_REQUEST).lower()
        if policy not in (POLICY_OWNS_DOMAIN, POLICY_ON_REQUEST):
            policy = POLICY_ON_REQUEST
        emb = row.get("routing_embedding") or ()
        try:
            emb = tuple(float(x) for x in emb)
        except (TypeError, ValueError):
            emb = ()
        slots = tuple(s for s in (row.get("slots") or []) if isinstance(s, dict) and s.get("name"))
        return AgentRecord(
            id=str(row.get("id") or ""),
            name=str(row.get("name") or "").strip(),
            url=str(row.get("url") or "").strip(),
            description=str(row.get("description") or "").strip(),
            keywords=_strs(row.get("keywords")),
            capabilities=_strs(row.get("capabilities")),
            user_intents=_strs(row.get("user_intents")),
            domains=_strs(row.get("domains")),
            intent_aliases=_strs(row.get("intent_aliases")),
            routing_policy=policy,
            user_data=_bool(row.get("user_data"), False),
            side_effects=_bool(row.get("side_effects"), True),
            service_id=(str(row["service_id"]) if row.get("service_id") else None),
            modes=tuple(m.lower() for m in _strs(row.get("modes"))) or (MODE_BRIDGE,),
            task_ops=tuple(o.lower() for o in _strs(row.get("task_ops"))) or DEFAULT_TASK_OPS,
            events=tuple(e.lower() for e in _strs(row.get("events"))),
            binding=str(row.get("binding") or "ws").lower(),
            max_concurrency=int(num("max_concurrency", 8, 1, 1000)),
            max_reply_latency_s=num("max_reply_latency_s", DEFAULT_REPLY_LATENCY_S, 1.0, 30.0),
            default_deadline_s=deadline,
            slots=slots,
            version=str(row.get("version") or "1"),
            embedding=emb,
        )

    def supports(self, mode: str) -> bool:
        if mode == MODE_TASK:
            # Task mode needs push delivery (callback) — the orchestrator never polls.
            return MODE_TASK in self.modes and "callback" in self.events
        return mode in self.modes

    def supports_op(self, op: str) -> bool:
        return op in self.task_ops

    @property
    def required_slots(self) -> list[dict]:
        return [s for s in self.slots if s.get("required")]

    def to_public(self) -> dict:
        return {
            "id": self.id, "name": self.name, "description": self.description,
            "service_id": self.service_id, "modes": list(self.modes),
            "side_effects": self.side_effects,
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

    MATCHED: use `best`. AMBIGUOUS: ask; `candidates` are the contenders.
    NONE: nothing plausible. WRONG_MODE: `wrong_mode` is the agent the user
    clearly meant, which doesn't support the requested mode.
    """

    decision: str
    query: str
    candidates: list[Candidate] = field(default_factory=list)
    wrong_mode: Optional[AgentRecord] = None
    named: bool = False  # True if the user named an agent (vs. intent routing)

    @property
    def best(self) -> Optional[AgentRecord]:
        return self.candidates[0].agent if self.candidates else None

    @property
    def matched(self) -> bool:
        return self.decision == DECISION_MATCHED

    def names(self, n: int = 3) -> list[str]:
        out: list[str] = []
        for c in self.candidates:
            if c.agent.name not in out:
                out.append(c.agent.name)
            if len(out) >= n:
                break
        return out


@dataclass
class _Index:
    agents: list[AgentRecord]
    by_id: dict[str, AgentRecord]
    by_service_id: dict[str, AgentRecord]
    by_norm_name: dict[str, list[AgentRecord]]
    by_phonetic: dict[str, list[AgentRecord]]
    name_tokens: dict[str, set[str]]
    doc_tokens: dict[str, set[str]]
    idf: dict[str, float]
    aliases: list[tuple[str, str]]  # (normalized alias, agent id)
    norm_names: list[str]
    norm_name_to_ids: dict[str, list[str]]
    built_at: float

    @staticmethod
    def empty() -> "_Index":
        return _Index([], {}, {}, {}, {}, {}, {}, {}, [], [], {}, time.monotonic())


def _build_index(agents: Iterable[AgentRecord]) -> _Index:
    agents = [a for a in agents if a.name and a.url]
    by_id: dict[str, AgentRecord] = {}
    by_service_id: dict[str, AgentRecord] = {}
    by_norm_name: dict[str, list[AgentRecord]] = defaultdict(list)
    by_phonetic: dict[str, list[AgentRecord]] = defaultdict(list)
    name_tokens: dict[str, set[str]] = defaultdict(set)
    doc_tokens: dict[str, set[str]] = defaultdict(set)
    df: dict[str, int] = defaultdict(int)
    aliases: list[tuple[str, str]] = []

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
            tpk = phonetic_key(tok)
            if tpk and tpk != pk:
                by_phonetic[tpk].append(a)
        doc = " ".join([
            a.name, a.name, a.description, " ".join(a.keywords), " ".join(a.capabilities),
            " ".join(a.user_intents), " ".join(a.domains), " ".join(a.domains),
        ])
        for tok in set(tokenize(doc)):
            doc_tokens[tok].add(a.id)
            df[tok] += 1
        for alias in a.intent_aliases:
            na = normalize_name(alias)
            if na:
                aliases.append((na, a.id))

    n = max(1, len(agents))
    idf = {tok: math.log(1.0 + n / (1.0 + c)) for tok, c in df.items()}
    return _Index(
        agents=agents, by_id=by_id, by_service_id=by_service_id,
        by_norm_name=dict(by_norm_name), by_phonetic=dict(by_phonetic),
        name_tokens=dict(name_tokens), doc_tokens=dict(doc_tokens), idf=idf,
        aliases=sorted(aliases, key=lambda x: -len(x[0])),
        norm_names=sorted(by_norm_name.keys()),
        norm_name_to_ids={k: [a.id for a in v] for k, v in by_norm_name.items()},
        built_at=time.monotonic(),
    )


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


@dataclass
class _Health:
    consecutive_failures: int = 0
    open_until: float = 0.0
    last_failure_reason: str = ""


Loader = Callable[[], list[dict]]


class AgentRouter:
    """Thread-safe, lazily refreshing router over the agent registry."""

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
        self._index = _Index.empty()
        self._loaded_once = False
        self._dirty = True
        self._retry_after = 0.0
        self._last_refresh_error: Optional[str] = None
        self._lock = threading.Lock()
        self._refreshing = False
        self._health: dict[str, _Health] = defaultdict(_Health)

    # ----- snapshot ---------------------------------------------------------

    def invalidate(self) -> None:
        self._dirty = True

    def load_now(self) -> int:
        with self._lock:
            if self._refreshing:
                return len(self._index.agents)
            self._refreshing = True
        try:
            records = []
            for r in self._loader() or []:
                try:
                    rec = AgentRecord.from_registry_row(r)
                except Exception:
                    log.exception("router: skipping malformed registry row")
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

    def ensure_fresh(self) -> None:
        if self._needs_refresh():
            self.load_now()

    async def ensure_fresh_async(self) -> None:
        if self._needs_refresh():
            await asyncio.to_thread(self.load_now)

    def ensure_fresh_nonblocking(self) -> None:
        """Never block on a reload after the first one (stale is served meanwhile)."""
        if not self._needs_refresh():
            return
        if not self._loaded_once:
            self.load_now()
            return
        threading.Thread(target=self.load_now, name="agent-router-refresh", daemon=True).start()

    def agents(self) -> list[AgentRecord]:
        return list(self._index.agents)

    def get(self, agent_id: str) -> Optional[AgentRecord]:
        if not self._loaded_once:
            self.load_now()
        return self._index.by_id.get(str(agent_id))

    def stats(self) -> dict:
        idx = self._index
        now = time.monotonic()
        return {
            "agents": len(idx.agents),
            "task_capable": sum(1 for a in idx.agents if a.supports(MODE_TASK)),
            "with_embeddings": sum(1 for a in idx.agents if a.embedding),
            "snapshot_age_s": round(now - idx.built_at, 1),
            "last_refresh_error": self._last_refresh_error,
            "open_circuits": [aid for aid, h in self._health.items() if h.open_until > now],
        }

    # ----- health -----------------------------------------------------------

    def report_failure(self, agent_id: str, reason: str = "") -> None:
        h = self._health[agent_id]
        h.consecutive_failures += 1
        h.last_failure_reason = reason
        if h.consecutive_failures >= self._failure_threshold:
            h.open_until = time.monotonic() + self._cooldown_s
            log.warning("router: circuit OPEN agent_id=%s reason=%s", agent_id, reason)

    def report_success(self, agent_id: str) -> None:
        h = self._health[agent_id]
        h.consecutive_failures = 0
        h.open_until = 0.0

    def is_healthy(self, agent_id: str) -> bool:
        h = self._health.get(agent_id)
        return h is None or h.open_until <= time.monotonic()

    # ----- resolution -------------------------------------------------------

    def resolve(
        self,
        selector: str = "",
        intent: str = "",
        *,
        mode: Optional[str] = None,
        k: int = 5,
        query_embedding: Optional[Sequence[float]] = None,
    ) -> RouteResult:
        """Resolve a spoken agent name and/or an intent to a decision.

        `selector` is what the user called the agent (may be garbled or a
        service_id). `intent` is what they want. `mode` filters to agents
        supporting bridge or task; None means any mode (the caller decides the
        mode afterwards). `query_embedding` is used only when no agent is named.
        Never raises.
        """
        # Never block a voice turn on the database: serve the current snapshot
        # and refresh in the background (only the very first load is synchronous).
        self.ensure_fresh_nonblocking()
        idx = self._index
        sel_raw = (selector or "").strip()
        sel = normalize_name(sel_raw)
        intent = (intent or "").strip()
        result = RouteResult(decision=DECISION_NONE, query=sel_raw or intent, named=bool(sel_raw))
        if not idx.agents:
            return result

        scores: dict[str, float] = defaultdict(float)
        reasons: dict[str, list[str]] = defaultdict(list)

        def bump(aid: str, s: float, why: str) -> None:
            if s > scores[aid]:
                scores[aid] = s
            reasons[aid].append(why)

        if sel_raw:
            a = idx.by_service_id.get(sel_raw.lower())
            if a:
                bump(a.id, 1.0, "service_id")
            for a in idx.by_norm_name.get(sel, []):
                bump(a.id, 1.0, "exact_name")
            if sel:
                for norm in idx.norm_names:
                    if norm == sel:
                        continue
                    if norm.startswith(sel):
                        for aid in idx.norm_name_to_ids[norm]:
                            bump(aid, 0.9, "name_prefix")
                    elif (" " + sel + " ") in (" " + norm + " "):
                        for aid in idx.norm_name_to_ids[norm]:
                            bump(aid, 0.85, "name_contains")
                    elif sel in norm:
                        for aid in idx.norm_name_to_ids[norm]:
                            bump(aid, 0.7, "name_substring")
            sel_pk = phonetic_key(sel)
            if sel_pk:
                for a in idx.by_phonetic.get(sel_pk, []):
                    bump(a.id, 0.75, "phonetic")
            for tok in tokenize(sel_raw):
                tpk = phonetic_key(tok)
                if tpk and len(tpk) >= 2:
                    for a in idx.by_phonetic.get(tpk, []):
                        bump(a.id, 0.5, "phonetic_token")
                for aid in idx.name_tokens.get(tok, ()):
                    bump(aid, 0.6, "name_token")
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

        # Intent signals. When a name was given they only lift existing name
        # candidates (a tie-breaker); otherwise they are the routing signal.
        intent_text = intent if sel_raw else " ".join(x for x in (intent, sel_raw) if x)
        intent_scores: dict[str, float] = defaultdict(float)
        if intent_text:
            norm_intent = " " + normalize_name(intent_text) + " "
            for alias, aid in idx.aliases:
                if (" " + alias + " ") in norm_intent:
                    intent_scores[aid] = max(intent_scores[aid], ALIAS_SCORE)
                    reasons[aid].append("alias")
            toks = set(tokenize(intent_text))
            if toks:
                hits: dict[str, float] = defaultdict(float)
                max_possible = sum(idx.idf.get(t, 0.0) for t in toks) or 1.0
                for tok in toks:
                    w = idx.idf.get(tok)
                    if not w:
                        continue
                    for aid in idx.doc_tokens.get(tok, ()):
                        hits[aid] += w
                for aid, s in hits.items():
                    frac = min(1.0, s / max_possible)
                    lex = 0.35 + 0.5 * frac
                    if lex > intent_scores[aid]:
                        intent_scores[aid] = lex
                    reasons[aid].append("intent")
            if not sel_raw and query_embedding:
                span = max(1e-6, EMBED_HIGH - EMBED_LOW)
                for a in idx.agents:
                    if not a.embedding:
                        continue
                    sim = cosine(query_embedding, a.embedding)
                    if sim <= EMBED_LOW:
                        continue
                    emb = 0.35 + 0.5 * min(1.0, (sim - EMBED_LOW) / span)
                    if emb > intent_scores[a.id]:
                        intent_scores[a.id] = emb
                    reasons[a.id].append(f"embedding:{sim:.2f}")
        for aid, s in intent_scores.items():
            if sel_raw:
                if aid in scores:
                    scores[aid] = min(1.0, scores[aid] + 0.1 * s)
            else:
                bump(aid, min(ALIAS_SCORE, s), "intent_route")

        ranked: list[Candidate] = []
        best_wrong_mode: Optional[tuple[float, AgentRecord]] = None
        for aid, s in scores.items():
            a = idx.by_id.get(aid)
            if a is None:
                continue
            if mode is not None and not a.supports(mode):
                if s >= self._confident_score and (best_wrong_mode is None or s > best_wrong_mode[0]):
                    best_wrong_mode = (s, a)
                continue
            if not self.is_healthy(aid) and not sel_raw:
                # Intent routes prefer healthy agents. A user who *names* an agent
                # still gets it (and hears it isn't answering if the dial fails).
                s *= 0.5
                reasons[aid].append("unhealthy")
            ranked.append(Candidate(agent=a, score=min(1.0, s), reasons=tuple(dict.fromkeys(reasons[aid]))))
        ranked.sort(key=lambda c: (-c.score, c.agent.name.lower()))
        result.candidates = ranked[: max(1, k)]

        if best_wrong_mode is not None and (not ranked or ranked[0].score < self._confident_score):
            result.decision = DECISION_WRONG_MODE
            result.wrong_mode = best_wrong_mode[1]
            return result
        if not ranked or ranked[0].score < self._match_floor:
            result.decision = DECISION_NONE
            return result
        top = ranked[0].score
        second = ranked[1].score if len(ranked) > 1 else 0.0
        if top >= self._confident_score and second < top - 1e-9:
            result.decision = DECISION_MATCHED
        elif self._ambiguity_margin > 0 and second >= self._match_floor and (top - second) <= self._ambiguity_margin:
            result.decision = DECISION_AMBIGUOUS
        else:
            result.decision = DECISION_MATCHED
        return result

    def search(self, query: str, *, k: int = 5, query_embedding: Optional[Sequence[float]] = None) -> list[Candidate]:
        """Discovery for `find_agents`: rank agents for a free-text query, any mode."""
        res = self.resolve("", query, mode=None, k=k, query_embedding=query_embedding)
        if res.candidates:
            return [c for c in res.candidates if c.score >= self._match_floor * 0.8]
        # Fall back to treating the query as a name ("is there a weather bot?").
        return self.resolve(query, "", mode=None, k=k).candidates

    def prompt_summary(self, limit: int = 30) -> str:
        """Bounded description of the registry for the system prompt (§1.3)."""
        self.ensure_fresh_nonblocking()
        agents = self._index.agents
        if not agents:
            return ""
        n = len(agents)
        if n > limit:
            return (
                f"There are {n} registered agents, too many to list. Do NOT guess names. "
                "Pass what the user said as `agent` (and what they want as `intent`); the "
                "orchestrator resolves it. Use find_agents to check which agents exist."
            )
        lines = [
            f"Registered agents ({n}). Pass the agent's name as you heard it as `agent`; "
            "the orchestrator resolves it, including speech-to-text garbles:"
        ]
        for a in sorted(agents, key=lambda x: x.name.lower()):
            desc = a.description.split("\n")[0][:120]
            modes = "live calls" if a.modes == (MODE_BRIDGE,) else (
                "background tasks" if MODE_BRIDGE not in a.modes else "live calls and background tasks"
            )
            lines.append(f"- {a.name} ({modes}): {desc}" if desc else f"- {a.name} ({modes})")
        return "\n".join(lines)


def _default_loader() -> list[dict]:
    import agents_registry  # local import keeps this module DB-free at import time

    return agents_registry.list_agents(active_only=True)


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
    """Test hook."""
    global _router
    _router = router


def invalidate() -> None:
    """Registry write hook: the next lookup reloads."""
    if _router is not None:
        _router.invalidate()

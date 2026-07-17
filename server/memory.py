"""The whole memory engine, top to bottom.

Write path:  add()    conversation turn -> extract facts -> scrub PII ->
                      compare with similar memories -> ADD/UPDATE/DELETE/NOOP -> journal
Read path:   search() query -> retrieve this customer's memories, nearest first ->
                      deterministic blended rerank (relevance + importance + recency
                      + lexical) with a relevance floor that keyword hits bypass.
                      No LLM in the hot path.
"""

import json
import logging
import os
import re
import threading
from datetime import date, datetime, timezone

from openai import OpenAI
from pydantic import BaseModel, ValidationError, field_validator

import store
from prompts import DECIDE_SYSTEM, EXTRACT_SYSTEM
from scrub import scrub

log = logging.getLogger("ledger.memory")

# ---------- model I/O (the only code that talks to OpenAI) ----------

CHAT_MODEL = os.getenv("LEDGER_CHAT_MODEL", "gpt-4o")
EMBED_MODEL = os.getenv("LEDGER_EMBED_MODEL", "text-embedding-3-small")
# Fixed seed + temperature 0 make the extraction/reconciliation LLM calls as
# reproducible as the API allows (agent replies intentionally vary). Log
# system_fingerprint if you need to detect model drift.
LEDGER_SEED = int(os.getenv("LEDGER_SEED", "7"))

_client: OpenAI | None = None
_client_lock = threading.Lock()


def client() -> OpenAI:
    global _client
    # Double-checked lock: FastAPI runs sync endpoints in a threadpool, so two threads
    # could otherwise race to build the client on the first request.
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = OpenAI()
    return _client


def embed(texts: list[str]) -> list[list[float]]:
    resp = client().embeddings.create(model=EMBED_MODEL, input=texts)
    return [d.embedding for d in resp.data]


def llm_chat(messages: list, tools: list | None = None):
    """One agent turn; returns the raw message (may contain tool calls)."""
    resp = client().chat.completions.create(
        model=CHAT_MODEL, messages=messages, tools=tools, temperature=0.3
    )
    return resp.choices[0].message


def _llm_json(system: str, user: str) -> dict:
    resp = client().chat.completions.create(
        model=CHAT_MODEL,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        response_format={"type": "json_object"},
        temperature=0,
        seed=LEDGER_SEED,
    )
    return json.loads(resp.choices[0].message.content or "{}")


# ---------- extraction phase: one turn -> candidate facts ----------

CATEGORIES = {"preference", "profile", "issue", "commitment", "episode"}
MAX_FACTS_PER_TURN = 8




class Candidate(BaseModel):
    """Pydantic model representing a candidate memory fact extracted from chat context."""
    text: str
    category: str = "episode"
    expires_at: date | None = None

    @field_validator("category", mode="before")
    @classmethod
    def known_category(cls, v):
        """Sanitizes the category string, falling back to 'episode' if unknown."""
        return v if v in CATEGORIES else "episode"

    @field_validator("expires_at", mode="before")
    @classmethod
    def loose_date(cls, v):
        """Fuzzy parses expiry ISO dates, ignoring none/null representations."""
        if not v or not isinstance(v, str) or v.lower() in ("null", "none"):
            return None
        try:
            return date.fromisoformat(v[:10])
        except ValueError:
            return None


def extract_facts(recent: list[dict], user_msg: str, assistant_msg: str) -> list[Candidate]:
    context = "\n".join(f"{m['role']}: {m['content']}" for m in recent)
    prompt = (
        f"Earlier in this session:\n{context or '(session start)'}\n\n"
        f"Latest exchange:\nuser: {user_msg}\nassistant: {assistant_msg}"
    )
    try:
        raw = _llm_json(EXTRACT_SYSTEM.format(today=date.today().isoformat()), prompt)
    except Exception as e:
        # Fail open: a malformed/failed extraction call must not crash the chat turn.
        # (decide_one and the grader already guard their own _llm_json calls; this is
        # the one extraction call that previously let a JSONDecodeError propagate.)
        log.warning("fact extraction failed, learning nothing this turn: %s", e)
        return []

    facts: list[Candidate] = []
    for item in (raw.get("facts") or [])[:MAX_FACTS_PER_TURN]:
        try:
            c = Candidate(**item)
        except (ValidationError, TypeError):
            continue
        clean, _ = scrub(c.text)
        if clean.strip():
            facts.append(c.model_copy(update={"text": clean.strip()}))
    return facts


# ---------- update phase: reconcile candidates against what's known ----------

OPS = {"ADD", "UPDATE", "DELETE", "NOOP"}


class Decision(BaseModel):
    op: str = "NOOP"
    target_id: str | None = None
    text: str | None = None

    @field_validator("op", mode="before")
    @classmethod
    def known_op(cls, v):
        v = str(v or "").upper()
        return v if v in OPS else "NOOP"


def decide_one(candidate: Candidate, neighbors: list[dict]) -> Decision:
    """Invokes the LLM to adjudicate if the new candidate matches, updates, or deletes
    any of the top similar memories.
    """
    payload = json.dumps(
        {
            "candidate_fact": candidate.text,
            "existing_memories": [
                {"id": n["id"], "text": n["text"], "similarity": round(n["similarity"], 3)}
                for n in neighbors
            ],
        },
        indent=2,
    )
    try:
        # Request decision block JSON from GPT
        d = Decision(**_llm_json(DECIDE_SYSTEM, payload))
    except (ValidationError, TypeError, json.JSONDecodeError):
        return Decision(op="NOOP")

    known_ids = {n["id"] for n in neighbors}
    # Fail-safe: if LLM references a random id it wasn't shown, treat as ADD or ignore
    if d.op in ("UPDATE", "DELETE") and d.target_id not in known_ids:
        return Decision(op="ADD") if d.op == "UPDATE" else Decision(op="NOOP")
    return d


# Below this cosine similarity, nothing stored is close enough to reconcile against,
# so the candidate is a deterministic ADD. NOTE: there is deliberately no symmetric
# high-similarity NOOP threshold - two facts can be textually near-identical yet
# contradict ("lives in Delhi" vs "lives in Mumbai"), so cosine alone can't declare a
# duplicate safely. The only deterministic NOOP is an exact (normalised) restatement.
SIM_ADD_BELOW = float(os.getenv("LEDGER_SIM_ADD_BELOW", "0.55"))

# How many existing memories a candidate is reconciled against. This is a CORRECTNESS
# knob, not a cost one, and it is the write path's mirror of RERANK_FETCH: the window is
# what the gate below can see, and anything outside it may as well not exist. Too narrow
# is a silent growth bug - if a candidate's true contradiction ranks just outside the
# window, nothing adjudicates the conflict and BOTH facts get stored, which DECIDE_SYSTEM
# guardrails 2 and 4 explicitly forbid ("Never let two contradictory memories coexist").
# The failure mode is invisible: no error, just a store that quietly stops deduplicating
# as a customer accumulates facts. The LLM never sees this whole window - gate() sends it
# only the genuinely-similar slice - so widening it costs one larger SQL read, not tokens.
NEIGHBOR_FETCH = int(os.getenv("LEDGER_NEIGHBOR_FETCH", "20"))


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def gate(candidate: Candidate, neighbors: list[dict]) -> Decision:
    """Applies fast, deterministic heuristics to bypass LLM calls for clear-cut cases.
    - Empty database: always ADD.
    - Exact word-for-word duplicate: always NOOP.
    - Cosine similarity below threshold: always ADD (no near facts exist).
    - Otherwise: ask LLM to adjudicate conflicts/updates.

    `neighbors` is the full NEIGHBOR_FETCH window (sorted nearest first). The deterministic
    checks scan all of it; only the slice above SIM_ADD_BELOW is handed to the LLM, so a
    wider window makes dedup stricter while making the adjudication payload *smaller* -
    it drops the sub-threshold padding the LLM was previously sent as context.
    """
    if not neighbors:
        return Decision(op="ADD")                     # empty store -> new
    norm = _norm(candidate.text)
    if any(_norm(n["text"]) == norm for n in neighbors):
        return Decision(op="NOOP")                     # exact restatement already stored
    if neighbors[0]["similarity"] <= SIM_ADD_BELOW:
        return Decision(op="ADD")                      # nothing close enough to reconcile
    contenders = [n for n in neighbors if n["similarity"] > SIM_ADD_BELOW]
    return decide_one(candidate, contenders)           # gray zone -> LLM adjudicates


def reconcile(customer_id: str, candidates: list[Candidate], source: str) -> list[dict]:
    """Apply the pipeline for each candidate; returns UI-friendly event dicts."""
    events: list[dict] = []
    if not candidates:
        return events
    # Embed all candidate texts in ONE API call, then reconcile each in turn. The neighbour
    # search still runs per-candidate against the live store, so intra-turn dedup (a later
    # candidate seeing an earlier one just inserted) is unaffected.
    embeddings = embed([c.text for c in candidates])
    for c, embedding in zip(candidates, embeddings):
        neighbors = store.similar_memories(customer_id, embedding, k=NEIGHBOR_FETCH)
        d = gate(c, neighbors)

        if d.op == "ADD":
            mid = store.insert_memory(
                customer_id, c.text, c.category, embedding, c.expires_at, source
            )
            events.append({"op": "ADD", "memory_id": mid, "text": c.text})

        elif d.op == "UPDATE":
            new_text = (d.text or c.text).strip()
            old = next((n["text"] for n in neighbors if n["id"] == d.target_id), None)
            store.update_memory(d.target_id, new_text, embed([new_text])[0], source)
            events.append({"op": "UPDATE", "memory_id": d.target_id,
                           "text": new_text, "old_text": old})

        elif d.op == "DELETE":
            old = next((n["text"] for n in neighbors if n["id"] == d.target_id), None)
            store.deactivate_memory(d.target_id, "DELETE", source)
            events.append({"op": "DELETE", "memory_id": d.target_id, "text": old})

        else:
            events.append({"op": "NOOP", "text": c.text})
    return events


# ---------- read path: retrieval + deterministic blended rerank ----------
#
# Deterministic-first, mirroring the write path's gate(): the read path ranks with plain,
# auditable Python arithmetic - no LLM in the hot path.
#
# Retrieval (store.similar_memories) hands the reranker this customer's memories, nearest
# first. The pool is deliberately GENEROUS rather than tight, because selecting the pool on
# one signal and then ranking it on four is a silent truncation: a fact the blend would have
# picked never gets the chance if a hard cosine cap dropped it first. A pool that doesn't
# bind in practice makes the blend below the only thing that decides recall.
#
# The rerank then BLENDS four signals so recall isn't "whatever is nearest in cosine space".
# Every weight and threshold is an explicit, env-overridable constant (house style, like
# SIM_ADD_BELOW), so the ranking can be tuned and reasoned about without touching code.
#
#   relevance  - cosine similarity to the (conversation-contextualised) query
#   importance - a per-category prior: an open commitment or a live issue matters more to
#                the current turn than a stable profile fact or a one-off episode
#   recency    - exponential decay on the memory's age, so fresh facts edge out stale ones
#   lexical    - token overlap with the query/recent turns; the same keyword signal, as a
#                ranking nudge
#
# A relevance FLOOR drops memories that aren't actually about this query - but a keyword/id
# hit bypasses it, so an exact match is never floored out for having mediocre cosine. Be
# clear about how much work the floor really does now that the pool is generous: a common
# query word ("order", "refund") is a salient term, so it waves plenty of memories straight
# past the floor. It trims the obviously off-topic tail; it is not a strong filter, and it is
# not what picks the top k - the blend is. That is the intended division of labour, and it is
# why the blend, not the floor, is the thing worth tuning. If a query is so generic that
# nothing survives, we rank the whole pool rather than starving the reply of context.

# Size of the candidate pool handed to the rerank. A SAFETY VALVE, not a quality knob: it
# is set so it does not bind for a real customer, so the blend ranks every fact they have.
# The non-relevance weights sum to at most 0.35 + 0.20 + 0.25 = 0.80, so a memory can trail
# the cosine leader by up to 0.80 and still win the blend - routine inside a top-15 pool
# (which is what this used to be), vanishingly unlikely inside a top-500. If it ever does
# bind, we degrade to the 500 nearest rather than starving the blend.
RERANK_FETCH = int(os.getenv("LEDGER_RERANK_FETCH", "500"))

# A candidate must clear this cosine similarity to count as relevant to the query at all.
RELEVANCE_FLOOR = float(os.getenv("LEDGER_RELEVANCE_FLOOR", "0.20"))

# Blend weights. relevance dominates; the rest nudge the order.
W_RELEVANCE = float(os.getenv("LEDGER_W_RELEVANCE", "1.0"))
W_IMPORTANCE = float(os.getenv("LEDGER_W_IMPORTANCE", "0.35"))
W_RECENCY = float(os.getenv("LEDGER_W_RECENCY", "0.20"))
W_LEXICAL = float(os.getenv("LEDGER_W_LEXICAL", "0.25"))

# Age (days) at which the recency signal has decayed to half.
RECENCY_HALFLIFE_DAYS = float(os.getenv("LEDGER_RECENCY_HALFLIFE_DAYS", "45"))

# Importance prior by category. Open obligations to the customer outrank background facts.
CATEGORY_PRIOR = {
    "commitment": 1.0,
    "issue": 0.9,
    "preference": 0.6,
    "profile": 0.5,
    "episode": 0.3,
}
DEFAULT_PRIOR = 0.4


# Words and identifiers, punctuation stripped: "ORD-5512," -> "ord-5512". Shared by the
# lexical signal and the keyword-retrieval terms so both tokenise the same way.
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9\-]*")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


def _recent_user_text(recent: list[dict], query: str) -> str:
    """This query plus the customer's last three turns - the context the lexical signals see.

    Filter to the customer's turns and THEN take the last three. Slicing the last three
    messages first and keeping whichever happened to be theirs is not the same thing: with
    roles alternating, that window holds one or two customer turns, so the signal saw less
    context than the name promises. _contextual_query already reads this history the
    filter-then-slice way; the two now agree.
    """
    users = [m["content"] for m in (recent or []) if m.get("role") == "user"]
    return " ".join([query] + users[-3:])


def _salient_terms(recent: list[dict], query: str, limit: int = 8) -> list[str]:
    """Distinctive tokens worth a keyword match - order ids and content words, not stopwords
    like "the"/"on". Used both to widen retrieval and to bypass the relevance floor."""
    out: list[str] = []
    for t in _TOKEN_RE.findall(_recent_user_text(recent, query).lower()):
        if (len(t) >= 4 or any(c.isdigit() for c in t)) and t not in out:
            out.append(t)
        if len(out) >= limit:
            break
    return out


def _recency_decay(when, now: datetime) -> float:
    """0..1 exponential decay on a memory's age; unknown timestamps score neutrally."""
    if not isinstance(when, datetime):
        return 0.5
    try:
        age_days = max(0.0, (now - when).total_seconds() / 86400.0)
    except (TypeError, ValueError):
        return 0.5
    return 0.5 ** (age_days / RECENCY_HALFLIFE_DAYS)


def _lexical_overlap(text: str, terms: set[str]) -> float:
    """Jaccard token overlap between a memory and the query/recent-turn terms (0..1)."""
    toks = _tokens(text)
    if not toks or not terms:
        return 0.0
    return len(toks & terms) / len(toks | terms)


def _context_terms(recent: list[dict], query: str) -> set[str]:
    """The keyword bag the lexical ranking signal matches against."""
    return _tokens(_recent_user_text(recent, query))


def _contextual_query(recent: list[dict], query: str) -> str:
    """Fold the immediately preceding user turn into the text we embed, so retrieval
    reflects the conversation, not just the latest message in isolation."""
    prior = [m["content"] for m in (recent or []) if m.get("role") == "user"]
    return f"{prior[-1]}\n{query}" if prior else query


def _blended_score(row: dict, terms: set[str], now: datetime) -> float:
    similarity = float(row.get("similarity") or 0.0)
    prior = CATEGORY_PRIOR.get(row.get("category"), DEFAULT_PRIOR)
    recency = _recency_decay(row.get("updated_at") or row.get("created_at"), now)
    lexical = _lexical_overlap(row.get("text", ""), terms)
    return (W_RELEVANCE * similarity + W_IMPORTANCE * prior
            + W_RECENCY * recency + W_LEXICAL * lexical)


def contextual_rerank(recent: list[dict], query: str, rows: list[dict], k: int,
                      now: datetime | None = None) -> list[dict]:
    """Rank the retrieved pool by the deterministic blend, drop sub-floor memories, and
    return the top k. Each returned row carries its `score` so the choice is auditable.
    Fully deterministic given a fixed clock - no LLM call.
    """
    if not rows:
        return []
    now = now or datetime.now(timezone.utc)
    terms = _context_terms(recent, query)
    salient = set(_salient_terms(recent, query))
    for r in rows:
        r["score"] = round(_blended_score(r, terms, now), 4)

    # Keep a memory if it clears the cosine floor OR shares a salient term (order id/keyword)
    # with the query - a keyword hit is relevant regardless of how fuzzy its embedding is.
    kept = [r for r in rows
            if float(r.get("similarity") or 0.0) >= RELEVANCE_FLOOR
            or (salient & _tokens(r.get("text", "")))]
    if not kept:
        # nothing is clearly on-topic (e.g. a broad "what do you know about me?"); rank the
        # whole pool rather than returning nothing.
        kept = list(rows)
    kept.sort(key=lambda r: r["score"], reverse=True)
    if len(kept) < len(rows):
        log.info("rerank dropped %d/%d sub-floor memories", len(rows) - len(kept), len(rows))
    return kept[:k]


# ---------- the public API ----------

class Memory:
    """One instance serves all customers; every call is scoped by customer_id."""

    def add(self, user_msg: str, assistant_msg: str, customer_id: str,
            recent: list[dict] | None = None) -> list[dict]:
        """Run the write path on one conversation turn. Returns applied ops."""
        candidates = extract_facts(recent or [], user_msg, assistant_msg)
        if not candidates:
            return []
        return reconcile(customer_id, candidates, source=user_msg)

    def search(self, query: str, customer_id: str, recent: list[dict] | None = None, k: int = 6) -> list[dict]:
        store.expire_sweep(customer_id)
        # Retrieve nearest to the conversation-contextualised query, then rank the pool with
        # the deterministic blend + relevance floor. The pool is generous (see RERANK_FETCH),
        # so the blend - not a cosine pre-filter - is what decides what the agent sees.
        pool_query = _contextual_query(recent or [], query)
        rows = store.similar_memories(customer_id, embed([pool_query])[0], RERANK_FETCH)
        reranked = contextual_rerank(recent or [], query, rows, k)
        return [
            {"id": r["id"], "text": r["text"], "category": r["category"],
             "score": r.get("score", 0.0)}
            for r in reranked
        ]

    def get_all(self, customer_id: str) -> list[dict]:
        store.expire_sweep(customer_id)
        return store.get_memories(customer_id)

    def history(self, memory_id: str) -> list[dict]:
        return store.memory_history(memory_id)

    def forget(self, memory_id: str) -> None:
        store.deactivate_memory(memory_id, "DELETE", "manual delete")

    @staticmethod
    def prompt_block(memories: list[dict]) -> str:
        if not memories:
            return "(nothing yet - this is your first conversation with them)"
        return "\n".join(f"- ({m['category']}) {m['text']}" for m in memories)

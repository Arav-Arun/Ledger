"""The whole memory engine, top to bottom.

Write path:  add()    conversation turn -> extract facts -> scrub PII ->
                      compare with similar memories -> ADD/UPDATE/DELETE/NOOP -> journal
Read path:   search() query -> vector search (scoped to customer) -> deterministic
                      blended rerank (relevance + importance + recency + lexical) with a
                      relevance floor. No LLM in the hot path.
"""

import json
import logging
import os
from datetime import date, datetime, timezone

from openai import OpenAI
from pydantic import BaseModel, ValidationError, field_validator, model_validator

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


def client() -> OpenAI:
    global _client
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
    raw = _llm_json(EXTRACT_SYSTEM.format(today=date.today().isoformat()), prompt)

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


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def gate(candidate: Candidate, neighbors: list[dict]) -> Decision:
    """Applies fast, deterministic heuristics to bypass LLM calls for clear-cut cases.
    - Empty database: always ADD.
    - Exact word-for-word duplicate: always NOOP.
    - Cosine similarity below threshold: always ADD (no near facts exist).
    - Otherwise: ask LLM to adjudicate conflicts/updates.
    """
    if not neighbors:
        return Decision(op="ADD")                     # empty store -> new
    norm = _norm(candidate.text)
    if any(_norm(n["text"]) == norm for n in neighbors):
        return Decision(op="NOOP")                     # exact restatement already stored
    if neighbors[0]["similarity"] <= SIM_ADD_BELOW:
        return Decision(op="ADD")                      # nothing close enough to reconcile
    return decide_one(candidate, neighbors)            # gray zone -> LLM adjudicates


def reconcile(customer_id: str, candidates: list[Candidate], source: str) -> list[dict]:
    """Apply the pipeline for each candidate; returns UI-friendly event dicts."""
    events: list[dict] = []
    for c in candidates:
        embedding = embed([c.text])[0]
        neighbors = store.similar_memories(customer_id, embedding, k=5)
        d = gate(c, neighbors)

        if d.op == "ADD":
            mid = store.insert_memory(
                customer_id, c.text, c.category, embedding, c.expires_at, source
            )
            events.append({"op": "ADD", "memory_id": mid, "text": c.text})

        elif d.op == "UPDATE":
            new_text = (d.text or c.text).strip()
            old = next(n["text"] for n in neighbors if n["id"] == d.target_id)
            store.update_memory(d.target_id, new_text, embed([new_text])[0], source)
            events.append({"op": "UPDATE", "memory_id": d.target_id,
                           "text": new_text, "old_text": old})

        elif d.op == "DELETE":
            old = next(n["text"] for n in neighbors if n["id"] == d.target_id)
            store.deactivate_memory(d.target_id, "DELETE", source)
            events.append({"op": "DELETE", "memory_id": d.target_id, "text": old})

        else:
            events.append({"op": "NOOP", "text": c.text})
    return events


# ---------- read path: scoped search + deterministic blended rerank ----------
#
# Deterministic-first, mirroring the write path's gate(): the read path ranks with plain,
# auditable Python arithmetic - no LLM in the hot path. Vector search fetches a candidate
# pool; the rerank then BLENDS four signals so recall isn't "whatever is nearest in cosine
# space". Every weight and threshold is an explicit, env-overridable constant (house style,
# like SIM_ADD_BELOW), so the ranking can be tuned and reasoned about without touching code.
#
#   relevance  - cosine similarity to the (conversation-contextualised) query
#   importance - a per-category prior: an open commitment or a live issue matters more to
#                the current turn than a stable profile fact or a one-off episode
#   recency    - exponential decay on the memory's age, so fresh facts edge out stale ones
#   lexical    - token overlap with the query/recent turns; a cheap hybrid signal so an
#                order number or ID typed verbatim isn't lost to fuzzy dense similarity
#
# A relevance FLOOR drops memories that aren't actually about this query (a real filter,
# not just a reorder). If a query is so generic that nothing clears the floor, we fall back
# to ranking the whole pool rather than starving the reply of context.

RERANK_FETCH = int(os.getenv("LEDGER_RERANK_FETCH", "15"))

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
    toks = set(_norm(text).split())
    if not toks or not terms:
        return 0.0
    return len(toks & terms) / len(toks | terms)


def _context_terms(recent: list[dict], query: str) -> set[str]:
    """The keyword bag the lexical signal matches against: this query plus the customer's
    last few turns, so an ID mentioned a message or two ago still helps."""
    parts = [query] + [m["content"] for m in (recent or [])[-3:] if m.get("role") == "user"]
    terms: set[str] = set()
    for p in parts:
        terms |= set(_norm(p).split())
    return terms


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
    for r in rows:
        r["score"] = round(_blended_score(r, terms, now), 4)

    kept = [r for r in rows if float(r.get("similarity") or 0.0) >= RELEVANCE_FLOOR]
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
        # Fetch a candidate pool by vector similarity to the conversation-contextualised
        # query, then rerank deterministically (relevance + importance + recency + lexical).
        pool_query = _contextual_query(recent or [], query)
        rows = store.similar_memories(customer_id, embed([pool_query])[0], k=RERANK_FETCH)
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

"""The whole memory engine, top to bottom.

Write path:  add()    conversation turn -> extract facts -> scrub PII ->
                      compare with similar memories -> ADD/UPDATE/DELETE/NOOP -> journal
Read path:   search() query -> vector search (scoped to customer) ->
                      rerank by relevance + importance + recency
"""

import json
import os
from datetime import date, datetime, timezone

from openai import OpenAI
from pydantic import BaseModel, ValidationError, field_validator, model_validator

import store
from prompts import DECIDE_SYSTEM, EXTRACT_SYSTEM, RERANK_SYSTEM
from scrub import scrub

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


# ---------- read path: scoped search + rerank ----------

RERANK_FETCH = 15


def contextual_rerank(recent: list[dict], query: str, rows: list[dict], k: int) -> list[dict]:
    if not rows:
        return []
    if len(rows) <= k:
        return rows

    # Format conversation context (last 6 turns)
    context = "\n".join(f"{m['role']}: {m['content']}" for m in recent[-6:])
    # Format memories list with indices
    memories_formatted = "\n".join(f"[{i}] ({r['category']}) {r['text']}" for i, r in enumerate(rows))

    prompt = (
        f"Conversation history:\n{context or '(session start)'}\n\n"
        f"Latest user query: {query}\n\n"
        f"Memories to evaluate and rank:\n{memories_formatted}"
    )

    try:
        sys_prompt = RERANK_SYSTEM.format(k=k)
        raw = _llm_json(sys_prompt, prompt)
        ranked_indices = raw.get("ranked_indices") or []

        seen = set()
        reranked = []
        for idx in ranked_indices:
            try:
                i = int(idx)
                if 0 <= i < len(rows) and i not in seen:
                    reranked.append(rows[i])
                    seen.add(i)
            except (ValueError, TypeError):
                continue

        # Append remaining candidates in vector order in case LLM missed some
        for i, row in enumerate(rows):
            if i not in seen:
                reranked.append(row)

        return reranked[:k]
    except Exception:
        # Fallback to vector search order if LLM reranking fails
        return rows[:k]


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
        # Fetch a pool of candidates based on vector similarity
        rows = store.similar_memories(customer_id, embed([query])[0], k=RERANK_FETCH)
        # Rerank candidates dynamically using the LLM contextual reranker
        reranked = contextual_rerank(recent or [], query, rows, k)
        return [
            {"id": r["id"], "text": r["text"], "category": r["category"]}
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

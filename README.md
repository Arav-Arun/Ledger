# Ledger

**Long-term memory for conversational AI — deterministic where it counts, auditable end to end.**

Ledger watches a conversation, extracts durable facts about the user, reconciles them against
what it already knows, and recalls the relevant ones on future turns, across separate sessions.
Every belief it holds is traceable to the message that created it, updated when the truth
changed, deleted when it stopped being true, and never polluted with secrets.

The design is **deterministic-first**: the parts that decide behaviour — PII scrubbing, the
reconcile gate, the retrieval rerank, the grounding pass/fail — are plain, auditable Python.
The LLM is used only where judgement is genuinely needed: extracting facts, and resolving
conflicts in the gray zone. **No LLM runs in the retrieval hot path.**

The repo ships a customer-support bot as a demonstration harness; the reusable part is the
engine in [`server/memory.py`](server/memory.py) and [`server/store.py`](server/store.py).

---

## Demo screenshots

| 1. Profile Ingestion & PII Redaction | 2. Issue Resolution & Context Recall |
| :---: | :---: |
| ![Profile Ingestion & PII Redaction](Assets/img1.png) | ![Issue Resolution & Context Recall](Assets/img2.png) |

---

## What makes it different

Most memory layers (mem0, Zep, LangMem, Letta) do a version of extract → embed → reconcile →
recall. Ledger's bet is that in a system of record for what you believe about a person,
**auditability and determinism are features, not overhead.** Five guarantees follow from that,
and together they are the pitch:

| Guarantee | What it means | Why competitors usually don't have it |
|-----------|---------------|----------------------------------------|
| **Same-transaction provenance ledger** | Every `ADD`/`UPDATE`/`DELETE`/`EXPIRE`/`EVICT` is journalled *in the same transaction as the mutation*, with the exact source message that caused it. The audit trail can never disagree with the memory table. | History exists elsewhere, but not mutation-level, source-attributed, and transactionally inseparable from the data. |
| **Learning gated on grounding** | A reply is learned from **only if** it passed an explicit grounding rubric. An unverified draft's content is withheld, so a hallucinated specific can't be laundered into permanent memory. | Others happily extract "facts" from whatever the assistant said, hallucinations included. |
| **Deterministic, auditable recall** | Ranking is a pure-Python weighted blend with every weight an env-overridable constant; each recalled fact carries its score. Pinnable in a unit test with no DB and no API key. | Recall order comes from opaque vector search or an LLM in the loop. |
| **PII stripped before the model sees it** | A deterministic regex + Luhn filter removes cards, OTP/CVV/PIN, account numbers, SSNs, emails, and phone numbers *before* text reaches the LLM or the store. | Redaction, when present, is best-effort or model-trusted. |
| **Point-in-time reconstruction** | Because the ledger is append-only and complete, "what did we believe about this customer on date X" is answered deterministically by replaying it — no separate temporal store. | Bitemporal answers usually require a dedicated temporal graph database. |

See [Where Ledger fits](#where-ledger-fits) for an honest comparison, including what it *doesn't* do.

---

## The memory model

Every fact is one row in `memories`, scoped to a customer:

| Field | Purpose |
|-------|---------|
| `text` | The fact, atomic and third-person (`"Customer prefers email over phone."`). |
| `category` | `issue` · `commitment` · `preference` · `profile` · `episode`. Drives the importance prior at recall. |
| `embedding` | `vector(1536)` (OpenAI `text-embedding-3-small`), for cosine search. |
| `active` | Soft-delete flag. Deletes flip this to `false`; rows are never destroyed. |
| `expires_at` | Optional TTL for time-bound facts (a trip, a temporary hold). Swept on access. |

Every mutation also appends a row to `memory_events` (`ADD` / `UPDATE` / `DELETE` / `EXPIRE` /
`EVICT`) with the old text, the new text, and the **source message that caused it** — an
append-only audit trail written in the same transaction as the mutation, so the two can never
disagree, and the complete history from which point-in-time state is reconstructed.

There is deliberately **no vector index.** Every query is scoped to one customer, and
`customer_id` is far more selective than the vector search, so the intended plan is to narrow
by `idx_memories_customer` and then scan that customer's rows exactly. An HNSW index over every
customer's vectors cannot serve that; it is built for a global nearest-neighbour search this
engine never issues — and pgvector's default `hnsw.iterative_scan = off` can silently return
fewer rows than the `LIMIT` asked for when combined with a selective non-indexed filter, which
would quietly degrade reconciliation to append-only. (This trade-off has a scale ceiling; see
[Demo vs production](#demo-vs-production).)

---

## Write path: learning from a turn

Runs after each assistant reply. [`memory.py`](server/memory.py) → `add()`.

```mermaid
flowchart LR
    T(["Turn"]) --> S["Scrub PII"] --> X["Extract facts (LLM)"] --> E["Embed"] --> G{"Gate"} --> DB[("Postgres")] --> C["Cap episodes"]
```

1. **Scrub** — deterministic regex + Luhn checksum strips card numbers, OTP/CVV/PIN,
   keyword-introduced account numbers, SSNs, emails, and unambiguous phone numbers *before*
   text reaches the LLM or the DB. Order ids (`ORD-5512`) and bare local digit runs are
   deliberately kept — they're indistinguishable from tracking numbers, so they are only
   redacted when disambiguated. ([`scrub.py`](server/scrub.py))
2. **Extract** — the one exchange becomes 0–8 atomic, third-person candidate facts, each with a
   category and optional expiry. Small talk yields an empty list. Fails open: a malformed
   extraction call learns nothing this turn rather than crashing it. (`prompts.EXTRACT_SYSTEM`)
3. **Reconcile** — each candidate is embedded and matched against its nearest existing memories
   (`NEIGHBOR_FETCH`, default 20), then a deterministic **gate** decides the operation, calling
   the LLM only when it must. The window is a *correctness* knob, not a cost one: the gate can
   only adjudicate what retrieval hands it, so a contradiction ranked outside the window is
   never seen and both facts get stored. The LLM is shown only the slice above `SIM_ADD_BELOW`,
   so a wider window costs a bigger SQL read, not tokens.

   | Gate condition | Operation | LLM? |
   |----------------|-----------|------|
   | No neighbours exist | `ADD` | no |
   | Normalised text exactly matches a neighbour | `NOOP` | no |
   | Top cosine similarity ≤ `0.55` (`SIM_ADD_BELOW`) | `ADD` | no |
   | Otherwise (the gray zone) | LLM adjudicates → `ADD` / `UPDATE` / `DELETE` / `NOOP` | yes |

   `UPDATE` rewrites a fact in place (a changed city, a switched channel); `DELETE` soft-removes
   one that is now resolved or cancelled. There is intentionally no high-similarity auto-`NOOP`:
   two near-identical sentences can still contradict (`"lives in Delhi"` vs `"lives in Mumbai"`),
   so only an exact restatement is a safe deterministic skip.
4. **Journal** — the mutation and its `memory_events` row commit together.
5. **Cap** — the gate makes most categories self-limiting. A preference or profile fact is
   `UPDATE`d in place when it changes; issues/commitments track real events a customer raises.
   `episode` is the only genuinely additive category, so it carries a per-customer ceiling
   (`MAX_EPISODES_PER_CUSTOMER`, default 200) with oldest-first eviction, journalled as `EVICT`.
   Open commitments are deliberately never evicted: silently forgetting an obligation made to a
   customer is worse than carrying a stale one.

Only a **grounded** assistant reply is learned from (see [Grounded replies](#grounded-replies-demo-harness));
an unverified draft's content is withheld so a hallucinated specific can't become permanent
memory. Learning is best-effort and isolated from the response: an extraction or embedding
outage degrades to "learned nothing this turn", never a `500` after the customer already has a
reply.

---

## Read path: recalling for a turn

Runs before each reply, fully deterministic. [`memory.py`](server/memory.py) → `search()`.

```mermaid
flowchart LR
    M(["Query"]) --> C["Contextualise + embed"] --> H["Retrieve pool"] --> RK["Blended rerank + floor"] --> TOP["Top-k"] --> AG[["Agent"]]
```

1. **Contextualise** — the query is prepended with the customer's previous turn before
   embedding, so recall reflects the conversation, not one message in isolation.
2. **Retrieve the pool** — this customer's active memories, nearest first, cosine attached. The
   pool is deliberately generous (`RERANK_FETCH`, default 500) rather than tight: selecting the
   pool on cosine alone and then ranking on four signals would silently truncate away facts the
   blend would have picked. The cap is a safety valve, not a quality knob. If retrieval fails
   (embedding outage, DB blip), recall **fails soft** — the turn is answered with no recalled
   memories rather than erroring.
3. **Blended rerank** — each candidate gets a deterministic score, no LLM.

   ```
   score = 1.00·relevance  +  0.35·importance  +  0.20·recency  +  0.25·lexical
   ```

   *relevance* = cosine to the contextualised query · *importance* = a per-category prior (an
   open `commitment` or live `issue` outranks a stable `profile` fact) · *recency* = exponential
   decay (45-day half-life) · *lexical* = token overlap with the query. Every weight and
   threshold is an env-overridable constant.
4. **Relevance floor** — memories below `0.20` cosine are dropped as off-topic, *unless* they
   share a salient term with the query (an exact id hit is never floored out). With a generous
   pool the floor trims the obviously off-topic tail; the blend is what picks the top `k`
   (default 6). If a query is so broad that nothing clears the floor, the whole pool is ranked
   rather than starving the reply.

---

## Grounded replies (demo harness)

The sample assistant drafts a reply, a grader scores it against an explicit **rubric** (no
invented customer facts, no contradiction, asks when a fact is unknown), and it revises until
the rubric passes or a hard iteration cap (2 rewrites) is hit. The rubric is plain data in one
place; a plain Python rule, not the LLM, decides whether a draft ships.

The check **fails closed**: a reply is marked `grounded` only if the grader returned an explicit
pass on *every* criterion. A missing verdict, wrong shape, or grader outage counts as
not-grounded, never a silent pass. The full per-attempt verdict trail is returned and shown in
the UI. ([`grounding.py`](server/grounding.py))

---

## Point-in-time recall

Because the event ledger is append-only and complete, the state of a customer's memory at any
past instant is a pure function of the events up to that instant. `store.memories_as_of()`
replays them — a memory's most recent event on or before `T` decides whether it was alive
(an `ADD`/`UPDATE`) or gone (a `DELETE`/`EXPIRE`/`EVICT`) — and reconstructs exactly what Ledger
believed then. This is bitemporal recall with **no separate temporal store to keep in sync**:
the audit trail already is the history.

```bash
curl "http://localhost:8000/api/customers/priya_sharma/memories-as-of?t=2026-07-01T00:00:00Z"
```

---

## Guarantees & invariants

- **The ledger and the memory table can never disagree** — every mutation and its audit row
  commit in one transaction (`FOR UPDATE` on updates avoids TOCTOU with a concurrent delete).
- **No hallucination laundering** — only replies that pass the grounding rubric are learned from.
- **No LLM in the retrieval hot path** — recall is deterministic and reproducible.
- **PII never reaches the model or the store** — it is stripped at the boundary, before extraction.
- **Growth is bounded** — self-limiting categories plus an episode cap; open commitments are
  never silently evicted.
- **Every belief is explainable** — traceable to its source message and reconstructable at any
  past time.
- **Graceful under failure** — recall fails soft, learning is isolated and best-effort, the
  embedding dimension is validated at the boundary, and OpenAI calls are bounded by an explicit
  timeout + retries.

---

## Configuration

Everything is an environment variable with a sensible default; see
[`server/.env.example`](server/.env.example) for the annotated list. The knobs that shape
behaviour:

| Variable | Default | What it controls |
|----------|---------|------------------|
| `OPENAI_API_KEY` | — | **Required.** |
| `DATABASE_URL` | — | **Required.** Postgres with pgvector. |
| `LEDGER_CHAT_MODEL` | `gpt-4o` | Extraction / reconciliation / agent model. |
| `LEDGER_EMBED_MODEL` | `text-embedding-3-small` | Embedding model. |
| `LEDGER_EMBED_DIM` | `1536` | Vector width; **must** match the `vector(N)` column. |
| `LEDGER_OPENAI_TIMEOUT` | `30` | Per-call timeout (seconds). |
| `LEDGER_OPENAI_MAX_RETRIES` | `2` | Retries on transient failures. |
| `LEDGER_SIM_ADD_BELOW` | `0.55` | Below this cosine, a candidate is a deterministic `ADD`. |
| `LEDGER_NEIGHBOR_FETCH` | `20` | Reconciliation window (a **correctness** knob). |
| `LEDGER_MAX_EPISODES` | `200` | Per-customer ceiling on the one unbounded category. |
| `LEDGER_RERANK_FETCH` | `500` | Recall candidate pool (a safety valve, not a quality knob). |
| `LEDGER_RELEVANCE_FLOOR` | `0.20` | Min cosine to count as on-topic (keyword hits bypass). |
| `LEDGER_W_RELEVANCE` / `_IMPORTANCE` / `_RECENCY` / `_LEXICAL` | `1.0` / `0.35` / `0.20` / `0.25` | Blend weights. |
| `LEDGER_RECENCY_HALFLIFE_DAYS` | `45` | Age at which the recency signal halves. |
| `LEDGER_LOG_LEVEL` | `INFO` | Server log level. |

---

## Demo vs production

This repo runs end to end and is honest about what it is: a **reference implementation and demo
harness.** The engine is production-shaped; the surface around it makes demo-friendly choices you
would change before exposing it publicly.

| Area | In this repo | For production |
|------|--------------|----------------|
| **Auth / multi-tenancy** | None. `customer_id` is a client-supplied string; every API route is open. | Put authentication in front and map the authenticated principal → `customer_id`; never let the client name the tenant. |
| **Learning latency** | Learning runs **synchronously** in the request so the UI can show this turn's ops. The endpoint already isolates it from the response. | Move `memory.add(...)` to a background worker/queue; return the reply immediately. |
| **Retrieval at scale** | Exact per-customer scan; optimal up to ~low thousands of active memories per customer. | Past that ceiling, add a per-customer partial vector index or partition `memories` by `customer_id`. The no-global-index argument still holds *within* a partition. |
| **Schema migrations** | Idempotent `CREATE ... IF NOT EXISTS` plus best-effort `ALTER`s, logged. | Adopt a versioned migration tool (e.g. Alembic). |
| **Right to erasure** | `delete_customer` hard-deletes everything; per-fact `forget()` soft-deletes and *retains* ledger history (provenance vs erasure tension). | Add a hard-forget that purges the memory row and its `memory_events` when compliance requires it. |
| **Edge concerns** | No rate limiting, CORS is default, secrets via env. | Add rate limiting, tighten CORS, and use a secrets manager. |
| **Prompt-injection surface** | Recalled memory text is rendered into the agent's system prompt. Blast radius is one customer's own future turns (memory is per-customer). | Keep the memory block clearly delimited as data, and consider a stored-content policy check. |

---

## Where Ledger fits

An honest read against the field. Ledger's edge is provenance, determinism, and the grounding
write-gate; it is intentionally *not* a graph/agentic-memory framework.

| System | Its strength | What Ledger has that it doesn't | What it has that Ledger doesn't |
|--------|--------------|----------------------------------|----------------------------------|
| **mem0** | Provider-agnostic, optional graph memory, user/session/agent scopes. | Same-transaction provenance, grounding-gated writes, deterministic rerank, built-in PII scrub. | Pluggable LLM/embedder/vector store; graph traversal; managed platform. |
| **Zep (Graphiti)** | Bitemporal knowledge graph, typed entities/edges, hybrid search. | Radically simpler ops (just Postgres), deterministic hot path, mutation-level audit, grounding gate. | A real graph and richer temporal query surface (Ledger reconstructs point-in-time, but stores flat per-customer facts, not entities/relations). |
| **LangMem** | Semantic/episodic/procedural typing, agent-callable memory tools, background consolidation. | Auditability, reproducible reconciliation, fail-closed grounding, a self-contained running system. | Procedural memory; memory-as-a-tool; summarising consolidation. |
| **Letta / MemGPT** | Self-editing in-context memory, context paging, shared agent memory. | Auditable, deterministic, non-self-editing memory a hallucination can't rewrite; clean engine/agent separation. | Agent-managed memory and virtual context management. |

The clearest roadmap items — entity/relationship links between facts, memory consolidation, and
pluggable providers — are in [Limitations & roadmap](#limitations--roadmap). Each is weighed
against the deterministic-first ethos rather than adopted for parity's sake.

---

## Repository layout

```
server/                FastAPI backend + the memory engine
  memory.py            the engine: write path (extract → gate → reconcile → cap) + read path (retrieve → rerank)
  store.py             Postgres/pgvector access: memories, event ledger, point-in-time reconstruction, sessions
  scrub.py             deterministic PII redaction (card/OTP/PIN/account/SSN/email/phone) via regex + Luhn
  grounding.py         draft → grade-against-rubric → revise loop for the demo assistant
  agent.py             the assistant's two LLM moves: draft a reply, revise a flagged one
  prompts.py           every LLM system prompt, in one place
  main.py              API routes (/api/*) and app wiring
  seed.py              loads the six demo customers (idempotent; --reset to wipe first)
  tests/               deterministic-core tests — run with no DB and no API key
ui/src/                React + Vite frontend
  App.tsx              customer picker, onboarding form, page layout
  Chat.tsx             chat panel + the grounding verdict trail
  MemoryPanel.tsx      live memory view and each fact's audit trail
  SessionsPanel.tsx    per-customer session list
  api.ts               typed client for the backend
Dockerfile             multi-stage build: bundles the UI, serves it behind the API
docker-compose.yml     turn-key local stack: pgvector + server
```

---

## Local setup

**Prerequisites:** Python 3.11+, Node.js 20+, an OpenAI API key, and a PostgreSQL connection
string with the `pgvector` extension available (e.g. Supabase).

### Option A — Docker (turn-key)

```bash
OPENAI_API_KEY=sk-... docker compose up --build
docker compose exec server python seed.py     # load the six demo customers
# open http://localhost:8000
```

### Option B — run the pieces directly

**Backend**
```bash
cd server
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then set OPENAI_API_KEY and DATABASE_URL
python seed.py                # create tables + load the six demo customers (idempotent)
uvicorn main:app --reload --env-file .env
```

**Frontend**
```bash
cd ui
npm install
npm run dev                   # proxies /api to the backend (port 8000 by default)
```

### Tests

The deterministic core runs with **no database and no API key** — every LLM/embedding/DB call is
stubbed, which is exactly what the deterministic-first design buys you:

```bash
cd server
.venv/bin/python -m pytest tests/ -q
```

---

## API reference

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Liveness (process up). |
| GET | `/api/ready` | Readiness (can reach Postgres); `503` if not. |
| GET / POST | `/api/customers` | List or create customers. |
| DELETE | `/api/customers/{id}` | Delete a customer and all their data. |
| POST | `/api/sessions` | Start a session. |
| GET | `/api/customers/{id}/sessions` | List a customer's sessions. |
| GET | `/api/sessions/{id}/messages` | Messages in a session. |
| PATCH | `/api/sessions/{id}` | Rename a session. |
| DELETE | `/api/sessions/{id}` | Delete a session. |
| POST | `/api/chat` | Submit a turn → reply, memories recalled, ops applied, grounding trail. |
| GET | `/api/memories/{id}` | Active memories for a customer. |
| GET | `/api/customers/{id}/memories-as-of?t=<iso>` | Point-in-time recall reconstructed from the ledger. |
| GET | `/api/memory/{id}/history` | Audit trail for one memory. |
| DELETE | `/api/memory/{id}` | Forget a fact (soft-delete). |

---

## Limitations & roadmap

Honest gaps, each weighed against the deterministic-first ethos rather than adopted for parity:

- **Flat facts, no entity graph.** Memories are per-customer strings with no links between them.
  Entity/relationship edges (the Zep/mem0-graph capability) would enable multi-hop recall; the
  fit with a deterministic, auditable store is the open design question.
- **No consolidation.** Episodes are capped and evicted, never summarised into a durable digest.
  A periodic, journalled consolidation pass is a natural, ethos-preserving addition.
- **Single provider, single scope.** OpenAI is hardcoded and memory is scoped only by customer.
  A pluggable embedder/LLM boundary and session/agent scopes are additive.
- **No retrieval eval harness.** The deterministic core is unit-tested, but recall *quality* has
  no benchmark (LOCOMO/LongMemEval-style). A fixed-corpus eval would make ranking changes
  measurable — and it fits the ethos, because the read path is deterministic.

---

## License

See the repository for license details.

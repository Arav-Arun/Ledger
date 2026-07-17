"""Postgres store: memories, the append-only event ledger, sessions, messages.

Plain SQL over a small psycopg pool. Vectors go in as pgvector literals;
similarity search happens in SQL (`<=>` is cosine distance).
"""

import logging
import os

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

log = logging.getLogger("ledger.store")

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS customers (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS memories (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id TEXT NOT NULL REFERENCES customers(id),
    text        TEXT NOT NULL,
    category    TEXT NOT NULL DEFAULT 'episode',
    embedding   vector(1536) NOT NULL,
    active      BOOLEAN NOT NULL DEFAULT true,
    expires_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_memories_customer ON memories (customer_id) WHERE active;

CREATE TABLE IF NOT EXISTS memory_events (
    id          BIGSERIAL PRIMARY KEY,
    memory_id   UUID NOT NULL,
    customer_id TEXT NOT NULL,
    op          TEXT NOT NULL,
    old_text    TEXT,
    new_text    TEXT,
    source      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_events_memory ON memory_events (memory_id);

CREATE TABLE IF NOT EXISTS sessions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id TEXT NOT NULL REFERENCES customers(id),
    title       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);



CREATE TABLE IF NOT EXISTS messages (
    id          BIGSERIAL PRIMARY KEY,
    session_id  UUID NOT NULL REFERENCES sessions(id),
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages (session_id)
"""

_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise RuntimeError(
                "DATABASE_URL is not set - add it to server/.env "
                "(a Postgres connection string with the pgvector extension available)."
            )
        _pool = ConnectionPool(
            url,
            min_size=1,
            max_size=5,
            open=True,
            # prepare_threshold=None keeps transaction poolers (PgBouncer, Supavisor) happy;
            # search_path covers setups where pgvector lives in `extensions`
            kwargs={
                "autocommit": True,
                "row_factory": dict_row,
                "prepare_threshold": None,
                "options": "-c search_path=public,extensions",
            },
        )
    return _pool


def close() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def init() -> None:
    with pool().connection() as conn:
        for stmt in SCHEMA.split(";"):
            if stmt.strip():
                try:
                    conn.execute(stmt)
                except Exception as e:
                    if "CREATE EXTENSION" in stmt.upper():
                        raise RuntimeError(
                             "pgvector is not enabled - run `CREATE EXTENSION vector;` "
                             "in your database (requires superuser or rds_superuser)."
                        ) from e
                    raise
        # Auto-migration from older schema versions. Best-effort per statement: an
        # already-migrated DB no-ops, but a genuine failure is now logged instead of
        # vanishing silently (and one bad step no longer blocks the others).
        for label, migration in (
            ("drop legacy importance column",
             "ALTER TABLE memories DROP COLUMN IF EXISTS importance;"),
            ("coerce memory_id to uuid",
             "ALTER TABLE memory_events ALTER COLUMN memory_id TYPE UUID USING memory_id::uuid;"),
            # There is deliberately NO vector index (this drops the HNSW one earlier
            # versions built). Every query here is scoped to one customer, and
            # `customer_id` is far more selective than the vector search, so the plan we
            # want is: narrow by idx_memories_customer, then scan that customer's rows
            # exactly. An HNSW index over every customer's vectors cannot serve that -
            # it is built for a global nearest-neighbour search we never issue.
            #
            # It was not merely useless, it was a hazard. pgvector defaults
            # `hnsw.iterative_scan` to off, so an HNSW scan combined with a selective
            # filter that isn't in the index (our customer_id) can return FEWER rows than
            # the LIMIT asked for. Silently. If the planner ever picked it, the write
            # path's neighbour window would come back short or empty, gate() would take
            # `if not neighbors: return ADD`, and reconciliation would degrade to
            # append-only with no error raised - unbounded growth caused by an index.
            # It also cost an HNSW insert on every single memory write.
            ("drop unused hnsw vector index",
             "DROP INDEX IF EXISTS idx_memories_embedding;"),
        ):
            try:
                conn.execute(migration)
            except Exception as e:
                log.warning("auto-migration step skipped (%s): %s", label, e)


def _vec(embedding: list[float]) -> str:
    """Formats a float embedding list into a pgvector bracketed string literal format.
    
    Example: [0.1, 0.2, ...] -> "[0.1000000,0.2000000,...]"
    """
    return "[" + ",".join(f"{x:.7f}" for x in embedding) + "]"


def _q(sql: str, params: tuple = ()) -> list[dict]:
    """Helper query runner that gets a connection from the pool, executes the SQL,
    and returns matching rows as a list of dicts.
    """
    with pool().connection() as conn:
        cur = conn.execute(sql, params)
        # If the cursor description exists, it indicates a return set (e.g. SELECT/RETURNING)
        return cur.fetchall() if cur.description else []


# -- customers ---------------------------------------------------------------

def create_customer(id: str, name: str) -> dict:
    """Creates a new customer profile record with the given ID and name."""
    return _q("INSERT INTO customers (id, name) VALUES (%s, %s) RETURNING id, name", (id, name))[0]


def get_customer(id: str) -> dict | None:
    """Queries and returns a customer profile record by its primary ID, or None if not found."""
    rows = _q("SELECT id, name FROM customers WHERE id = %s", (id,))
    return rows[0] if rows else None


def list_customers() -> list[dict]:
    """Lists all customers along with the count of active, non-expired memories
    linked to each customer record. Ordered by creation date.
    """
    return _q(
        """SELECT c.id, c.name, count(m.id) AS memory_count
           FROM customers c
           LEFT JOIN memories m ON m.customer_id = c.id AND m.active
             AND (m.expires_at IS NULL OR m.expires_at > now())
           GROUP BY c.id ORDER BY c.created_at"""
    )


# -- memories ----------------------------------------------------------------

def insert_memory(customer_id: str, text: str, category: str,
                  embedding: list[float], expires_at=None, source: str = "") -> str:
    """Inserts a new vector-embedded memory for a customer.

    The row and its ADD ledger event are written in one transaction, so the audit
    trail can never disagree with the memories table (no orphaned row or event).
    """
    with pool().connection() as conn:
        with conn.transaction():
            mid = conn.execute(
                """INSERT INTO memories (customer_id, text, category, embedding, expires_at)
                   VALUES (%s, %s, %s, %s::vector, %s) RETURNING id::text""",
                (customer_id, text, category, _vec(embedding), expires_at),
            ).fetchall()[0]["id"]
            _log_event(conn, mid, customer_id, "ADD", None, text, source)
    return mid


def update_memory(memory_id: str, new_text: str, embedding: list[float], source: str = "") -> None:
    """Updates the text content and embedding vector of an active memory.

    The read, write, and ledger event run in one transaction with the row locked
    (FOR UPDATE), so a concurrent deactivate cannot slip in between the active-check
    and the write (no TOCTOU); the UPDATE itself also re-checks `active`.
    """
    with pool().connection() as conn:
        with conn.transaction():
            old = conn.execute(
                "SELECT customer_id, text FROM memories WHERE id = %s::uuid AND active FOR UPDATE",
                (memory_id,),
            ).fetchall()
            if not old:
                return
            conn.execute(
                """UPDATE memories
                   SET text = %s, embedding = %s::vector, updated_at = now()
                   WHERE id = %s::uuid AND active""",
                (new_text, _vec(embedding), memory_id),
            )
            _log_event(conn, memory_id, old[0]["customer_id"], "UPDATE", old[0]["text"], new_text, source)


def deactivate_memory(memory_id: str, op: str = "DELETE", source: str = "") -> None:
    """Soft-deletes a memory by setting active = false.

    The soft-delete and its ledger event are written in one transaction, so the
    deletion is always recorded (or not applied at all).
    """
    with pool().connection() as conn:
        with conn.transaction():
            rows = conn.execute(
                """UPDATE memories SET active = false, updated_at = now()
                   WHERE id = %s::uuid AND active
                   RETURNING customer_id, text""",
                (memory_id,),
            ).fetchall()
            if rows:
                _log_event(conn, memory_id, rows[0]["customer_id"], op, rows[0]["text"], None, source)


def expire_sweep(customer_id: str) -> None:
    """Sweeps and soft-deletes all memories for a customer that have passed their
    expiry date, logging an EXPIRE event per affected memory.

    The sweep and all its ledger events share one transaction, so a mid-sweep crash
    can't leave some rows flipped without their matching EXPIRE events.
    """
    with pool().connection() as conn:
        with conn.transaction():
            rows = conn.execute(
                """UPDATE memories SET active = false, updated_at = now()
                   WHERE customer_id = %s AND active AND expires_at <= now()
                   RETURNING id::text, text""",
                (customer_id,),
            ).fetchall()
            for r in rows:
                _log_event(conn, r["id"], customer_id, "EXPIRE", r["text"], None, "expiry sweep")


def evict_surplus_episodes(customer_id: str, keep: int) -> list[dict]:
    """Cap the one category that grows without bound; the newest `keep` survive.

    Growth is otherwise self-limiting. A preference or profile fact is UPDATEd in place
    when it changes, so a customer who moves five times still has one address memory, and
    issue/commitment track real events a customer actually raises. `episode` is the only
    genuinely additive category - a new trip does not overwrite an older one, and its
    expires_at is optional, set only when the fact can be dated - so it is the only one
    that needs a ceiling.

    Evicts oldest-first and journals an EVICT per row in the same transaction, so a fact
    never leaves without the ledger recording why. Deliberately scoped to episodes: an
    open commitment is an obligation we made to a customer, and silently forgetting one is
    worse than carrying it stale.
    """
    with pool().connection() as conn:
        with conn.transaction():
            rows = conn.execute(
                """UPDATE memories SET active = false, updated_at = now()
                   WHERE id IN (
                       SELECT id FROM memories
                       WHERE customer_id = %s AND active AND category = 'episode'
                         AND (expires_at IS NULL OR expires_at > now())
                       ORDER BY created_at DESC
                       OFFSET %s
                   )
                   RETURNING id::text, text""",
                (customer_id, keep),
            ).fetchall()
            for r in rows:
                _log_event(conn, r["id"], customer_id, "EVICT", r["text"], None,
                           f"episode cap of {keep} exceeded")
    return rows


def get_memories(customer_id: str) -> list[dict]:
    """Queries all active and non-expired memories for a customer ordered by recency."""
    return _q(
        """SELECT id::text, text, category, expires_at, created_at, updated_at
           FROM memories
           WHERE customer_id = %s AND active AND (expires_at IS NULL OR expires_at > now())
           ORDER BY updated_at DESC""",
        (customer_id,),
    )


def similar_memories(customer_id: str, embedding: list[float], k: int = 5) -> list[dict]:
    """Performs a pgvector cosine similarity search (`<=>`) on active, non-expired
    memories for a customer. Returns up to k elements sorted by similarity.

    Both paths use this: the write path takes a small k (the neighbours to reconcile
    against), the read path takes a large one (the pool to rerank - see
    memory.RERANK_FETCH). The `customer_id` filter is far more selective than the vector
    search, so this is an exact scan over one customer's rows either way.
    """
    v = _vec(embedding)
    return _q(
        """SELECT id::text, text, category, created_at, updated_at,
                  1 - (embedding <=> %s::vector) AS similarity
           FROM memories
           WHERE customer_id = %s AND active AND (expires_at IS NULL OR expires_at > now())
           ORDER BY embedding <=> %s::vector
           LIMIT %s""",
        (v, customer_id, v, k),
    )


# -- the ledger (audit log) --------------------------------------------------

def _log_event(conn, memory_id: str, customer_id: str, op: str,
               old_text: str | None, new_text: str | None, source: str) -> None:
    """Append one row to the audit ledger on an EXISTING connection, so it shares the
    caller's transaction with the memory mutation it describes."""
    conn.execute(
        """INSERT INTO memory_events (memory_id, customer_id, op, old_text, new_text, source)
           VALUES (%s::uuid, %s, %s, %s, %s, %s)""",
        (memory_id, customer_id, op, old_text, new_text, (source or "")[:500]),
    )


def log_event(memory_id: str, customer_id: str, op: str,
              old_text: str | None, new_text: str | None, source: str) -> None:
    """Standalone ledger append (opens its own connection). The in-module mutations use
    _log_event to stay in one transaction; this wrapper exists for any external caller."""
    with pool().connection() as conn:
        _log_event(conn, memory_id, customer_id, op, old_text, new_text, source)


def memory_history(memory_id: str) -> list[dict]:
    return _q(
        """SELECT op, old_text, new_text, source, created_at
           FROM memory_events WHERE memory_id = %s ORDER BY created_at""",
        (memory_id,),
    )


# -- sessions & messages -----------------------------------------------------

def create_session(customer_id: str) -> str:
    return _q("INSERT INTO sessions (customer_id) VALUES (%s) RETURNING id::text", (customer_id,))[0]["id"]


def list_sessions(customer_id: str) -> list[dict]:
    return _q(
        """SELECT s.id::text, s.title, s.created_at,
                  (SELECT count(*) FROM messages m WHERE m.session_id = s.id) AS message_count
           FROM sessions s
           WHERE s.customer_id = %s
           ORDER BY s.created_at DESC""",
        (customer_id,),
    )


def rename_session(session_id: str, title: str) -> None:
    _q("UPDATE sessions SET title = %s WHERE id = %s::uuid RETURNING id", (title, session_id))


def add_message(session_id: str, role: str, content: str) -> None:
    _q("INSERT INTO messages (session_id, role, content) VALUES (%s::uuid, %s, %s) RETURNING id",
       (session_id, role, content))


def get_messages(session_id: str) -> list[dict]:
    return _q("SELECT role, content FROM messages WHERE session_id = %s::uuid ORDER BY id", (session_id,))


def delete_customer(customer_id: str) -> None:
    with pool().connection() as conn:
        with conn.transaction():
            conn.execute(
                "DELETE FROM messages WHERE session_id IN (SELECT id FROM sessions WHERE customer_id = %s)",
                (customer_id,),
            )
            conn.execute("DELETE FROM sessions WHERE customer_id = %s", (customer_id,))
            conn.execute("DELETE FROM memory_events WHERE customer_id = %s", (customer_id,))
            conn.execute("DELETE FROM memories WHERE customer_id = %s", (customer_id,))
            conn.execute("DELETE FROM customers WHERE id = %s", (customer_id,))


def delete_session(session_id: str) -> None:
    with pool().connection() as conn:
        with conn.transaction():
            conn.execute("DELETE FROM messages WHERE session_id = %s::uuid", (session_id,))
            conn.execute("DELETE FROM sessions WHERE id = %s::uuid", (session_id,))



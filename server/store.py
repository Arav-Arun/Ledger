"""Postgres store: memories, the append-only event ledger, sessions, messages.

Plain SQL over a small psycopg pool. Vectors go in as pgvector literals;
similarity search happens in SQL (`<=>` is cosine distance).
"""

import os

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

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
        _pool = ConnectionPool(
            os.environ["DATABASE_URL"],
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
        # Auto-migration: drop column and fix type mismatch if migrating from old version
        try:
            conn.execute("ALTER TABLE memories DROP COLUMN IF EXISTS importance;")
            conn.execute("ALTER TABLE memory_events ALTER COLUMN memory_id TYPE UUID USING memory_id::uuid;")
        except Exception:
            pass


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
    
    Also logs the addition to the memory audit event log.
    """
    mid = _q(
        """INSERT INTO memories (customer_id, text, category, embedding, expires_at)
           VALUES (%s, %s, %s, %s::vector, %s) RETURNING id::text""",
        (customer_id, text, category, _vec(embedding), expires_at),
    )[0]["id"]
    log_event(mid, customer_id, "ADD", None, text, source)
    return mid


def update_memory(memory_id: str, new_text: str, embedding: list[float], source: str = "") -> None:
    """Updates the text content and embedding vector of an active memory.
    
    Also appends an update event to the memory audit event log.
    """
    old = _q("SELECT customer_id, text FROM memories WHERE id = %s::uuid AND active", (memory_id,))
    if not old:
        return
    _q(
        """UPDATE memories
           SET text = %s, embedding = %s::vector, updated_at = now()
           WHERE id = %s::uuid RETURNING id""",
        (new_text, _vec(embedding), memory_id),
    )
    log_event(memory_id, old[0]["customer_id"], "UPDATE", old[0]["text"], new_text, source)


def deactivate_memory(memory_id: str, op: str = "DELETE", source: str = "") -> None:
    """Soft-deletes a memory by setting active = false.
    
    Documents the deletion with a record in the memory events audit table.
    """
    rows = _q(
        """UPDATE memories SET active = false, updated_at = now()
           WHERE id = %s::uuid AND active
           RETURNING customer_id, text""",
        (memory_id,),
    )
    if rows:
        log_event(memory_id, rows[0]["customer_id"], op, rows[0]["text"], None, source)


def expire_sweep(customer_id: str) -> None:
    """Sweeps and soft-deletes all memories for a customer that have passed
    their expiry date. Logs EXPIRE event for each affected memory.
    """
    rows = _q(
        """UPDATE memories SET active = false, updated_at = now()
           WHERE customer_id = %s AND active AND expires_at <= now()
           RETURNING id::text, text""",
        (customer_id,),
    )
    for r in rows:
        log_event(r["id"], customer_id, "EXPIRE", r["text"], None, "expiry sweep")


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

def log_event(memory_id: str, customer_id: str, op: str,
              old_text: str | None, new_text: str | None, source: str) -> None:
    _q(
        """INSERT INTO memory_events (memory_id, customer_id, op, old_text, new_text, source)
           VALUES (%s::uuid, %s, %s, %s, %s, %s) RETURNING id""",
        (memory_id, customer_id, op, old_text, new_text, (source or "")[:500]),
    )


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



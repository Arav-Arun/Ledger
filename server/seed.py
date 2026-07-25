"""Seed the six demo customers and their starting memories.

Idempotent by default: a demo customer that already exists is left untouched, so re-running
is safe and cheap. Pass --reset to wipe ALL data first (destructive; for a clean demo env).

Reuses the engine's own embedding call (memory.embed) rather than a second OpenAI client, so
seeded vectors are produced exactly the way the write path produces them, and batches one
embedding request per customer instead of one per memory.

Run:  python seed.py            # create any missing demo customers (safe to repeat)
      python seed.py --reset    # wipe everything first, then seed
"""

import argparse
import logging
import os
import sys

from dotenv import load_dotenv

# Ensure sibling modules import whether run as `python seed.py` or `python -m seed`.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import memory
import store

load_dotenv()
logging.basicConfig(
    level=os.getenv("LEDGER_LOG_LEVEL", "INFO"),
    format="%(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("ledger.seed")

DEFAULT_CUSTOMERS = [
    {
        "id": "priya_sharma",
        "name": "Priya Sharma",
        "memories": [
            {"text": "Customer has a pending refund on a returned coffee grinder (return RET-4821, glass cracked on arrival).", "category": "issue"},
            {"text": "Customer was promised the coffee-grinder refund would be processed within 5-7 working days.", "category": "commitment"},
            {"text": "Customer prefers short, jargon-free replies.", "category": "preference"},
            {"text": "Customer prefers deliveries left with building security, never at the door.", "category": "preference"},
            {"text": "Customer is vegetarian and avoids purchasing leather items.", "category": "profile"},
        ],
    },
    {
        "id": "rahul_verma",
        "name": "Rahul Verma",
        "memories": [
            {"text": "Customer is travelling abroad and requested to pause all deliveries.", "category": "episode"},
            {"text": "Customer was promised a 10% goodwill discount code on their next order.", "category": "commitment"},
            {"text": "Customer prefers to receive updates via WhatsApp.", "category": "preference"},
            {"text": "Customer mainly purchases camera and photography equipment.", "category": "profile"},
        ],
    },
    {
        "id": "vikram_nair",
        "name": "Vikram Nair",
        "memories": [
            {"text": "Customer reported a desk lamp (order ORD-2290) marked delivered but never received; escalation is open.", "category": "issue"},
            {"text": "Customer was promised a free replacement desk lamp.", "category": "commitment"},
            {"text": "Customer works late-night shifts and requested delivery drivers call only before 11:00 AM.", "category": "preference"},
            {"text": "Customer lives in a gated community requiring couriers to check in at the front gate.", "category": "profile"},
        ],
    },
    {
        "id": "fatima_sheikh",
        "name": "Fatima Sheikh",
        "memories": [
            {"text": "Customer is waiting on a refund for returned running shoes (order ORD-5512, wrong size).", "category": "issue"},
            {"text": "Customer was issued a 500 INR store credit as an apology for shipping delays.", "category": "commitment"},
            {"text": "Customer prefers receiving updates via WhatsApp.", "category": "preference"},
            {"text": "Customer primarily purchases kids' clothing and toys as gifts.", "category": "profile"},
        ],
    },
    {
        "id": "daniel_thomas",
        "name": "Daniel Thomas",
        "memories": [
            {"text": "Customer prefers orders to be gift-wrapped with a handwritten gift note.", "category": "preference"},
            {"text": "Customer prefers shipping to office on weekdays and home address on weekends.", "category": "profile"},
            {"text": "Customer is sensitive to strong chemical fragrances.", "category": "profile"},
            {"text": "Customer prefers email communications and dislikes phone calls.", "category": "preference"},
        ],
    },
    {
        "id": "ananya_iyer",
        "name": "Ananya Iyer",
        "memories": [
            {"text": "Customer recently signed up and prefers SMS updates.", "category": "preference"},
            {"text": "Customer is highly interested in fitness, home workout gear, and yoga.", "category": "profile"},
        ],
    },
]


def _wipe() -> None:
    """Delete ALL data in one transaction. Destructive; only reached via --reset."""
    log.warning("--reset: deleting ALL customers, memories, events, sessions, and messages")
    with store.pool().connection() as conn:
        with conn.transaction():
            # Child rows first to respect foreign keys. Table names are hard-coded, not input.
            for table in ("messages", "sessions", "memory_events", "memories", "customers"):
                conn.execute(f"DELETE FROM {table}")


def seed(reset: bool = False) -> None:
    log.info("initialising schema")
    store.init()
    if reset:
        _wipe()

    created = 0
    for cust in DEFAULT_CUSTOMERS:
        if store.get_customer(cust["id"]):
            log.info("customer %s already exists, skipping", cust["id"])
            continue
        store.create_customer(cust["id"], cust["name"])
        # One batched embedding call per customer, via the engine's own embed().
        embeddings = memory.embed([m["text"] for m in cust["memories"]])
        for m, emb in zip(cust["memories"], embeddings):
            store.insert_memory(cust["id"], m["text"], m["category"], emb, source="initial seeding")
        created += 1
        log.info("seeded %s with %d memories", cust["name"], len(cust["memories"]))

    store.close()
    log.info("seeding complete: %d new customer(s), %d already present",
             created, len(DEFAULT_CUSTOMERS) - created)


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed Ledger's demo customers.")
    parser.add_argument("--reset", action="store_true",
                        help="wipe ALL data before seeding (destructive)")
    args = parser.parse_args()

    missing = [v for v in ("OPENAI_API_KEY", "DATABASE_URL") if not os.getenv(v)]
    if missing:
        log.error("missing required environment variable(s): %s (see .env.example)",
                  ", ".join(missing))
        return 1
    try:
        seed(reset=args.reset)
    except Exception as e:
        log.error("seeding failed: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""FastAPI app: the JSON API under /api/*, plus the built UI as static files."""

import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import grounding
import store
from memory import CATEGORIES, Memory, embed
from scrub import scrub

log = logging.getLogger("ledger.chat")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Surface the engine's own signals (fail-closed grounding, dropped memories) in the
    # server log - the read/write paths were previously silent.
    logging.basicConfig(level=os.getenv("LEDGER_LOG_LEVEL", "INFO"))
    store.init()
    yield
    store.close()


app = FastAPI(title="Ledger", lifespan=lifespan)
memory = Memory()


class InitialMemory(BaseModel):
    text: str = Field(min_length=1, max_length=280)
    category: str = "profile"


class CustomerBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    memories: list[InitialMemory] = Field(default_factory=list, max_length=12)


class SessionBody(BaseModel):
    customer_id: str


class SessionRenameBody(BaseModel):
    title: str = Field(min_length=1, max_length=60)


class ChatBody(BaseModel):
    customer_id: str
    session_id: str
    message: str = Field(min_length=1)


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/customers")
def customers():
    return store.list_customers()


@app.post("/api/customers")
def create_customer(body: CustomerBody):
    cid = f"cust_{uuid.uuid4().hex[:8]}"
    customer = store.create_customer(cid, body.name.strip())
    # onboarding values become the first memories; scrub PII, skip if empty
    inserted = 0
    for m in body.memories:
        clean, _ = scrub(m.text)
        clean = clean.strip()
        if not clean:
            continue
        category = m.category if m.category in CATEGORIES else "profile"
        store.insert_memory(cid, clean, category,
                            embed([clean])[0], None, source="onboarding")
        inserted += 1
    customer["memory_count"] = inserted
    return customer


@app.post("/api/sessions")
def create_session(body: SessionBody):
    if not store.get_customer(body.customer_id):
        raise HTTPException(404, "unknown customer")
    return {"session_id": store.create_session(body.customer_id)}


@app.post("/api/chat")
def chat(body: ChatBody):
    customer = store.get_customer(body.customer_id)
    if not customer:
        raise HTTPException(404, "unknown customer")

    clean, redactions = scrub(body.message)
    store.add_message(body.session_id, "user", clean)
    history = store.get_messages(body.session_id)

    recalled = memory.search(clean, body.customer_id, recent=history[:-1], k=6)
    memory_block = Memory.prompt_block(recalled)

    # draft -> grade against the rubric -> revise until grounded or the cap is hit.
    # `grounding_trail` is every attempt and its per-criterion verdict, for the UI/audit;
    # `grounded` is True only if the reply passed every rubric criterion.
    reply, grounding_trail, grounded = grounding.answer(
        customer["name"], history, memory_block, body.customer_id)
    store.add_message(body.session_id, "assistant", reply)

    # Only learn from a reply that passed grounding. An ungrounded draft may contain
    # invented specifics; extracting "facts" from it would launder a hallucination into
    # permanent memory. We still learn from the customer's own message (pass an empty
    # assistant turn), just not from an unverified assistant reply.
    # sync in the demo so the UI can show this turn's ops; a background task in prod.
    # history already contains the user message that was just stored;
    # [:-1] strips it so we only pass prior context, [-6:] caps the window.
    learn_reply = reply if grounded else ""
    if not grounded:
        log.warning("reply for %s was not grounded; not learning from it", body.customer_id)
    events = memory.add(clean, learn_reply, body.customer_id, recent=history[:-1][-6:])

    return {"reply": reply, "memories_used": recalled, "events": events,
            "redactions": redactions, "grounding": grounding_trail, "grounded": grounded}


@app.get("/api/memories/{customer_id}")
def memories(customer_id: str):
    return memory.get_all(customer_id)


@app.get("/api/memory/{memory_id}/history")
def history(memory_id: str):
    return memory.history(memory_id)


@app.delete("/api/memory/{memory_id}")
def forget(memory_id: str):
    memory.forget(memory_id)
    return {"ok": True}


@app.get("/api/customers/{customer_id}/sessions")
def sessions(customer_id: str):
    return store.list_sessions(customer_id)


@app.get("/api/sessions/{session_id}/messages")
def session_messages(session_id: str):
    return store.get_messages(session_id)


@app.delete("/api/customers/{customer_id}")
def delete_customer(customer_id: str):
    store.delete_customer(customer_id)
    return {"ok": True}


@app.patch("/api/sessions/{session_id}")
def rename_session(session_id: str, body: SessionRenameBody):
    title = body.title.strip()
    if not title:
        raise HTTPException(422, "title cannot be empty")
    store.rename_session(session_id, title)
    return {"ok": True, "title": title}


@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: str):
    store.delete_session(session_id)
    return {"ok": True}


ui_dist = Path(os.getenv("UI_DIST", Path(__file__).resolve().parent.parent / "ui" / "dist"))
if ui_dist.is_dir():
    app.mount("/", StaticFiles(directory=ui_dist, html=True), name="ui")

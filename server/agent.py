import json
from memory import llm_chat
from prompts import AGENT_SYSTEM

def respond(customer_name: str, messages: list[dict], memory_block: str, customer_id: str) -> str:
    """One agent turn: history (ending with the latest user message) -> reply."""
    msgs: list = [{
        "role": "system",
        "content": AGENT_SYSTEM.format(customer_name=customer_name, memory_block=memory_block),
    }] + [{"role": m["role"], "content": m["content"]} for m in messages]

    m = llm_chat(msgs)
    return m.content or "Sorry, I lost my train of thought - could you say that again?"

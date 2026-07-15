"""The demo assistant's two moves: draft a reply, and revise one the grader flagged.

Both go through memory.llm_chat (the one place that talks to OpenAI). The
draft->grade->revise loop that ties them together lives in grounding.py.
"""

from memory import llm_chat
from prompts import AGENT_SYSTEM, REVISE_SYSTEM


def respond(customer_name: str, messages: list[dict], memory_block: str) -> str:
    """Draft one reply: history (ending with the latest user message) -> reply."""
    msgs: list = [{
        "role": "system",
        "content": AGENT_SYSTEM.format(customer_name=customer_name, memory_block=memory_block),
    }] + [{"role": m["role"], "content": m["content"]} for m in messages]

    m = llm_chat(msgs)
    return m.content or "Sorry, I lost my train of thought - could you say that again?"


def revise(customer_name: str, messages: list[dict], memory_block: str,
           draft: str, failed_reasons: list[str]) -> str:
    """Rewrite a draft the grounding grader rejected, told only what failed and why.

    Same persona and same evidence as the draft, plus REVISE_SYSTEM and the list of
    rubric failures, so the assistant fixes exactly the ungrounded parts.
    """
    flagged = "\n".join(f"- {reason}" for reason in failed_reasons)
    msgs: list = [
        {"role": "system", "content": AGENT_SYSTEM.format(customer_name=customer_name, memory_block=memory_block)},
        {"role": "system", "content": REVISE_SYSTEM},
    ] + [{"role": m["role"], "content": m["content"]} for m in messages] + [
        {"role": "assistant", "content": draft},
        {"role": "user", "content": f"The grounding check flagged your draft:\n{flagged}\n\nSend a corrected reply."},
    ]

    m = llm_chat(msgs)
    return m.content or draft  # keep the draft if the model returns nothing

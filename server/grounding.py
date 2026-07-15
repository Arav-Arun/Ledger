"""Rubric-checked grounding: keep the assistant's replies tied to real evidence.

The loop, in the four steps it was asked for:

    1. the customer asks a question         -> comes in from the chat endpoint
    2. draft an answer from the evidence     -> agent.respond, using recalled memories
    3. a grader sub-agent scores the draft   -> grade_draft, one verdict per rubric line
       against the grounding RUBRIC
    4. revise and re-grade until it passes    -> the loop in answer(), capped at MAX_REVISIONS
       or we hit the iteration cap

Deterministic-first, on purpose (see the project's design rule):
- the RUBRIC is plain data you can read and edit in one place - the criteria are not
  hidden inside a prompt;
- the pass/fail decision is a plain Python rule (rubric_passed): every criterion must
  pass. The grader only produces one verdict per criterion; this Python rule - not the
  LLM - decides whether the draft ships;
- the loop has a hard cap (MAX_REVISIONS) so a stubborn draft can't spin forever;
- the grader reuses memory's temperature=0 + fixed-seed JSON call, so the same draft
  grades the same way the API allows.

FAIL CLOSED. "Grounded" means the grader returned a parseable, explicit pass on every
criterion. A missing verdict, a wrong-shaped response, a prose value where a bool was
expected, or a grader outage all count as NOT verified - i.e. not grounded - never as a
silent pass. A verification step that certifies output it never actually checked is worse
than no verification step, so the default is to withhold the "grounded" stamp.

The whole trail (every draft, its per-criterion verdict, whether the grader could even
evaluate it, whether it passed) is returned so the UI can show the reasoning and it can
be audited.
"""

import json
import logging

from pydantic import BaseModel, ValidationError, field_validator

import agent
from memory import _llm_json  # the one reproducible (temp 0 + seed) JSON call to OpenAI
from prompts import GRADER_SYSTEM

log = logging.getLogger("ledger.grounding")

# The rubric IS the config (the "RubricMiddleware" the ask referred to). Each line is one
# grounding criterion the grader must check. Change what "grounded" means by editing this
# list - nothing else in the loop needs to change.
RUBRIC = [
    {"name": "no_made_up_facts",
     "check": "Every concrete detail about this customer (order numbers, dates, prices, "
              "delivery or refund status, addresses, past promises) appears in the "
              "evidence. Nothing about the customer is invented."},
    {"name": "no_contradiction",
     "check": "The reply does not contradict anything in the evidence."},
    {"name": "asks_when_unknown",
     "check": "If the reply needs a fact that isn't in the evidence, it asks the customer "
              "for it instead of guessing or making one up."},
]

# Hard stop for step 4: at most this many rewrites before we send the best draft we have.
MAX_REVISIONS = 2

# Only these, as an explicit bare boolean or a recognised affirmative string, count as a
# pass. Everything else - "partial", "N/A", "unsure", null, a number, junk - is NOT a
# verified pass. Kept as a tuple so the accepted vocabulary is auditable in one place.
_AFFIRMATIVE = ("true", "yes", "1", "pass", "passed", "ok", "grounded")


class Verdict(BaseModel):
    """One rubric criterion's result from the grader.

    `verified` records whether the grader actually returned a usable verdict for this
    criterion. A verdict that was never evaluated (grader silent, wrong shape, outage)
    is verified=False, passed=False - it fails the rubric and it tells the loop that
    revising won't help (the grader is broken, not the draft).
    """
    name: str
    passed: bool = False   # fail closed: only an explicit, parseable pass flips this
    verified: bool = False
    reason: str = ""

    @field_validator("passed", mode="before")
    @classmethod
    def strict_bool(cls, v):
        """Read 'passed' strictly: only an explicit affirmative passes. Anything
        ambiguous (prose like 'partial', null, junk) is not a verified pass."""
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return v == 1
        if isinstance(v, str):
            return v.strip().lower() in _AFFIRMATIVE
        return False

    @field_validator("reason", mode="before")
    @classmethod
    def coerce_reason(cls, v):
        """A malformed sibling field must never discard a verdict - coerce it instead
        of letting a ValidationError throw the whole verdict away (and default it to
        a pass, as the old code did)."""
        if v is None:
            return ""
        return v if isinstance(v, str) else str(v)


def grade_draft(question: str, evidence: str, draft: str) -> list[Verdict]:
    """The grader sub-agent: score one draft against every rubric criterion.

    Returns one Verdict per RUBRIC line, in RUBRIC order. This only *judges* the draft;
    it never rewrites it. Fails closed: any criterion the grader did not clearly pass -
    including ones it never returned - comes back passed=False, verified=False.
    """
    payload = json.dumps({
        "question": question,
        "evidence": evidence,
        "draft_reply": draft,
        "rubric": RUBRIC,
    }, indent=2)

    try:
        raw = _llm_json(GRADER_SYSTEM, payload)
    except Exception as e:  # outage, non-JSON, timeout - do NOT certify, do NOT crash
        log.warning("grounding grader unavailable, failing closed: %s", e)
        return [Verdict(name=line["name"], reason=f"grader unavailable: {type(e).__name__}")
                for line in RUBRIC]
    if not isinstance(raw, dict):  # unexpected top-level shape (e.g. a JSON array) -> fail closed
        log.warning("grounding grader returned non-object %s; failing closed", type(raw).__name__)
        return [Verdict(name=line["name"], reason="grader returned an unexpected shape")
                for line in RUBRIC]

    # Map the model's verdicts back onto our rubric by name, so the result always has
    # exactly our criteria in our order.
    graded = {v.get("name"): v for v in (raw.get("checks") or []) if isinstance(v, dict)}
    verdicts: list[Verdict] = []
    for line in RUBRIC:
        got = graded.get(line["name"])
        if not isinstance(got, dict) or "passed" not in got:
            # criterion was not evaluated (missing, key/name drift, wrong shape)
            log.warning("grader returned no usable verdict for %r; failing it closed", line["name"])
            verdicts.append(Verdict(name=line["name"],
                                    reason="grader did not return a verdict for this criterion"))
            continue
        try:
            verdicts.append(Verdict(name=line["name"], verified=True,
                                    passed=got.get("passed"), reason=got.get("reason", "")))
        except (ValidationError, TypeError):
            verdicts.append(Verdict(name=line["name"], reason="grader verdict was malformed"))
    return verdicts


def rubric_passed(verdicts: list[Verdict]) -> bool:
    """The deterministic decision: the rubric passes only if every criterion passed.
    The LLM produces the verdicts; this rule - not the LLM - decides whether the draft
    ships as grounded.
    """
    return bool(verdicts) and all(v.passed for v in verdicts)


def _evidence(memory_block: str, messages: list[dict]) -> str:
    """The source material the reply must stay grounded in: what we recalled from memory,
    plus the conversation the drafter saw. The grader is shown exactly the same
    conversation the drafter was given - nothing more, nothing less - so the check is fair.
    """
    conversation = "\n".join(f"{m['role']}: {m['content']}" for m in messages)
    return (f"Recalled memories:\n{memory_block}\n\n"
            f"Conversation so far:\n{conversation or '(none)'}")


def answer(customer_name: str, messages: list[dict], memory_block: str,
           customer_id: str) -> tuple[str, list[dict], bool]:
    """Run the rubric-checked grounding loop and return (reply, trail, grounded).

    reply    - the best grounded reply we produced (or the best-scoring draft if we never
               fully passed within the cap).
    trail    - one entry per attempt {attempt, reply, passed, checks}, so the reasoning
               can be shown and audited.
    grounded - True only if the chosen reply passed every rubric criterion. main.py uses
               this to decide whether the reply is safe to learn from.
    """
    question = messages[-1]["content"] if messages else ""
    evidence = _evidence(memory_block, messages)

    # step 2: draft an answer from the evidence
    draft = agent.respond(customer_name, messages, memory_block, customer_id)

    trail: list[dict] = []
    best_key = None            # (fully_passed, n_criteria_passed) - higher is better
    best_reply, best_passed = draft, False

    for attempt in range(MAX_REVISIONS + 1):   # one draft, then up to MAX_REVISIONS rewrites
        # step 3: grade this draft against the rubric
        verdicts = grade_draft(question, evidence, draft)
        passed = rubric_passed(verdicts)
        trail.append({
            "attempt": attempt,
            "reply": draft,
            "passed": passed,
            "checks": [v.model_dump() for v in verdicts],
        })

        # keep the best draft seen so far (prefer a full pass, then more criteria passed;
        # ties keep the earliest, so the loop never regresses to a worse later rewrite)
        key = (1 if passed else 0, sum(1 for v in verdicts if v.passed))
        if best_key is None or key > best_key:
            best_key, best_reply, best_passed = key, draft, passed

        # step 4: stop when grounded or at the cap; otherwise revise the flagged parts
        if passed or attempt == MAX_REVISIONS:
            break
        # only real content critiques are worth a rewrite - if the grader couldn't even
        # evaluate (outage/shape), revising won't fix that, so stop and mark ungrounded
        content_failures = [v.reason for v in verdicts if v.verified and not v.passed]
        if not content_failures:
            log.warning("grounding could not be verified for %s; returning draft unflagged-as-grounded",
                        customer_id)
            break
        draft = agent.revise(customer_name, messages, memory_block, draft, content_failures)

    return best_reply, trail, best_passed

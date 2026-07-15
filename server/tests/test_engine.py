"""Deterministic-core tests for Ledger's read path and grounding loop.

These run with NO database and NO OpenAI key: every LLM/embedding/DB call is
stubbed. That is the point of the deterministic-first design - the parts that
decide behaviour are plain Python and can be pinned in a unit test.

Run:  .venv/bin/python -m pytest tests/ -q      (from the server/ directory)
"""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import grounding
import memory


# --------------------------------------------------------------------------
# Grounding grader: it must FAIL CLOSED. A draft is only "grounded" when the
# grader returns a parseable, explicit pass on every rubric criterion. Anything
# else (missing verdict, wrong shape, prose instead of a bool, an outage) must
# be treated as "not verified" = fail, never silently certified as a pass.
# --------------------------------------------------------------------------

def _grader(monkeypatch, response):
    """Make the grader's single LLM call return `response` (dict or raises)."""
    def fake(system, user):
        if isinstance(response, Exception):
            raise response
        return response
    monkeypatch.setattr(grounding, "_llm_json", fake)


def _all_pass_payload():
    return {"checks": [{"name": line["name"], "passed": True, "reason": "ok"}
                       for line in grounding.RUBRIC]}


def test_grounded_only_when_every_criterion_explicitly_passes(monkeypatch):
    _grader(monkeypatch, _all_pass_payload())
    verdicts = grounding.grade_draft("q", "evidence", "draft")
    assert grounding.rubric_passed(verdicts) is True
    assert all(v.passed for v in verdicts)


def test_empty_grader_response_fails_closed(monkeypatch):
    _grader(monkeypatch, {})
    verdicts = grounding.grade_draft("q", "evidence", "draft")
    assert grounding.rubric_passed(verdicts) is False
    assert all(not v.passed for v in verdicts)


def test_key_drift_fails_closed(monkeypatch):
    # grader returns the right data under the wrong key
    _grader(monkeypatch, {"results": [{"name": line["name"], "passed": True}
                                       for line in grounding.RUBRIC]})
    verdicts = grounding.grade_draft("q", "evidence", "draft")
    assert grounding.rubric_passed(verdicts) is False


def test_criterion_name_drift_fails_closed(monkeypatch):
    # grader uses a human label instead of the rubric's machine name
    _grader(monkeypatch, {"checks": [{"name": "no made up facts", "passed": True}]})
    verdicts = grounding.grade_draft("q", "evidence", "draft")
    assert grounding.rubric_passed(verdicts) is False


def test_explicit_fail_with_null_reason_is_respected_not_inverted(monkeypatch):
    # The dangerous one: an explicit FAIL whose reason is null must stay a FAIL.
    _grader(monkeypatch, {"checks": [
        {"name": "no_made_up_facts", "passed": False, "reason": None},
        {"name": "no_contradiction", "passed": True, "reason": "ok"},
        {"name": "asks_when_unknown", "passed": True, "reason": "ok"},
    ]})
    verdicts = grounding.grade_draft("q", "evidence", "draft")
    assert grounding.rubric_passed(verdicts) is False
    by_name = {v.name: v for v in verdicts}
    assert by_name["no_made_up_facts"].passed is False


def test_prose_passed_value_does_not_count_as_pass(monkeypatch):
    # "partial" / "N/A" / "unsure" must not be coerced into True.
    for junk in ("partial", "N/A", "unsure", "maybe"):
        _grader(monkeypatch, {"checks": [
            {"name": "no_made_up_facts", "passed": junk, "reason": "hmm"},
            {"name": "no_contradiction", "passed": True, "reason": "ok"},
            {"name": "asks_when_unknown", "passed": True, "reason": "ok"},
        ]})
        verdicts = grounding.grade_draft("q", "evidence", "draft")
        assert grounding.rubric_passed(verdicts) is False, junk


def test_grader_outage_fails_closed_without_raising(monkeypatch):
    _grader(monkeypatch, RuntimeError("openai down"))
    verdicts = grounding.grade_draft("q", "evidence", "draft")  # must not raise
    assert grounding.rubric_passed(verdicts) is False


def test_grader_non_dict_shape_fails_closed(monkeypatch):
    # a JSON array (or any non-object) at the top level must fail closed, not crash
    _grader(monkeypatch, [{"name": "no_made_up_facts", "passed": True}])
    verdicts = grounding.grade_draft("q", "evidence", "draft")  # must not raise
    assert grounding.rubric_passed(verdicts) is False


# --------------------------------------------------------------------------
# answer(): returns the BEST draft (not merely the last), reports whether it is
# grounded, and produces one trail entry per attempt.
# --------------------------------------------------------------------------

def _verdicts(passes):
    # verified=True: these represent verdicts the grader actually evaluated (a real
    # content pass/fail), as opposed to fail-closed placeholders.
    return [grounding.Verdict(name=line["name"], passed=p, verified=True,
                              reason="" if p else "flagged")
            for line, p in zip(grounding.RUBRIC, passes)]


def test_answer_returns_grounded_draft_after_revision(monkeypatch):
    drafts = iter(["draft-0", "draft-1-fixed"])
    monkeypatch.setattr(grounding.agent, "respond", lambda *a, **k: next(drafts))
    monkeypatch.setattr(grounding.agent, "revise", lambda *a, **k: next(drafts))
    grades = iter([_verdicts([False, True, True]), _verdicts([True, True, True])])
    monkeypatch.setattr(grounding, "grade_draft", lambda *a, **k: next(grades))

    reply, trail, grounded = grounding.answer("Priya", [{"role": "user", "content": "hi"}], "mem", "c1")
    assert grounded is True
    assert reply == "draft-1-fixed"
    assert len(trail) == 2 and trail[-1]["passed"] is True


def test_answer_picks_best_when_nothing_fully_passes(monkeypatch):
    drafts = iter(["d0", "d1", "d2"])
    monkeypatch.setattr(grounding.agent, "respond", lambda *a, **k: next(drafts))
    monkeypatch.setattr(grounding.agent, "revise", lambda *a, **k: next(drafts))
    # attempt 0 passes 2/3 (best), then it only gets worse. Never fully grounded.
    grades = iter([_verdicts([True, True, False]), _verdicts([True, False, False]),
                   _verdicts([False, False, False])])
    monkeypatch.setattr(grounding, "grade_draft", lambda *a, **k: next(grades))

    reply, trail, grounded = grounding.answer("Priya", [{"role": "user", "content": "hi"}], "mem", "c1")
    assert grounded is False
    assert reply == "d0"  # the better of the two, not the last


# --------------------------------------------------------------------------
# Deterministic contextual rerank: blends relevance (cosine) + importance
# (category prior) + recency, drops sub-floor memories, and is fully
# deterministic given a fixed clock.
# --------------------------------------------------------------------------

NOW = datetime(2026, 7, 15, tzinfo=timezone.utc)


def _row(text, category, similarity, age_days=1):
    return {"id": text, "text": text, "category": category, "similarity": similarity,
            "created_at": NOW - timedelta(days=age_days),
            "updated_at": NOW - timedelta(days=age_days)}


def test_rerank_drops_below_relevance_floor():
    rows = [_row("relevant", "profile", 0.6), _row("junk", "profile", 0.01)]
    out = memory.contextual_rerank([], "q", rows, k=6, now=NOW)
    ids = [r["id"] for r in out]
    assert "relevant" in ids and "junk" not in ids


def test_rerank_prefers_open_commitment_over_stable_profile_at_equal_similarity():
    rows = [_row("profile fact", "profile", 0.5),
            _row("open commitment", "commitment", 0.5)]
    out = memory.contextual_rerank([], "q", rows, k=6, now=NOW)
    assert out[0]["id"] == "open commitment"


def test_rerank_prefers_recent_at_equal_similarity_and_category():
    rows = [_row("old", "preference", 0.5, age_days=400),
            _row("fresh", "preference", 0.5, age_days=1)]
    out = memory.contextual_rerank([], "q", rows, k=6, now=NOW)
    assert out[0]["id"] == "fresh"


def test_rerank_caps_at_k():
    rows = [_row(f"m{i}", "profile", 0.5) for i in range(10)]
    out = memory.contextual_rerank([], "q", rows, k=6, now=NOW)
    assert len(out) == 6


def test_rerank_is_deterministic():
    rows = [_row("a", "issue", 0.4), _row("b", "commitment", 0.55), _row("c", "episode", 0.6)]
    a = memory.contextual_rerank([], "q", rows, k=3, now=NOW)
    b = memory.contextual_rerank([], "q", rows, k=3, now=NOW)
    assert [r["id"] for r in a] == [r["id"] for r in b]


def test_search_returns_scored_floored_topk(monkeypatch):
    monkeypatch.setattr(memory, "embed", lambda texts: [[0.0] * 1536])
    monkeypatch.setattr(memory.store, "expire_sweep", lambda cid: None)
    rows = [_row("keep-1", "commitment", 0.7), _row("keep-2", "issue", 0.5),
            _row("drop", "profile", 0.02)]
    monkeypatch.setattr(memory.store, "similar_memories", lambda cid, emb, k: rows)

    out = memory.Memory().search("any news?", "c1", recent=[], k=6)
    ids = [r["id"] for r in out]
    assert "drop" not in ids
    assert all("score" in r for r in out)
    assert out[0]["id"] == "keep-1"  # commitment, highest blended score

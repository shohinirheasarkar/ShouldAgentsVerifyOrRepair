"""Evaluator-only semantic dirty-bit check (Step 6.5), with an offline fake reader.

Run with ``python -m pytest -q tests/test_dirty_bit.py``; no models or network.
"""
import json
import re

import pytest

from verify_repair.dirty_bit import collect_stale_accesses, judge_sample, summarize, wilson_interval
from verify_repair.llm.client import FakeLLMClient
from verify_repair.llm.reader import AnswerReader
from verify_repair.models import ChangeType as CT
from verify_repair.simulator.handbuilt import workload_from_spec


def reading_reader(stale_errors=True):
    """Fake reader that actually reads the evidence: finds '<attribute> <value>' or abstains."""
    def handler(request):
        question = re.search(r"<question>\n(.*?)\n</question>", request.user_prompt, re.S).group(1)
        evidence = re.search(r"<evidence>\n(.*?)\n</evidence>", request.user_prompt, re.S).group(1)
        attribute = re.search(r"current (\w+) of", question).group(1)
        found = re.search(rf"\b{attribute} (\[retracted\]|\w+)", evidence)
        if found is None or found.group(1) == "[retracted]":
            return json.dumps({"answer": "INSUFFICIENT_EVIDENCE", "abstained": True, "cited_sources": []})
        return json.dumps({"answer": found.group(1), "abstained": False, "cited_sources": []})
    return AnswerReader(FakeLLMClient(handler=handler))


def two_fact_trace():
    # Memory 1 has facts attr01 and attr02. A change to attr02 makes the summary
    # stale, but questions about attr01 are still answered correctly from it.
    return workload_from_spec(
        changes=[(5, 1, 2, CT.CORRECTION, "birch", None), (40, 1, 1, CT.CORRECTION, "cedar", None)],
        queries=[(10, 1, 1), (12, 1, 2), (20, 1, 1), (45, 1, 1), (50, 1, 2)],
        facts=2, memories=1, hours=1)


def test_structural_check_separates_needed_flags_from_false_alarms():
    accesses = collect_stale_accesses(two_fact_trace())
    assert len(accesses) == 5  # every query after the first change hits a stale summary
    needed = [a.structurally_needed for a in accesses]
    # attr01 asked before its own change (minutes 10, 20): false alarms; attr02 and later attr01: needed.
    assert needed == [False, True, False, True, True]
    assert all(a.stale_text != a.fresh_text for a in accesses)


def test_semantic_check_agrees_with_a_reader_that_reads_the_evidence():
    accesses = collect_stale_accesses(two_fact_trace())
    judgments = judge_sample(accesses, reading_reader(), sample_size=10, seed=0)
    assert len(judgments) == 5
    assert all(j.fresh_correct for j in judgments)
    assert [j.semantically_needed for j in judgments] == [a.structurally_needed for a in accesses]
    report = summarize(accesses, judgments)
    assert report["structural_flag_precision"] == pytest.approx(3 / 5)
    assert report["semantic_flag_precision"] == pytest.approx(3 / 5)
    assert report["fresh_answer_accuracy"] == 1.0
    assert report["needs_review"] == 0  # structural and semantic judgments agree everywhere
    row = judgments[0].to_row()
    assert {"stale_answer", "fresh_answer", "needs_review", "structurally_needed"} <= set(row)


def test_retraction_counts_as_correct_only_when_the_reader_abstains():
    workload = workload_from_spec(
        changes=[(5, 1, 1, CT.RETRACTION, None, None)], queries=[(10, 1, 1)], facts=1, memories=1, hours=1)
    accesses = collect_stale_accesses(workload)
    assert accesses[0].expected_value is None and "[retracted]" in accesses[0].fresh_text
    (judgment,) = judge_sample(accesses, reading_reader(), sample_size=1, seed=0)
    assert judgment.fresh_correct and judgment.fresh_abstained
    assert not judgment.stale_correct  # the stale summary still states the old value


def test_sampling_is_seeded():
    accesses = collect_stale_accesses(two_fact_trace())
    first = [j.access.query_id for j in judge_sample(accesses, reading_reader(), 3, seed=4)]
    again = [j.access.query_id for j in judge_sample(accesses, reading_reader(), 3, seed=4)]
    assert first == again and len(first) == 3


def test_wilson_interval():
    assert wilson_interval(0, 0) is None
    low, high = wilson_interval(50, 100)
    assert low == pytest.approx(0.4038, abs=1e-3) and high == pytest.approx(0.5962, abs=1e-3)
    assert wilson_interval(10, 10)[1] == pytest.approx(1.0)


def test_reader_errors_are_marked_for_review():
    """A reader that ignores the evidence gets fresh answers wrong, and those need review."""
    stubborn = AnswerReader(FakeLLMClient(handler=lambda request: json.dumps(
        {"answer": "amber", "abstained": False, "cited_sources": []})))
    accesses = collect_stale_accesses(two_fact_trace())
    judgments = judge_sample(accesses, stubborn, sample_size=5, seed=0)
    wrong_fresh = [j for j in judgments if not j.fresh_correct]
    assert wrong_fresh and all(j.needs_review for j in wrong_fresh)

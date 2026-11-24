"""Evaluator-only semantic dirty-bit check (Step 6.5).

The substrate marks a summary stale whenever any of its source facts changes,
even if the change does not affect what a given question asks. This module
measures how often a stale flag is a false alarm, on synthetic-v2 traces:

1. Replay the trace under Verify-Only with deterministic operations, so every
   stale access is a VERIFY row and stored summaries keep their old text.
2. **Structural check (all stale accesses, exact):** the flag is *needed* when
   the queried fact's version in the summary's provenance is no longer current
   at query time.
3. **Semantic check (a seeded sample, LLM reader):** the frozen reader answers
   the question twice, from the stale stored summary and from a freshly rebuilt
   summary (deterministic template over the current facts). Each answer is
   scored against event-time ledger truth. The flag is *semantically needed*
   when the stale-summary answer is wrong.

Flag precision is the fraction of stale flags that were needed, reported with
a Wilson 95% interval. Cases where the stale and fresh answers disagree, or
where the fresh answer is itself wrong, are returned for human review. Nothing
here is visible to policies, and no stored state is modified.
"""
from __future__ import annotations

import random
from dataclasses import asdict, dataclass
from math import sqrt

from verify_repair.answer_scoring import exact_match, resolve_event_time_truth
from verify_repair.experiments import make_policy
from verify_repair.maintenance.summary_builder import DeterministicTemplateSummaryBuilder
from verify_repair.simulator.engine import Simulator
from verify_repair.simulator.events import QueryEvent
from verify_repair.simulator.evolving import EvolvingWorkload, evolving_run_config, load_evolving


@dataclass(frozen=True)
class StaleAccess:
    query_id: str
    event_id: str
    event_timestamp: str
    summary_id: str
    stale_episode_id: str
    question: str
    stale_text: str
    fresh_text: str
    stale_refs: tuple[str, ...]
    fresh_refs: tuple[str, ...]
    expected_value: str | None
    outdated_values: tuple[str, ...]
    structurally_needed: bool


@dataclass(frozen=True)
class DirtyBitJudgment:
    access: StaleAccess
    stale_answer: str
    stale_abstained: bool
    stale_correct: bool
    fresh_answer: str
    fresh_abstained: bool
    fresh_correct: bool

    @property
    def semantically_needed(self) -> bool:
        return not self.stale_correct

    @property
    def needs_review(self) -> bool:
        """Answers disagree, or the fresh answer is wrong (a reader or rebuild failure)."""
        return (not self.fresh_correct) or (
            self.stale_abstained != self.fresh_abstained
            or not exact_match(self.stale_answer, self.fresh_answer))

    def to_row(self) -> dict:
        row = asdict(self.access)
        for key in ("stale_refs", "fresh_refs", "outdated_values"):
            row[key] = ";".join(row[key])
        row.update(stale_answer=self.stale_answer, stale_abstained=self.stale_abstained,
                   stale_correct=self.stale_correct, fresh_answer=self.fresh_answer,
                   fresh_abstained=self.fresh_abstained, fresh_correct=self.fresh_correct,
                   semantically_needed=self.semantically_needed, needs_review=self.needs_review)
        return row


def wilson_interval(successes: int, total: int, z: float = 1.959964) -> tuple[float, float] | None:
    """Wilson score interval for a binomial proportion; None for an empty sample."""
    if total == 0:
        return None
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    half = z * sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return max(0.0, centre - half), min(1.0, centre + half)


def collect_stale_accesses(workload: EvolvingWorkload) -> list[StaleAccess]:
    """Replay under Verify-Only and describe every stale access (deterministic)."""
    from verify_repair.llm.reader import format_ref_labels, render_reader_question

    db, ledger, summaries, retriever, events = load_evolving(workload)
    try:
        result = Simulator(ledger, summaries, retriever, make_policy("verify-only"),
                           evolving_run_config(workload)).run(events)
        by_event = {event.event_id: event for event in events if isinstance(event, QueryEvent)}
        builder = DeterministicTemplateSummaryBuilder()
        accesses = []
        for row in result.logs:
            if row["action"] != "VERIFY":
                continue
            event = by_event[row["event_id"]]
            summary = summaries.get_summary(row["summary_id"])
            refs = summaries.get_source_facts(row["summary_id"])
            current_refs = {slot: ledger.resolve_current_ref(ref, event.timestamp) for slot, ref in refs.items()}
            values = {slot: (ledger.get_version(ref).value if ref is not None else None)
                      for slot, ref in current_refs.items()}
            truth = resolve_event_time_truth(ledger, event)
            queried = [ref for ref in refs.values() if ref.fact_id in event.expected_fact_ids]
            accesses.append(StaleAccess(
                query_id=event.query_id, event_id=event.event_id, event_timestamp=row["event_timestamp"],
                summary_id=row["summary_id"], stale_episode_id=row["stale_episode_id"],
                question=render_reader_question(event, ledger),
                stale_text=summary.text,
                fresh_text=builder.build(summary.text_template, values),
                stale_refs=format_ref_labels(refs),
                fresh_refs=format_ref_labels(current_refs),
                expected_value=truth.expected_values[0] if truth.expected_values else None,
                outdated_values=truth.outdated_values,
                structurally_needed=any(
                    ledger.resolve_current_ref(ref, event.timestamp) != ref for ref in queried),
            ))
        return accesses
    finally:
        db.close()


def _correct(answer: str, abstained: bool, expected: str | None) -> bool:
    if expected is None:  # retracted: the right behavior is to abstain
        return abstained
    return (not abstained) and bool(exact_match(answer, expected))


def judge_sample(accesses: list[StaleAccess], reader, sample_size: int, seed: int) -> list[DirtyBitJudgment]:
    """Ask the frozen reader each sampled question from stale and fresh evidence."""
    from verify_repair.llm.reader import EvidencePacket

    rng = random.Random(seed)
    sample = rng.sample(accesses, min(sample_size, len(accesses)))
    judgments = []
    for access in sorted(sample, key=lambda a: (a.event_timestamp, a.query_id)):
        answers = {}
        for mode, text, refs in (("stale_summary", access.stale_text, access.stale_refs),
                                 ("fresh_summary", access.fresh_text, access.fresh_refs)):
            answers[mode] = reader.answer(EvidencePacket(
                query_id=access.query_id, question_text=access.question, summary_ids=(access.summary_id,),
                evidence_mode=mode, evidence_text=text, source_ref_labels=refs))
        stale, fresh = answers["stale_summary"], answers["fresh_summary"]
        judgments.append(DirtyBitJudgment(
            access=access,
            stale_answer=stale.answer_text, stale_abstained=stale.abstained,
            stale_correct=_correct(stale.answer_text, stale.abstained, access.expected_value),
            fresh_answer=fresh.answer_text, fresh_abstained=fresh.abstained,
            fresh_correct=_correct(fresh.answer_text, fresh.abstained, access.expected_value),
        ))
    return judgments


def summarize(accesses: list[StaleAccess], judgments: list[DirtyBitJudgment]) -> dict:
    """Flag precision (structural on all accesses; semantic on the sample) with Wilson CIs."""
    structural = sum(a.structurally_needed for a in accesses)
    semantic = sum(j.semantically_needed for j in judgments)
    fresh_ok = sum(j.fresh_correct for j in judgments)
    sampled_structural = [j for j in judgments if j.access.structurally_needed]
    return {
        "stale_accesses": len(accesses),
        "structural_flag_precision": structural / len(accesses) if accesses else None,
        "structural_flag_precision_ci95": wilson_interval(structural, len(accesses)),
        "sample_size": len(judgments),
        "semantic_flag_precision": semantic / len(judgments) if judgments else None,
        "semantic_flag_precision_ci95": wilson_interval(semantic, len(judgments)),
        "fresh_answer_accuracy": fresh_ok / len(judgments) if judgments else None,
        "fresh_answer_accuracy_ci95": wilson_interval(fresh_ok, len(judgments)),
        "sample_structurally_needed": len(sampled_structural),
        "stale_wrong_given_structurally_needed": (
            sum(j.semantically_needed for j in sampled_structural) / len(sampled_structural)
            if sampled_structural else None),
        "stale_wrong_given_not_structurally_needed": (
            sum(j.semantically_needed for j in judgments if not j.access.structurally_needed)
            / (len(judgments) - len(sampled_structural)) if len(judgments) > len(sampled_structural) else None),
        "needs_review": sum(j.needs_review for j in judgments),
    }

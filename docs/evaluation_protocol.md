# Evaluation Protocol

The main question behind this project is pretty simple: once an agent realizes that one of its stored summaries is stale, should it keep verifying that information when it comes up, repair the summary permanently, or wait and spend its maintenance budget somewhere more useful?

Actually comparing those choices is slightly less simple. If one policy gets an easier set of questions or fewer corrections, its results are not really comparable to the others. This document keeps track of what we need to hold constant and what we want to measure.

## Keeping the comparison fair

Every policy should receive the exact same:

- Facts and initial summaries
- Corrections and queries
- Event ordering
- Random seed
- Verification and repair costs
- Repair budget
- Retrieval results
- Initial memory state

The only thing that should change is the policy deciding what to do. In other words, a policy should not look better just because it happened to receive a friendlier experiment.

## What we expect to happen

We currently have four main hypotheses:

1. P1 should avoid the worst behavior of both extremes. Verify-Only should be reasonable for memories that are barely used, while Eager Refresh should be reasonable for memories that are used constantly. P1 should handle both groups without making the most expensive mistake in either direction.
2. P2 should improve on P1 when past activity actually helps predict future questions.
3. P2 should fall back toward P1 when its prediction is uncertain or wrong. A bad prediction should not completely destroy its performance.
4. When repair slots are limited, a prediction-aware policy should use those slots more effectively than Random-First or Oldest-First when access frequency is uneven.

These are hypotheses, not results we are assuming in advance. If the experiments disagree, that is still useful information about our policy or workload assumptions.

## What we will measure

For every run, we should record:

- **Total nominal cost:** verification cost, repair cost, and any configured stale-answer penalty.
- **Verifications and repairs:** how often the policy chooses each action.
- **Stale-answer rate:** the percentage of answers that actually use outdated information.
- **Stale-summary exposures:** how often a query encounters a summary that is still marked stale, even if the policy verifies it before answering.
- **Time-to-repair:** how long a summary remains stale before it is repaired.
- **Peak stale backlog:** the largest number of summaries waiting for repair at one time.
- **Query latency:** both p50 and p95 wall-clock latency.
- **Group cost:** results separated into cold, warm, and hot memories.
- **Oracle regret:** how much more the policy spends than a policy with perfect knowledge of future questions.

For a stale episode, the oracle knows how many future queries will use that summary. It chooses whichever is cheaper: verifying those queries individually or repairing the summary once.

```text
oracle regret = policy cost - oracle cost
```

The oracle is not a realistic policy. It gives us a lower bound so we can measure how much each practical policy loses because it cannot see the future.

## How results should be reported

Policies should be evaluated on paired runs using identical seeds and traces. We should save the result for every seed rather than keeping only the final average.

Each saved run should include its full settings, including the policy parameters, workload size, cost values, seed, and dataset version. Summary tables should report a mean or median along with some measure of variability.

Nominal cost units and real latency also need to stay separate. A repair costing five abstract points does not mean that it took five milliseconds (as convenient as that would be).

Unexpected or negative results should not be removed. If P2 loses to P1 under distribution shift, for example, that is an important robustness result rather than a failed experiment.

## External validation

Synthetic workloads are useful because we can control exactly how many corrections and future questions occur. They should not be our only evidence, though.

Once the main experiment pipeline works, we should test a small explicit-update subset from a benchmark such as StateMemBench, MemOps, or LongMemEval. This will help us check whether the patterns from our controlled workloads still appear in more realistic memory-update scenarios.

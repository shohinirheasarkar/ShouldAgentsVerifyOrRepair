# Dirty-bit sample: cases for review

From the first Step 6 Zaratan job (24252085, code `7533be2`): 300 sampled stale
accesses of the 1K pilot trace (seed 2001). A case is listed when the fresh
answer is wrong or the semantic and structural judgments disagree. The
classification below is a first pass by the implementer from the saved text and
answers; **a teammate should confirm it** before the report calls it human-reviewed.

| Query | Question | Truth | Stale summary | Stale answer | Fresh answer | First-pass classification |
| --- | --- | --- | --- | --- | --- | --- |
| `query_000004718` | What is the current attr01 of Entity000589? | garnet | Entity000589 record: attr01 garnet. | garnet | garnet | Version changed, value did not: stale summary still correct (structural false alarm) |
| `query_000005700` | What is the current attr02 of Entity000051? | fjord | Entity000051 record: attr01 amber; attr02 fjord. | fjord | fjord | Version changed, value did not: stale summary still correct (structural false alarm) |
| `query_000005845` | What is the current attr01 of Entity000287? | delta | Entity000287 record: attr01 harbor. | harbor | INSUFFICIENT_EVIDENCE | Reader error: abstains on the current value although the fresh summary states it |
| `query_000006829` | What is the current attr01 of Entity000697? | delta | Entity000697 record: attr01 garnet. | garnet | INSUFFICIENT_EVIDENCE | Reader error: abstains on the current value although the fresh summary states it |
| `query_000006879` | What is the current attr01 of Entity000297? | fjord | Entity000297 record: attr01 fjord. | fjord | fjord | Version changed, value did not: stale summary still correct (structural false alarm) |
| `query_000008147` | What is the current attr01 of Entity000690? | birch | Entity000690 record: attr01 birch. | birch | birch | Version changed, value did not: stale summary still correct (structural false alarm) |
| `query_000008298` | What is the current attr01 of Entity000297? | fjord | Entity000297 record: attr01 fjord. | fjord | fjord | Version changed, value did not: stale summary still correct (structural false alarm) |
| `query_000008526` | What is the current attr01 of Entity000173? | fjord | Entity000173 record: attr01 fjord. | fjord | fjord | Version changed, value did not: stale summary still correct (structural false alarm) |
| `query_000009655` | What is the current attr02 of Entity000050? | delta | Entity000050 record: attr01 amber; attr02 garnet; attr03 amber. | garnet | INSUFFICIENT_EVIDENCE | Reader error: abstains on the current value although the fresh summary states it |
| `query_000009880` | What is the current attr01 of Entity000719? | delta | Entity000719 record: attr01 fjord. | fjord | INSUFFICIENT_EVIDENCE | Reader error: abstains on the current value although the fresh summary states it |
| `query_000011110` | What is the current attr01 of Entity000475? | fjord | Entity000475 record: attr01 fjord. | fjord | fjord | Version changed, value did not: stale summary still correct (structural false alarm) |
| `query_000011430` | What is the current attr01 of Entity000099? | harbor | Entity000099 record: attr01 harbor. | harbor | harbor | Version changed, value did not: stale summary still correct (structural false alarm) |
| `query_000012485` | What is the current attr01 of Entity000324? | amber | Entity000324 record: attr01 amber. | amber | amber | Version changed, value did not: stale summary still correct (structural false alarm) |
| `query_000012547` | What is the current attr02 of Entity000051? | fjord | Entity000051 record: attr01 amber; attr02 fjord. | fjord | fjord | Version changed, value did not: stale summary still correct (structural false alarm) |

Summary:

- 10 cases: the queried fact changed (or was retracted) and was later set back to its original value,
  so its current version is new but its value matches the stale summary (checked against the regenerated trace:
  e.g. `query_000004718`: garnet → retracted → garnet; `query_000011430`: harbor → ember → harbor).
  Version-level staleness overstates semantic staleness here; the stale answer was correct.
- 4 cases: reader abstained on a stated value. All involve the value "delta" (4 of 33 "delta" answers; 0 errors in 267 answers with other values), possibly
  because "delta" reads like a change marker (an untested hypothesis). Likely an artifact of the synthetic value
  vocabulary rather than of staleness.

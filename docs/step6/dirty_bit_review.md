# Dirty-bit sample: cases for review

From the first Step 6 Zaratan job (24252085, code `7533be2`): 300 sampled stale
accesses of the 1K pilot trace (seed 2001). A case is listed when the fresh
answer is wrong or the semantic and structural judgments disagree. The
classification below is a first pass by the implementer from the saved text and
answers. Independent teammate reviewer Arik Gershman subsequently confirmed
the event-time audit and signed below.
In that first-pass table, “version changed, value did not” describes only the
stored and query-time endpoints; the queried value did change in between, as
the independent event-time audit below shows.

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
- 4 cases: reader abstained on a stated value. All involve the value "delta"
  (4 of 33 "delta" answers; 0 errors in 267 answers with other values).
  The cause has not been established.

## Historical follow-up recommendation (November 27, 2026; now resolved)

An automated cross-check of both archived samples (`24252085` and `24252220`)
reproduces the 14 cases above using the current `DirtyBitJudgment.needs_review`
rule: a fresh-answer error or a structural/semantic disagreement. The archived
CSV flags and summary JSON instead report 164 review cases. Those historical
fields do not match the current rule; preserve the raw artifacts and report
this distinction rather than treating all 164 flags as unexplained cases.

This recommendation was to confirm the 10 changed-back cases against event-time
history and the four false abstentions against fresh evidence. Arik's November
28 sign-off below closes the case review. Keep the four
false abstentions in the reported reader error counts; "delta" as a change
marker remains an untested explanation, not an established cause.

The v6 GPU pilot (job 24312071) reran the same seeded sample. Its 14 flagged
query IDs, current truths, stale answers, and fresh answers match this table;
the v6 CSV is archived in [`zaratan_24312071/dirty_bit_sample.csv`](zaratan_24312071/dirty_bit_sample.csv).
The automated case-level comparison and Arik's independent teammate review
are complete.

A small paired check of the four failing evidence packets and
otherwise identical packets with another value can test whether the problem
is value-specific. Avoid a special-case rule for "delta" or changing the
saved workload to improve its scores. A broader reader change is warranted
only if the error persists or materially affects the final comparisons.

## Independent event-time audit of v6 job 24312071 (November 28, 2026)

Reviewer: **Arik (and Codex)**. Reviewed job ID:
**24312071**. Review date: **2026-11-28 (EST)**. This records the reproducible
evidence check and Arik's independent teammate review.

The supplied `pilot_workload.json` has SHA-256
`adc9358cecbe8e3a1b0e89441915e8a1ed0592fbc93744c46ed7e2f247672784`,
matching the v6 manifest. The supplied manifest and CSV match the archived
[`pilot_manifest.json`](zaratan_24312071/pilot_manifest.json) and
[`dirty_bit_sample.csv`](zaratan_24312071/dirty_bit_sample.csv) byte for byte.
Filtering the 300 CSV rows for `needs_review=True` yields **14** rows. For each,
the query ID, timestamp, and target fact agree with the workload's query event.
I reconstructed the target fact's versions from its initial value and change
events up to that query, excluded expired temporary exceptions, and compared
the effective version/value to `fresh_refs` and `expected_value`. All 14
expected values agree. A later trace value was never substituted for the
query-time value. In the table, all times are UTC on **2026-01-01**; `vN`
denotes the version of the fact named in that row, and `change_…` IDs refer to
events in the supplied workload. The question and full CSV event timestamp are
shown for each query, including repeated questions about the same entity.

| Query ID | Effective value and supporting history | Proposed classification | Confirmed / corrected / unresolved | Explanation |
| --- | --- | --- | --- | --- |
| `query_000004718`<br>`2026-01-01T09:03:19.557252+00:00`<br>What is the current attr01 of Entity000589? | `Entity000589.attr01`, `memory_000589_attr01`: v1 garnet → v2 retracted (`change_000000727`) → **v3 garnet** (`change_000000973`, 08:15:38). | Outdated source version, but stale answer correct because the value returned. | **Confirmed** | Stored v1 differs from current v3; the retraction intervened. `stale_text` says “attr01 garnet” and `stale_answer=garnet`. |
| `query_000005700`<br>`2026-01-01T10:56:11.066804+00:00`<br>What is the current attr02 of Entity000051? | `Entity000051.attr02`, `memory_000051_attr02`: v1 fjord → v2 birch (`change_000000056`) → v3 retracted (`change_000000470`) → v4 birch (`change_000000897`) → **v5 fjord** (`change_000000903`, 07:38:59). | Outdated source version, but stale answer correct because the value returned. | **Confirmed** | Stored v1 differs from current v5; `stale_text` says “attr02 fjord” and `stale_answer=fjord`. The changed `attr01` in fresh text does not alter this target's truth. |
| `query_000005845`<br>`2026-01-01T11:13:47.563047+00:00`<br>What is the current attr01 of Entity000287? | `Entity000287.attr01`, `memory_000287_attr01`: v1 harbor → **v2 delta** (`change_000000804`, 06:48:27). | False abstention despite explicit supporting evidence. | **Confirmed** | Target v2 is present, not retracted. `fresh_text` says “attr01 delta”; `fresh_answer=INSUFFICIENT_EVIDENCE`, `fresh_abstained=True`. |
| `query_000006829`<br>`2026-01-01T13:03:32.109533+00:00`<br>What is the current attr01 of Entity000697? | `Entity000697.attr01`, `memory_000697_attr01`: v1 garnet → v2 amber (`change_000000446`) → v3 retracted (`change_000001057`) → **v4 delta** (`change_000001224`, 10:18:27). | False abstention despite explicit supporting evidence. | **Confirmed** | The correction restored the target after its retraction. `fresh_text` says “attr01 delta”; `fresh_answer=INSUFFICIENT_EVIDENCE`, `fresh_abstained=True`. |
| `query_000006879`<br>`2026-01-01T13:09:49.328057+00:00`<br>What is the current attr01 of Entity000297? | `Entity000297.attr01`, `memory_000297_attr01`: v1 fjord → v2 ember (`change_000000934`) → **v3 fjord** (`change_000001428`, 12:01:04). | Outdated source version, but stale answer correct because the value returned. | **Confirmed** | Stored v1 differs from current v3; `stale_text` says “attr01 fjord” and `stale_answer=fjord`. |
| `query_000008147`<br>`2026-01-01T15:34:57.684838+00:00`<br>What is the current attr01 of Entity000690? | `Entity000690.attr01`, `memory_000690_attr01`: v1 birch → v2 garnet (`change_000000411`) → **v3 birch** (`change_000001751`, 14:30:11). | Outdated source version, but stale answer correct because the value returned. | **Confirmed** | Stored v1 differs from current v3; `stale_text` says “attr01 birch” and `stale_answer=birch`. |
| `query_000008298`<br>`2026-01-01T15:53:26.788546+00:00`<br>What is the current attr01 of Entity000297? | `Entity000297.attr01`, `memory_000297_attr01`: v1 fjord → v2 ember (`change_000000934`) → **v3 fjord** (`change_000001428`, 12:01:04). | Outdated source version, but stale answer correct because the value returned. | **Confirmed** | This is a separate later query from `query_000006879`. Stored v1 differs from current v3; `stale_text` says “attr01 fjord” and `stale_answer=fjord`. |
| `query_000008526`<br>`2026-01-01T16:19:57.570412+00:00`<br>What is the current attr01 of Entity000173? | `Entity000173.attr01`, `memory_000173_attr01`: v1 fjord → v2 temporary delta (`change_000000675`, expired 05:47:42) → v3 amber (`change_000000891`) → **v4 fjord** (`change_000001271`, 10:36:46) → v5 temporary cedar (`change_000001683`, expired 14:52:20). **Effective v4 fjord** at query time. | Outdated source version, but stale answer correct because the value returned. | **Confirmed** | Stored v1 differs from effective v4. The later v5 exception had expired; `stale_text` says “attr01 fjord” and `stale_answer=fjord`. |
| `query_000009655`<br>`2026-01-01T18:31:34.923883+00:00`<br>What is the current attr02 of Entity000050? | `Entity000050.attr02`, `memory_000050_attr02`: v1 garnet → **v2 delta** (`change_000001709`, 14:09:08). | False abstention despite explicit supporting evidence. | **Confirmed** | `fresh_text` says “attr01 [retracted]; attr02 delta”; the retraction is of **attr01**, not the target attr02. `fresh_answer=INSUFFICIENT_EVIDENCE`, `fresh_abstained=True`. |
| `query_000009880`<br>`2026-01-01T18:56:44.454156+00:00`<br>What is the current attr01 of Entity000719? | `Entity000719.attr01`, `memory_000719_attr01`: v1 fjord → v2 harbor (`change_000000371`) → v3 garnet (`change_000001402`) → v4 harbor (`change_000001933`) → **v5 temporary delta** (`change_000002208`, 18:12:37–19:04:32). | False abstention despite explicit supporting evidence. | **Confirmed** | The target's v5 exception was active at 18:56:44. `fresh_text` says “attr01 delta”; `fresh_answer=INSUFFICIENT_EVIDENCE`, `fresh_abstained=True`. |
| `query_000011110`<br>`2026-01-01T21:17:22.705714+00:00`<br>What is the current attr01 of Entity000475? | `Entity000475.attr01`, `memory_000475_attr01`: v1 fjord → v2 garnet (`change_000000859`) → v3 amber (`change_000001204`) → v4 birch (`change_000001502`) → v5 harbor (`change_000001973`) → **v6 fjord** (`change_000002394`, 19:38:34). | Outdated source version, but stale answer correct because the value returned. | **Confirmed** | Stored v1 differs from current v6; `stale_text` says “attr01 fjord” and `stale_answer=fjord`. |
| `query_000011430`<br>`2026-01-01T21:51:15.743236+00:00`<br>What is the current attr01 of Entity000099? | `Entity000099.attr01`, `memory_000099_attr01`: v1 harbor → v2 ember (`change_000001244`) → **v3 harbor** (`change_000001998`, 16:29:07). | Outdated source version, but stale answer correct because the value returned. | **Confirmed** | Stored v1 differs from current v3; `stale_text` says “attr01 harbor” and `stale_answer=harbor`. |
| `query_000012485`<br>`2026-01-01T23:53:07.677482+00:00`<br>What is the current attr01 of Entity000324? | `Entity000324.attr01`, `memory_000324_attr01`: v1 amber → v2 birch (`change_000000074`) → **v3 amber** (`change_000002752`, 22:34:57). | Outdated source version, but stale answer correct because the value returned. | **Confirmed** | Stored v1 differs from current v3; `stale_text` says “attr01 amber” and `stale_answer=amber`. |
| `query_000012547`<br>`2026-01-01T23:59:50.368937+00:00`<br>What is the current attr02 of Entity000051? | `Entity000051.attr02`, `memory_000051_attr02`: v1 fjord → v2 birch (`change_000000056`) → v3 retracted (`change_000000470`) → v4 birch (`change_000000897`) → **v5 fjord** (`change_000000903`, 07:38:59). | Outdated source version, but stale answer correct because the value returned. | **Confirmed** | This is a separate later query from `query_000005700`. Stored v1 differs from current v5; `stale_text` says “attr02 fjord” and `stale_answer=fjord`. |

**Decision:** 10 confirmed returned-value cases; 4 confirmed false abstentions;
0 corrected classifications; **0 unresolved temporal interpretations**. The four
false abstentions remain errors in the reported accuracy calculation:
**296/300 = 0.9867** fresh answers correct. The
shared value `delta` is an observation, not an established cause of the reader
failures. The original CSV, workload, and saved model answers were not edited.

### Human sign-off

An owner or teammate can sign off here after checking the table against the
linked v6 artifacts and the supplied workload:

- Reviewer name: Arik Gershman
- Review date: 11/28/2026
- Reviewed job ID: **24312071**
- Decision (accept / request correction): accept

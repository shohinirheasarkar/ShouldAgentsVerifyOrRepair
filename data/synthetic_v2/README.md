# Representative synthetic-v2 traces

Small saved traces for Step 2+ development: 100 memories, 1–6 facts per
summary, a mean of 3 source changes per memory over 24 hours, and the default
change mix (correction/update/retraction/temporary = 0.6/0.2/0.1/0.1).

Regenerate any file byte-identically (the output path must not exist):

```bash
python -m verify_repair.cli generate-evolving --memories 100 --seed 1 \
  --demand-mode shifting --phases 2 --facts-per-summary 1 6 \
  --changes-per-memory 3 --output data/synthetic_v2/shifting-100-seed1.json
```

`fanout-100-seed1.json` adds bounded dependency fan-out: about half the
memories also read one earlier memory's fact (`--fan-out 3
--shared-fact-rate 0.5`), so one change can make up to three summaries stale.

Replay one with any policy:

```bash
python -m verify_repair.cli run-synthetic --policy p1 \
  --workload data/synthetic_v2/shifting-100-seed1.json
```

`memories`, `episodes`, and `phase_starts` in each file are evaluator-only
metadata (groups per phase, exact accesses per source-change episode). The
simulator and policies never receive them. Experiment scoring for v2 traces
(`run-experiment`) arrives with the multi-episode scorer in Step 2.

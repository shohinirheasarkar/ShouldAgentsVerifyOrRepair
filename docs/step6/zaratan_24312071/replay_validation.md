# V6 measured-cost replay development checks

The archived `cost_profile_wall_ms.json` came from Zaratan Step 6 job
24312071, prompt bundle `llm-prompts-v6`. Its SHA-256 is
`06f9553411394762873c8bd39c07c6b35adcec1c22f67d340a5ac5cd9f76f738`.
The profile and the historical v4 file share a `profile_id`; use this hash
and the `source.model.prompt_version` field to distinguish them.

Run from the repository root with a suitable Python environment. The checks
below used Python 3.13.3 with `PYTHONPATH=src` to load the checkout code.
Output files are generated outside Git and must have new names on each run.

```bash
RUN_DIR="$(mktemp -d /tmp/step6-v6-replay.XXXXXX)"

PYTHONPATH=src python -m verify_repair.cli run-experiment \
  --workload data/synthetic_v2/stable-100-seed1.json \
    data/synthetic_v2/bursty-100-seed1.json \
    data/synthetic_v2/shifting-100-seed1.json \
    data/synthetic_v2/fanout-100-seed1.json \
  --cost-profile docs/step6/zaratan_24312071/cost_profile_wall_ms.json \
  --stale-penalty 2000 --output "$RUN_DIR/replay-100.csv"

PYTHONPATH=src python -m verify_repair.cli run-experiment \
  --workload data/synthetic_v2/stable-100-seed1.json \
  --policies verify-only repair-on-first-access oldest-first p1 p2 \
  --cost-profile docs/step6/zaratan_24312071/cost_profile_wall_ms.json \
  --stale-penalty 2000 --budget 10000 --queue-order predicted-savings \
  --budget-oracle exact-milp \
  --output "$RUN_DIR/replay-100-budgeted.csv"

PYTHONPATH=src python -m verify_repair.cli generate-evolving \
  --memories 10000 --seed 2001 --facts-per-summary 1 6 \
  --changes-per-memory 3 \
  --output "$RUN_DIR/stable-10k-seed2001.json"

PYTHONPATH=src python -m verify_repair.cli run-experiment \
  --workload "$RUN_DIR/stable-10k-seed2001.json" \
  --policies verify-only repair-on-first-access p1 p2 \
  --cost-profile docs/step6/zaratan_24312071/cost_profile_wall_ms.json \
  --stale-penalty 2000 --output "$RUN_DIR/replay-10k.csv"
```

The 100-memory run wrote 128 rows (eight policies, four workloads); the
budgeted run wrote 20 rows (five policies, one workload); the 10K run wrote
16 rows (four policies, one workload). Minimum regret was zero or positive in
each output. The 10K saved workload had 10,000 memories, 29,981 source
changes, 126,599 queries, 32,793 episodes, and SHA-256
`f0f0d485a58f01c14ed4c7f1a82985b7ea036eb08ffc5409e92c816feef23e9c`.
For the 10K `all` rows, cost/oracle ratios were 2.13 (Verify-Only), 1.63
(Repair-on-First-Access), 1.59 (P1), and 1.46 (P2). These are one-seed
development checks. The paired held-out comparison belongs to Step 9 and
100K scale profiling belongs to Step 11.

#!/usr/bin/env bash
# Multi-seed 10K synthetic-v2 evaluation for the final report.
#
# Generates four regimes (stable, bursty, shifting, fan-out) with the same
# settings as data/synthetic_v2/*-100-seed1.json, scaled to 10,000 memories,
# for evaluation seeds 101-105 (never used during development). Then replays
# every policy on each trace:
#   unbudgeted: c_v = 1, c_r in {2, 5, 10}, lambda = 10
#   budgeted:   c_v = 1, c_r = 5, B in {500, 2000} per one-hour window,
#               predicted-savings queue, exact budget-aware oracle
# and averages across seeds with 95% CIs. Costs are nominal units.
#
# Usage (from the repository root; OUT must be outside Git, ~1 GB of traces):
#   scripts/run_v2_eval.sh OUT [JOBS]
# Runtime: about 800 replays of ~1-2 min each; ~3 h with JOBS=7 on a laptop.
set -euo pipefail

OUT=${1:?usage: scripts/run_v2_eval.sh OUT [JOBS]}
JOBS=${2:-7}
PYTHON=${PYTHON:-python}
SEEDS=(101 102 103 104 105)
REGIMES=(stable bursty shifting fanout)
mkdir -p "$OUT/workloads" "$OUT/unbudgeted" "$OUT/budgeted" "$OUT/logs"
git rev-parse HEAD > "$OUT/git_sha.txt"
"$PYTHON" --version > "$OUT/python_version.txt" 2>&1

generate() {
  local regime=$1 seed=$2 path="$OUT/workloads/$1-10k-seed$2.json"
  local args=(--memories 10000 --seed "$seed" --facts-per-summary 1 6 --changes-per-memory 3)
  case $regime in
    stable)   args+=(--demand-mode stable) ;;
    bursty)   args+=(--demand-mode bursty) ;;
    shifting) args+=(--demand-mode shifting --phases 2) ;;
    fanout)   args+=(--demand-mode stable --fan-out 3 --shared-fact-rate 0.5) ;;
  esac
  [ -e "$path" ] || "$PYTHON" -m verify_repair.cli generate-evolving "${args[@]}" --output "$path" \
    | sed 's/ output=.*//' > "$OUT/logs/generate-$regime-$seed.txt"
}

for regime in "${REGIMES[@]}"; do
  for seed in "${SEEDS[@]}"; do generate "$regime" "$seed"; done
done

# One job per (trace, setting); skip outputs that already exist so a run can resume.
jobs_file="$OUT/logs/jobs.txt"
: > "$jobs_file"
for regime in "${REGIMES[@]}"; do
  for seed in "${SEEDS[@]}"; do
    w="$OUT/workloads/$regime-10k-seed$seed.json"
    for cr in 2 5 10; do
      o="$OUT/unbudgeted/$regime-seed$seed-cr$cr.csv"
      [ -e "$o" ] || echo "--workload $w --repair-costs $cr --output $o" >> "$jobs_file"
    done
  done
done
for regime in "${REGIMES[@]}"; do
  for seed in "${SEEDS[@]}"; do
    w="$OUT/workloads/$regime-10k-seed$seed.json"
    for budget in 500 2000; do
      o="$OUT/budgeted/$regime-seed$seed-B$budget.csv"
      [ -e "$o" ] || echo "--workload $w --repair-costs 5 --budget $budget --queue-order predicted-savings --budget-oracle exact-milp --output $o" >> "$jobs_file"
    done
  done
done

export PYTHON OUT
xargs -P "$JOBS" -L 1 bash -c '
  name=$(basename "${@: -1}" .csv)
  "$PYTHON" -m verify_repair.cli run-experiment --verify-cost 1 --stale-penalty 10 "$@" \
    > "$OUT/logs/$name.log" 2>&1 || { echo "FAILED $name"; exit 255; }
' bash < "$jobs_file"

for regime in "${REGIMES[@]}"; do
  for kind in unbudgeted budgeted; do
    s="$OUT/summary-$kind-$regime.csv"
    rm -f "$s"
    "$PYTHON" -m verify_repair.cli summarize-experiment --input "$OUT/$kind/$regime"-*.csv --output "$s"
  done
done
echo "done: $OUT"

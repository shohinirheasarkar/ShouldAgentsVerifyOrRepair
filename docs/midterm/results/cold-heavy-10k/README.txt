# Workloads: python -m verify_repair.cli generate-synthetic --memories 10000 --seed SEED --group-counts 8000 1000 1000
# Run: run-experiment --verify-cost 1 --repair-costs 2 5 10 (no latency; defaults: refresh interval 100 events, 1 repair slot/tick)
# Code commit: ea2227d7cb733777755859eba5237e7080463fbb; Python 3.12.2 (anaconda), macOS, 8 cores
dataset=synthetic-v1-04fbc4aea682c758 memories=10000 questions=44405 groups=(8000, 1000, 1000) seed=1 sha256=4690791d078174451a49a4a5f6a5e59a11f8c0fe0f04ef137d0ac5b0beb88a9a
dataset=synthetic-v1-1b90545625848ddb memories=10000 questions=45348 groups=(8000, 1000, 1000) seed=2 sha256=11912907381c71d1c9231b108ddab9b2e7b5813dcffe04ae9f364f8b8d4c791d
dataset=synthetic-v1-0b52f31e2ac37356 memories=10000 questions=45021 groups=(8000, 1000, 1000) seed=3 sha256=f5aa7a3e1354c40f23ffd2363fdf24833aec5a43fc73afd188acfe3c92c764f5
dataset=synthetic-v1-833e7bd7ae43b82a memories=10000 questions=45865 groups=(8000, 1000, 1000) seed=4 sha256=285ea5015135b744567fae9f85c8e868643e553b4a0070778c024d5550ada4a6
dataset=synthetic-v1-bd51069ab5dbeb33 memories=10000 questions=44818 groups=(8000, 1000, 1000) seed=5 sha256=b95c97e35122b1b3ec227a6cb47bff2c68dd892f1251f0c2d5972cbeff3a7fb6

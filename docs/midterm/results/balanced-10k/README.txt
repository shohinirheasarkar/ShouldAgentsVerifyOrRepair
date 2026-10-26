# Workloads: python -m verify_repair.cli generate-synthetic --memories 10000 --seed SEED
# Run: run-experiment --verify-cost 1 --repair-costs 2 5 10 --measure-latency (defaults: refresh interval 100 events, 1 repair slot/tick)
# Code commit: ea2227d7cb733777755859eba5237e7080463fbb; Python 3.12.2 (anaconda), macOS, 8 cores; 5 seeds run as concurrent processes
dataset=synthetic-v1-1627940b9de7334f memories=10000 questions=125637 groups=(3334, 3333, 3333) seed=1 sha256=77494a102b7ebdd83095fb1378ad84ac3ed07a315ca59fbaedbb7676d63beb29
dataset=synthetic-v1-f8acff99de16dc6b memories=10000 questions=125943 groups=(3334, 3333, 3333) seed=2 sha256=6574b48f9a2641d27178d90a63f2ecc1705779ac910fd628604519e926f2f430
dataset=synthetic-v1-b86a7bff2823fa4b memories=10000 questions=126456 groups=(3334, 3333, 3333) seed=3 sha256=7a73e780eb264d1ae0308599a911c336a35d15191718f9955a3ca13024750601
dataset=synthetic-v1-2ca5fa8af6c9ba94 memories=10000 questions=127558 groups=(3334, 3333, 3333) seed=4 sha256=eb4156c6701b0dbf9746e9d9e21369338ad5e8d8d4ce7b7e152f763720c17eaf
dataset=synthetic-v1-9eacc76abefbc20e memories=10000 questions=127174 groups=(3334, 3333, 3333) seed=5 sha256=9aa0c5e2b6ae8ce3675a4f9e4389fa5dcc20a0d0af6ea4ad268b4fa98bae0841

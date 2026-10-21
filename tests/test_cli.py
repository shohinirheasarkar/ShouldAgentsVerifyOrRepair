"""CLI checkout discovery and saved synthetic experiment generation/replay."""
import json

import pytest

from verify_repair.cli import main, toy_root
from verify_repair.simulator.synthetic import read_synthetic


def test_toy_root_finds_checkout_from_subdirectory(monkeypatch):
    root = toy_root()
    monkeypatch.chdir(root / "data/toy")
    assert toy_root() == root


def test_generate_and_replay_synthetic_outside_checkout(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "workload.json"
    main(["generate-synthetic", "--memories", "100", "--seed", "42", "--output", str(path),
          "--group-counts", "50", "25", "25", "--cold-queries", "0", "0",
          "--warm-queries", "3", "3", "--hot-queries", "11", "11"])
    assert "memories=100" in capsys.readouterr().out
    assert read_synthetic(path).config.seed == 42
    logs = []
    for index in range(2):
        log_path = tmp_path / f"run{index}.jsonl"
        main(["run-synthetic", "--workload", str(path), "--policy", "repair-on-first-access",
              "--jsonl", str(log_path)])
        output = capsys.readouterr().out
        assert "queries=350" in output and "repairs=50" in output and "stale_source_uses=0" in output
        logs.append(log_path.read_bytes())
    assert logs[0] == logs[1]
    assert all(json.loads(line)["seed"] == 42 for line in logs[0].splitlines())


def test_generation_cli_rejects_invalid_configuration_before_writing(tmp_path, capsys):
    path = tmp_path / "invalid.json"
    with pytest.raises(SystemExit) as error:
        main(["generate-synthetic", "--memories", "0", "--output", str(path)])
    assert error.value.code == 2
    assert "num_memories" in capsys.readouterr().err
    assert not path.exists()


@pytest.mark.parametrize("hardlink", [False, True])
def test_synthetic_logs_cannot_overwrite_saved_inputs(tmp_path, capsys, hardlink):
    path = tmp_path / "inputs.json"
    main(["generate-synthetic", "--memories", "1", "--output", str(path)])
    before = path.read_bytes()
    output = tmp_path / "alias.jsonl" if hardlink else path
    if hardlink:
        output.hardlink_to(path)
    with pytest.raises(SystemExit) as error:
        main(["run-synthetic", "--workload", str(path), "--policy", "verify-only", "--jsonl", str(output)])
    assert error.value.code == 2
    assert "must differ" in capsys.readouterr().err
    assert path.read_bytes() == before


def test_controlled_synthetic_replay_requires_keyword_retrieval(tmp_path, capsys):
    with pytest.raises(SystemExit) as error:
        main(["run-synthetic", "--workload", str(tmp_path / "unused.json"),
              "--policy", "verify-only", "--retrieval", "hybrid"])
    assert error.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_p1_toy_cost_overrides_are_used_and_logged(tmp_path, capsys):
    log = tmp_path / "toy.jsonl"
    main(["run-toy", "--policy", "p1", "--verify-cost", "0.1", "--repair-cost", "0.3", "--jsonl", str(log)])
    assert "policy=p1" in capsys.readouterr().out
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    alice = [row for row in rows if row["query_id"] in ("q1", "q2", "q3")]
    assert [row["action"] for row in alice] == ["VERIFY", "VERIFY", "REPAIR"]
    assert all(row["config_snapshot"]["verify_cost_units"] == 0.1
               and row["config_snapshot"]["repair_cost_units"] == 0.3 for row in rows)
    main(["run-toy", "--policy", "p1", "--jsonl", str(log)])
    default_rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert rows[0]["config_hash"] != default_rows[0]["config_hash"]
    assert rows[0]["run_id"] != default_rows[0]["run_id"]
    assert rows[0]["trace_hash"] == default_rows[0]["trace_hash"]


def test_p1_saved_synthetic_cli_replay(tmp_path, capsys):
    path, log = tmp_path / "inputs.json", tmp_path / "run.jsonl"
    main(["generate-synthetic", "--memories", "3", "--group-counts", "1", "1", "1",
          "--cold-queries", "0", "0", "--warm-queries", "3", "3", "--hot-queries", "11", "11",
          "--output", str(path)])
    before = path.read_bytes()
    args = ["run-synthetic", "--workload", str(path), "--policy", "p1", "--verify-cost", "2",
            "--repair-cost", "5", "--jsonl", str(log)]
    main(args)
    output = capsys.readouterr().out
    assert "verifications=4 repairs=2 nominal_verify_cost=8 nominal_repair_cost=10 stale_source_uses=0" in output
    first = log.read_bytes()
    main(args)
    assert log.read_bytes() == first
    assert path.read_bytes() == before


@pytest.mark.parametrize("option", ["--verify-cost", "--repair-cost"])
@pytest.mark.parametrize("value", ["-1", "nan", "inf", "-inf"])
def test_cli_rejects_invalid_costs_before_loading_database(tmp_path, monkeypatch, capsys, option, value):
    def unexpected_load(*args):
        pytest.fail("invalid costs must fail before opening SQLite")

    monkeypatch.setattr("verify_repair.cli.load_toy", unexpected_load)
    log = tmp_path / "invalid.jsonl"
    with pytest.raises(SystemExit) as error:
        main(["run-toy", "--policy", "p1", f"{option}={value}", "--jsonl", str(log)])
    assert error.value.code == 2
    assert "finite and nonnegative" in capsys.readouterr().err
    assert not log.exists()

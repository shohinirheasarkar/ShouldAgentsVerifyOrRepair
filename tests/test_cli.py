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

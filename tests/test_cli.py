"""CLI checkout discovery works for source and installed-package locations."""
from verify_repair.cli import toy_root


def test_toy_root_finds_checkout_from_subdirectory(monkeypatch):
    root = toy_root()
    monkeypatch.chdir(root / "data/toy")
    assert toy_root() == root

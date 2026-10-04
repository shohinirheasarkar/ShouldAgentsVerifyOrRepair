"""Bootstrap smoke test; run with python -m pytest -q."""
from verify_repair.db import connect


def test_connection_has_foreign_keys_and_schema():
    db = connect()
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert db.execute("SELECT name FROM sqlite_master WHERE name='fact_versions'").fetchone()

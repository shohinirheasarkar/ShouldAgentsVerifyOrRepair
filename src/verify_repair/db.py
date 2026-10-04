"""SQLite schema and transaction helpers. All durable core state lives here."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager


def connect(path: str = ":memory:") -> sqlite3.Connection:
    db = sqlite3.connect(path, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    initialize(db)
    return db


@contextmanager
def transaction(db: sqlite3.Connection):
    """Nested callers share one atomic transition through SQLite savepoints."""
    if db.in_transaction:
        name = f"sp_{id(db)}"
        db.execute(f"SAVEPOINT {name}")
        try:
            yield
        except BaseException:
            db.execute(f"ROLLBACK TO {name}")
            db.execute(f"RELEASE {name}")
            raise
        else:
            db.execute(f"RELEASE {name}")
    else:
        db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            db.rollback()
            raise
        else:
            db.commit()


def initialize(db: sqlite3.Connection) -> None:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS fact_versions (
      fact_id TEXT NOT NULL, version INTEGER NOT NULL CHECK(version > 0),
      entity TEXT NOT NULL, attribute TEXT NOT NULL, value TEXT,
      change_type TEXT NOT NULL, valid_from TEXT NOT NULL, valid_to TEXT,
      event_id TEXT NOT NULL, event_sequence INTEGER NOT NULL DEFAULT 0,
      supersedes_fact_id TEXT, supersedes_version INTEGER,
      created_at TEXT NOT NULL,
      PRIMARY KEY (fact_id, version),
      FOREIGN KEY (supersedes_fact_id, supersedes_version)
        REFERENCES fact_versions(fact_id, version),
      CHECK(valid_to IS NULL OR valid_to > valid_from)
    );
    CREATE INDEX IF NOT EXISTS fact_key_time ON fact_versions(entity, attribute, valid_from, event_sequence);
    CREATE INDEX IF NOT EXISTS fact_event ON fact_versions(event_id);
    CREATE TRIGGER IF NOT EXISTS fact_no_update BEFORE UPDATE ON fact_versions
      BEGIN SELECT RAISE(ABORT, 'fact history is immutable'); END;
    CREATE TRIGGER IF NOT EXISTS fact_no_delete BEFORE DELETE ON fact_versions
      BEGIN SELECT RAISE(ABORT, 'fact history is immutable'); END;
    CREATE TABLE IF NOT EXISTS summaries (
      summary_id TEXT PRIMARY KEY, text TEXT NOT NULL, text_template TEXT NOT NULL,
      is_stale INTEGER NOT NULL DEFAULT 0 CHECK(is_stale IN (0,1)),
      stale_since TEXT, stale_episode_id TEXT, queries_this_episode INTEGER NOT NULL DEFAULT 0,
      verification_cost_this_episode REAL NOT NULL DEFAULT 0,
      last_repair_at TEXT, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS summary_slots (
      summary_id TEXT NOT NULL REFERENCES summaries(summary_id),
      slot_name TEXT NOT NULL, fact_id TEXT NOT NULL,
      PRIMARY KEY(summary_id,slot_name)
    );
    CREATE TABLE IF NOT EXISTS summary_fact_sources (
      summary_id TEXT NOT NULL REFERENCES summaries(summary_id),
      fact_id TEXT NOT NULL, fact_version INTEGER NOT NULL,
      slot_name TEXT NOT NULL,
      PRIMARY KEY(summary_id,slot_name),
      UNIQUE(summary_id,fact_id,fact_version),
      FOREIGN KEY(fact_id,fact_version) REFERENCES fact_versions(fact_id,version)
    );
    CREATE INDEX IF NOT EXISTS source_reverse ON summary_fact_sources(fact_id,summary_id);
    CREATE TABLE IF NOT EXISTS summary_dependencies (
      parent_summary_id TEXT NOT NULL REFERENCES summaries(summary_id),
      child_summary_id TEXT NOT NULL REFERENCES summaries(summary_id),
      PRIMARY KEY(parent_summary_id,child_summary_id)
    );
    CREATE TABLE IF NOT EXISTS stale_events (
      summary_id TEXT NOT NULL REFERENCES summaries(summary_id),
      event_id TEXT NOT NULL, PRIMARY KEY(summary_id,event_id)
    );
    CREATE VIRTUAL TABLE IF NOT EXISTS summary_fts USING fts5(summary_id UNINDEXED,text);
    """)

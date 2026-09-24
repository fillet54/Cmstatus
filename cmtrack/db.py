import sqlite3
from pathlib import Path

from flask import current_app, g

SCHEMA = Path(__file__).with_name("schema.sql")


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Columns added after a table first shipped: (table, column, definition).
# CREATE TABLE IF NOT EXISTS won't add them to an existing DB, so add them here.
MIGRATIONS = [
    ("release", "released_at", "TEXT"),
    ("version", "lineage", "TEXT NOT NULL DEFAULT 'auto'"),
    ("ci", "release_source", "TEXT"),
    ("ci", "source_params", "TEXT NOT NULL DEFAULT '{}'"),
    ("ci", "require_tested", "INTEGER NOT NULL DEFAULT 1"),
    ("ci", "last_sync", "TEXT"),
    ("release", "source_key", "TEXT"),
    ("release", "source_state", "TEXT CHECK (source_state IN ('synced', 'missing'))"),
    ("release", "pinned", "TEXT NOT NULL DEFAULT '[]'"),
    ("version", "source_key", "TEXT"),
    ("version", "source_state", "TEXT CHECK (source_state IN ('synced', 'missing'))"),
    ("version", "pinned", "TEXT NOT NULL DEFAULT '[]'"),
]

INDEXES = [
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_release_source_key ON release(ci_id, source_key) WHERE source_key IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_version_source_key ON version(ci_id, source_key) WHERE source_key IS NOT NULL",
]

# Tables and indexes that no longer exist in the model (tickets live only in the ticket source; release
# policies and spawning were replaced by release sources). An old database keeps its unused policy table
# and ci.policy_id column: SQLite can't drop a column that has a foreign key.
DROPPED = ["ticket_version", "ticket"]
DROPPED_INDEXES = ["ux_release_child_reason"]


def init_db(conn):
    conn.executescript(SCHEMA.read_text())
    for table in DROPPED:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    for table, column, definition in MIGRATIONS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    for index in DROPPED_INDEXES:
        conn.execute(f"DROP INDEX IF EXISTS {index}")
    for sql in INDEXES:
        conn.execute(sql)
    conn.commit()


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()

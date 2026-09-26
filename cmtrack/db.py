import sqlite3
from pathlib import Path

from flask import current_app, g

SCHEMA = Path(__file__).with_name("schema.sql")


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Columns added after a table first shipped: (table, column, definition[, backfill SQL]).
# CREATE TABLE IF NOT EXISTS won't add them to an existing DB, so add them here. The backfill runs once,
# right after the column is added.
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
    ("baseline", "derived_from_id", "INTEGER REFERENCES baseline(id)",
     "UPDATE baseline SET derived_from_id = supersedes_id"),   # best guess: each built on the one it replaced
    # IFCs spawn from one another instead of nesting: a parent becomes the spawn point (its latest approved
    # HSCM, else its latest HSCM; a parent with none leaves no link)
    ("ifc", "spawned_from_id", "INTEGER REFERENCES baseline(id)",
     "UPDATE ifc SET spawned_from_id = (SELECT b.id FROM baseline b WHERE b.ifc_id = ifc.parent_id "
     "ORDER BY b.status = 'approved' DESC, b.id DESC LIMIT 1) WHERE parent_id IS NOT NULL"),
    ("ifc", "final_id", "INTEGER REFERENCES baseline(id)"),
    # an IFC's HSCMs are a straight sequence of builds, numbered in creation order
    ("baseline", "seq", "INTEGER NOT NULL DEFAULT 0",
     "UPDATE baseline SET seq = (SELECT COUNT(*) FROM baseline b WHERE b.ifc_id = baseline.ifc_id AND b.id <= baseline.id)",
     "UPDATE baseline SET derived_from_id = COALESCE((SELECT b.id FROM baseline b WHERE b.ifc_id = baseline.ifc_id "
     "AND b.seq = baseline.seq - 1), (SELECT spawned_from_id FROM ifc WHERE ifc.id = baseline.ifc_id))"),
]

INDEXES = [
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_release_source_key ON release(ci_id, source_key) WHERE source_key IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_version_source_key ON version(ci_id, source_key) WHERE source_key IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_baseline_seq ON baseline(ifc_id, seq)",
]

# Tables and indexes that no longer exist in the model (tickets live only in the ticket source; release
# policies and spawning were replaced by release sources). An old database keeps its unused policy table,
# ci.policy_id and ifc.parent_id (IFCs now spawn instead of nest): SQLite can't drop a column that has a
# foreign key.
DROPPED = ["ticket_version", "ticket"]
DROPPED_INDEXES = ["ux_release_child_reason"]


def init_db(conn):
    conn.executescript(SCHEMA.read_text())
    for table in DROPPED:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    for table, column, definition, *backfill in MIGRATIONS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            for sql in backfill:
                conn.execute(sql)
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

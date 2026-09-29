"""Shared backlogs: everything about them lives in this package (see README.md here for the map).

    schema.sql                    the backlog, backlog_ci and backlog_item tables
    rank.py                       lexorank strings: between(lo, hi), spread(n)
    stores.py                     where membership + ranks live: SqliteStore, JiraRankStore (a Jira custom field)
    service.py                    domain logic (create, add, move, pull, rebalance, view)
    routes.py                     one blueprint: JSON API under /api/backlogs, pages under /backlogs
    templates/backlog/            list.html, page.html, _items.html, _macros.html (rank_item), _ci_card.html
    static/                       backlog.js (native drag and drop), backlog.css

The app wires it in with two calls (cmtrack/__init__.py): ``init_db(conn)`` after the core schema, and
``init_app(app)`` to register the blueprint.
"""
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")


# Columns added after the tables first shipped, so databases made before them keep working.
COLUMNS = [("backlog", "store", "TEXT NOT NULL DEFAULT 'sqlite'"), ("backlog", "store_params", "TEXT NOT NULL DEFAULT '{}'")]


def init_db(conn):
    """Create the backlog tables (after the core schema: backlog_ci references ci)."""
    conn.executescript(SCHEMA.read_text())
    for table, column, definition in COLUMNS:
        if column not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    conn.commit()


def init_app(app):
    """Register the blueprint and the rank stores.

    BACKLOG_STORES         {name: RankStore} beside the built-in "sqlite"; default: CMTRACK_BACKLOG_STORES,
                           e.g. "jira=cmtrack.backlog.stores:jira_store_from_env"
    BACKLOG_DEFAULT_STORE  store for new backlogs that don't name one (CMTRACK_BACKLOG_DEFAULT_STORE, "sqlite")
    """
    import os
    from ..tickets import load_sources
    from .routes import bp
    if app.config.get("BACKLOG_STORES") is None:
        app.config["BACKLOG_STORES"] = load_sources(os.environ.get("CMTRACK_BACKLOG_STORES"), "backlog store")
    app.config.setdefault("BACKLOG_DEFAULT_STORE", os.environ.get("CMTRACK_BACKLOG_DEFAULT_STORE", "sqlite"))
    app.register_blueprint(bp)

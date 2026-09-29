"""Shared backlogs: everything about them lives in this package (see README.md here for the map).

    schema.sql                    the backlog, backlog_ci and backlog_item tables
    rank.py                       lexorank strings: between(lo, hi), spread(n)
    service.py                    domain logic (create, add, move, pull, rebalance, view)
    routes.py                     one blueprint: JSON API under /api/backlogs, pages under /backlogs
    templates/backlog/            list.html, page.html, _items.html, _macros.html (rank_item), _ci_card.html
    static/                       backlog.js (native drag and drop), backlog.css

The app wires it in with two calls (cmtrack/__init__.py): ``init_db(conn)`` after the core schema, and
``init_app(app)`` to register the blueprint.
"""
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")


def init_db(conn):
    """Create the backlog tables (after the core schema: backlog_ci references ci)."""
    conn.executescript(SCHEMA.read_text())
    conn.commit()


def init_app(app):
    from .routes import bp
    app.register_blueprint(bp)

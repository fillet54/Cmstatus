"""cmtrack: configuration tracking for CSCIs, releases, versions, IFCs and HSCM baselines."""
import os
import sqlite3

from flask import Flask, jsonify, render_template, request

from . import db, tickets, ui
from .service import CMError, backfill_lineage


def create_app(config=None):
    app = Flask(__name__)
    app.config.update(
        DATABASE=os.environ.get("CMTRACK_DB", "cmtrack.db"),
        POLICY_DIR=os.environ.get("CMTRACK_POLICY_DIR", "policies"),
        TICKET_SOURCES=None,       # {name: TicketSource}; defaults to CMTRACK_TICKET_SOURCES
    )
    app.config.update(config or {})
    if app.config["TICKET_SOURCES"] is None:
        app.config["TICKET_SOURCES"] = tickets.load_sources(os.environ.get("CMTRACK_TICKET_SOURCES"))
    app.json.sort_keys = False

    conn = db.connect(app.config["DATABASE"])
    db.init_db(conn)
    with conn:
        backfill_lineage(conn)
    conn.close()

    ui.init_app(app)

    from .api import bp
    from .views import bp as ui_bp
    app.register_blueprint(bp, url_prefix="/api")
    app.register_blueprint(ui_bp)
    app.teardown_appcontext(db.close_db)

    @app.errorhandler(CMError)
    def _cm_error(e):
        if not request.path.startswith("/api"):
            return render_template("error.html", error=e), e.status
        body = {"error": e.message}
        if e.problems:
            body["problems"] = e.problems
        return jsonify(body), e.status

    @app.errorhandler(sqlite3.IntegrityError)
    def _integrity(e):
        return jsonify({"error": f"integrity error: {e}"}), 409

    return app

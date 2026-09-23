"""cmtrack: configuration tracking for CSCIs, releases, versions, IFCs and HSCM baselines."""
import os
import sqlite3

from flask import Flask, jsonify

from . import db
from .service import CMError


def create_app(config=None):
    app = Flask(__name__)
    app.config.update(
        DATABASE=os.environ.get("CMTRACK_DB", "cmtrack.db"),
        POLICY_DIR=os.environ.get("CMTRACK_POLICY_DIR", "policies"),
    )
    app.config.update(config or {})
    app.json.sort_keys = False

    conn = db.connect(app.config["DATABASE"])
    db.init_db(conn)
    conn.close()

    from .api import bp
    app.register_blueprint(bp, url_prefix="/api")
    app.teardown_appcontext(db.close_db)

    @app.errorhandler(CMError)
    def _cm_error(e):
        body = {"error": e.message}
        if e.problems:
            body["problems"] = e.problems
        return jsonify(body), e.status

    @app.errorhandler(sqlite3.IntegrityError)
    def _integrity(e):
        return jsonify({"error": f"integrity error: {e}"}), 409

    return app

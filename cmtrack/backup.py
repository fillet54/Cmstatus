"""Backups of the SQLite database to Nexus (or a directory): snapshot, check, gzip, upload.

A backup is SQLite's online backup API (a consistent copy even while the app is writing), an integrity check
of the copy, gzip, and one HTTP PUT to a Nexus **raw** repository:

    PUT {CMTRACK_BACKUP_URL}/{prefix}/cmtrack-20260930T020000Z.db.gz

Every run is recorded in the audit log (entity "backup": started / done / failed, with size and sha256).
Nothing is deleted here; keep the last N with a Nexus cleanup policy on the repository.

Three ways to run one, with no job server:

    1. The endpoint, e.g. from a scheduled GitLab pipeline:
           curl -fsS -X POST -H "Authorization: Bearer $CMTRACK_BACKUP_TOKEN" https://cmtrack.example/api/admin/backup
       GET on the same URL lists recent backups. Both are disabled until CMTRACK_BACKUP_TOKEN is set.
    2. A daily timer inside the app: set CMTRACK_BACKUP_DAILY_AT=02:00 (UTC). A background thread wakes every
       few minutes and backs up once per day after that time. Several worker processes (or the debug reloader)
       don't cause duplicates: each run is claimed in the database first. A missed run (the server was down)
       happens as soon as it's back.
    3. By hand, on the server:  python -m cmtrack.backup           (restore: python -m cmtrack.backup --restore URL FILE)

Settings (environment, or the same names without CMTRACK_ in app.config under BACKUP_*):
    CMTRACK_BACKUP_URL       where to put backups: a Nexus raw repository URL
                             (https://nexus.example/repository/cmtrack-backups), or a local directory
    CMTRACK_BACKUP_USER / CMTRACK_BACKUP_PASSWORD      Nexus credentials (Basic auth); or
    CMTRACK_BACKUP_BEARER    a Nexus user token sent as Bearer
    CMTRACK_BACKUP_PREFIX    folder inside the repository (default "cmtrack")
    CMTRACK_BACKUP_TOKEN     shared secret for the endpoint
    CMTRACK_BACKUP_DAILY_AT  HH:MM UTC for the built-in daily timer (unset = no timer)
"""
import argparse
import base64
import datetime as dt
import gzip
import hashlib
import hmac
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from flask import Blueprint, current_app, jsonify, request

log = logging.getLogger("cmtrack.backup")

SETTINGS = ("URL", "USER", "PASSWORD", "BEARER", "PREFIX", "TOKEN", "DAILY_AT")
IN_PROGRESS = dt.timedelta(hours=1)      # a started backup blocks another one this long (then it's retried)
WAKE_SECONDS = 300


class BackupError(RuntimeError):
    pass


def settings(config=None):
    """BACKUP_* from app config, else CMTRACK_BACKUP_* from the environment."""
    config = config or {}
    out = {k: config.get(f"BACKUP_{k}", os.environ.get(f"CMTRACK_BACKUP_{k}")) for k in SETTINGS}
    out["PREFIX"] = (out["PREFIX"] or "cmtrack").strip("/")
    return out


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _sql_time(t):
    """The format SQLite's datetime('now') uses in event.at."""
    return t.strftime("%Y-%m-%d %H:%M:%S")


# ----------------------------------------------------------------------------- the backup itself

def snapshot(db_path):
    """Gzipped bytes of a consistent copy of the database, checked with PRAGMA integrity_check."""
    with tempfile.TemporaryDirectory() as tmp:
        copy = os.path.join(tmp, "copy.db")
        src, dst = sqlite3.connect(db_path), sqlite3.connect(copy)
        try:
            src.backup(dst)                                   # online backup: safe while the app is writing
            result = dst.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            src.close()
            dst.close()
        if result != "ok":
            raise BackupError(f"integrity check of the copy failed: {result}")
        with open(copy, "rb") as f:
            return gzip.compress(f.read(), mtime=0)


def upload(conf, name, data):
    """Put ``data`` at ``{URL}/{PREFIX}/{name}``: an HTTP PUT for a URL, a file copy for a directory."""
    dest = (conf["URL"] or "").rstrip("/")
    if not dest:
        raise BackupError("no backup destination: set CMTRACK_BACKUP_URL")
    if not dest.startswith(("http://", "https://")):
        folder = os.path.join(dest, conf["PREFIX"])
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name)
        with open(path, "wb") as f:
            f.write(data)
        return path
    url = f"{dest}/{urllib.parse.quote(conf['PREFIX'])}/{name}"
    req = urllib.request.Request(url, data=data, method="PUT", headers={"Content-Type": "application/gzip"})
    if conf["BEARER"]:
        req.add_header("Authorization", f"Bearer {conf['BEARER']}")
    elif conf["USER"]:
        req.add_header("Authorization", "Basic " + base64.b64encode(
            f"{conf['USER']}:{conf['PASSWORD'] or ''}".encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            r.read()
    except urllib.error.HTTPError as e:
        raise BackupError(f"PUT {url} -> HTTP {e.code}: {e.read().decode(errors='replace')[:200]}") from None
    except urllib.error.URLError as e:
        raise BackupError(f"PUT {url} failed: {e.reason}") from None
    return url


def _event(db_path, action, **detail):
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            conn.execute("INSERT INTO event (entity, action, detail) VALUES ('backup', ?, ?)",
                         (action, json.dumps(detail)))
    finally:
        conn.close()


def run_backup(db_path, conf, reason="manual"):
    """Snapshot, upload and record one backup. Returns {url, name, bytes, sha256}; raises BackupError."""
    name = f"cmtrack-{_now().strftime('%Y%m%dT%H%M%SZ')}.db.gz"
    try:
        data = snapshot(db_path)
        where = upload(conf, name, data)
    except (BackupError, OSError, sqlite3.Error) as e:
        _event(db_path, "failed", reason=reason, error=str(e))
        raise BackupError(str(e)) from e
    out = {"url": where, "name": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    _event(db_path, "done", reason=reason, **out)
    log.info("backup %s (%d bytes) -> %s", name, len(data), where)
    return out


def recent(db_path, limit=20):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT at, action, detail FROM event WHERE entity = 'backup' ORDER BY id DESC LIMIT ?",
                            (limit,)).fetchall()
    finally:
        conn.close()
    return [{"at": r["at"], "action": r["action"], **json.loads(r["detail"])} for r in rows]


# ----------------------------------------------------------------------------- the daily timer

def slot_start(daily_at, now=None):
    """The most recent HH:MM (UTC) at or before ``now``."""
    now = now or _now()
    hh, mm = (int(x) for x in daily_at.split(":"))
    start = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return start if start <= now else start - dt.timedelta(days=1)


def claim(db_path, daily_at, now=None):
    """True if this process should back up now: nothing done since today's slot and nothing in progress.
    The check and the 'started' record happen in one write transaction, so only one process wins."""
    now = now or _now()
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        done = conn.execute("SELECT 1 FROM event WHERE entity = 'backup' AND action = 'done' AND at >= ?",
                            (_sql_time(slot_start(daily_at, now)),)).fetchone()
        busy = conn.execute("SELECT 1 FROM event WHERE entity = 'backup' AND action = 'started' AND at >= ?",
                            (_sql_time(now - IN_PROGRESS),)).fetchone()
        if done or busy:
            conn.execute("ROLLBACK")
            return False
        conn.execute("INSERT INTO event (entity, action, detail, at) VALUES ('backup', 'started', ?, ?)",
                     (json.dumps({"reason": "daily", "pid": os.getpid()}), _sql_time(now)))
        conn.execute("COMMIT")
        return True
    finally:
        conn.close()


def start_daily(db_path, conf):
    """Start the background thread for CMTRACK_BACKUP_DAILY_AT (a daemon: it dies with the process)."""
    daily_at = conf["DAILY_AT"]
    slot_start(daily_at)                                   # fail at startup on a bad HH:MM, not at 2am

    def loop():
        while True:
            try:
                if claim(db_path, daily_at):
                    run_backup(db_path, conf, reason="daily")
            except Exception:                              # keep the timer alive; the failure is in the log
                log.exception("daily backup failed")
            time.sleep(WAKE_SECONDS)

    thread = threading.Thread(target=loop, name="cmtrack-backup", daemon=True)
    thread.start()
    return thread


def init_app(app):
    """Register the endpoint, and start the daily timer if BACKUP_DAILY_AT / CMTRACK_BACKUP_DAILY_AT is set."""
    app.register_blueprint(bp)
    conf = settings(app.config)
    if conf["DAILY_AT"] and not app.config.get("TESTING"):
        start_daily(app.config["DATABASE"], conf)


# ----------------------------------------------------------------------------- the endpoint

bp = Blueprint("backup", __name__)


def _authorised():
    token = settings(current_app.config)["TOKEN"]
    if not token:
        return jsonify({"error": "backups over HTTP are disabled; set CMTRACK_BACKUP_TOKEN"}), 403
    sent = request.headers.get("Authorization", "")
    if not hmac.compare_digest(sent.encode(), f"Bearer {token}".encode()):
        return jsonify({"error": "missing or wrong bearer token"}), 401
    return None


@bp.post("/api/admin/backup")
def backup_now():
    """Back up now (e.g. from a scheduled GitLab job). 201 with the upload, 502 if the upload failed."""
    denied = _authorised()
    if denied:
        return denied
    try:
        out = run_backup(current_app.config["DATABASE"], settings(current_app.config), reason="api")
    except BackupError as e:
        return jsonify({"error": str(e)}), 502
    return jsonify(out), 201


@bp.get("/api/admin/backup")
def backup_status():
    """Recent backups (started / done / failed), newest first."""
    denied = _authorised()
    if denied:
        return denied
    return jsonify(recent(current_app.config["DATABASE"]))


# ----------------------------------------------------------------------------- command line

def restore(url, dest, conf):
    """Download a backup (Nexus URL or file path), check it, and write it to ``dest`` (which must not exist)."""
    if os.path.exists(dest):
        raise BackupError(f"{dest} already exists; restore to a new file and point CMTRACK_DB at it")
    if url.startswith(("http://", "https://")):
        req = urllib.request.Request(url)
        if conf["BEARER"]:
            req.add_header("Authorization", f"Bearer {conf['BEARER']}")
        elif conf["USER"]:
            req.add_header("Authorization", "Basic " + base64.b64encode(
                f"{conf['USER']}:{conf['PASSWORD'] or ''}".encode()).decode())
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
    else:
        with open(url, "rb") as f:
            data = f.read()
    tmp = dest + ".part"
    with open(tmp, "wb") as f:
        f.write(gzip.decompress(data))
    conn = sqlite3.connect(tmp)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    if result != "ok":
        os.remove(tmp)
        raise BackupError(f"downloaded backup fails its integrity check: {result}")
    shutil.move(tmp, dest)
    return dest


def main():
    parser = argparse.ArgumentParser(description="Back up cmtrack's SQLite database (see cmtrack/backup.py)")
    parser.add_argument("--db", default=os.environ.get("CMTRACK_DB", "cmtrack.db"))
    parser.add_argument("--restore", nargs=2, metavar=("URL_OR_FILE", "NEW_DB"), help="restore a backup instead")
    args = parser.parse_args()
    conf = settings()
    try:
        if args.restore:
            print(f"restored to {restore(args.restore[0], args.restore[1], conf)}")
        else:
            print(json.dumps(run_backup(args.db, conf, reason="cli"), indent=2))
    except BackupError as e:
        raise SystemExit(f"backup failed: {e}")


if __name__ == "__main__":
    main()

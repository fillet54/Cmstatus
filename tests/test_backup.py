"""Database backups: snapshot, upload to (a stand-in for) Nexus, the endpoint, the daily claim, restore.
Run: python -m unittest discover -s tests"""
import datetime as dt
import gzip
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from cmtrack import backup, create_app

UTC = dt.timezone.utc


class FakeNexus(BaseHTTPRequestHandler):
    """A raw repository: PUT stores, GET returns; 'deny' in the path answers 401."""
    files, requests = {}, []

    def log_message(self, *a):
        pass

    def do_PUT(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        FakeNexus.requests.append((self.path, self.headers.get("Content-Type"), self.headers.get("Authorization")))
        if "deny" in self.path:
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"Unauthorized")
            return
        FakeNexus.files[self.path] = body
        self.send_response(201)
        self.end_headers()

    def do_GET(self):
        body = FakeNexus.files.get(self.path)
        self.send_response(200 if body else 404)
        self.end_headers()
        self.wfile.write(body or b"")


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        self.app = create_app({"DATABASE": self.db, "TICKET_SOURCES": {}, "BACKUP_TOKEN": "s3cret",
                               "BACKUP_URL": os.path.join(self.tmp, "backups")})
        self.c = self.app.test_client()
        self.c.post("/api/cis", json={"name": "NAV-SW"})
        FakeNexus.files, FakeNexus.requests = {}, []
        self.server = HTTPServer(("127.0.0.1", 0), FakeNexus)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.nexus = f"http://127.0.0.1:{self.server.server_port}/repository/cm-backups"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmp)

    def conf(self, **kw):
        return {**backup.settings({"BACKUP_URL": self.nexus}), **kw}

    def auth(self, token="s3cret"):
        return {"Authorization": f"Bearer {token}"}

    def test_snapshot_is_a_working_database(self):
        data = backup.snapshot(self.db)
        path = os.path.join(self.tmp, "restored.db")
        with open(path, "wb") as f:
            f.write(gzip.decompress(data))
        conn = sqlite3.connect(path)
        self.assertEqual(conn.execute("SELECT name FROM ci").fetchall(), [("NAV-SW",)])
        conn.close()

    def test_endpoint(self):
        self.app.config["BACKUP_TOKEN"] = None
        self.assertEqual(self.c.post("/api/admin/backup").status_code, 403)           # off until a token is set
        self.app.config["BACKUP_TOKEN"] = "s3cret"
        self.assertEqual(self.c.post("/api/admin/backup", headers=self.auth("nope")).status_code, 401)
        self.assertEqual(self.c.post("/api/admin/backup").status_code, 401)
        r = self.c.post("/api/admin/backup", headers=self.auth())
        self.assertEqual(r.status_code, 201, r.get_json())
        out = r.get_json()
        self.assertTrue(os.path.exists(out["url"]))                                   # a directory destination
        self.assertRegex(out["name"], r"^cmtrack-\d{8}T\d{6}Z\.db\.gz$")
        status = self.c.get("/api/admin/backup", headers=self.auth()).get_json()
        self.assertEqual((status[0]["action"], status[0]["reason"], status[0]["sha256"]), ("done", "api", out["sha256"]))

    def test_upload_to_nexus(self):
        out = backup.run_backup(self.db, self.conf(USER="svc", PASSWORD="pw", PREFIX="prod/cmtrack"))
        path, ctype, auth = FakeNexus.requests[0]
        self.assertEqual(path, f"/repository/cm-backups/prod/cmtrack/{out['name']}")
        self.assertEqual((ctype, auth.startswith("Basic ")), ("application/gzip", True))
        self.assertEqual(out["url"], self.nexus + f"/prod/cmtrack/{out['name']}")
        backup.run_backup(self.db, self.conf(BEARER="tok"))
        self.assertEqual(FakeNexus.requests[1][2], "Bearer tok")
        # restore from the URL: refuses to overwrite, then writes a checked copy
        with self.assertRaises(backup.BackupError):
            backup.restore(out["url"], self.db, self.conf())
        dest = backup.restore(out["url"], os.path.join(self.tmp, "back.db"), self.conf())
        self.assertEqual(sqlite3.connect(dest).execute("SELECT COUNT(*) FROM ci").fetchone()[0], 1)

    def test_failures_are_recorded(self):
        with self.assertRaises(backup.BackupError) as e:
            backup.run_backup(self.db, self.conf(PREFIX="deny"))
        self.assertIn("HTTP 401", str(e.exception))
        with self.assertRaises(backup.BackupError):
            backup.run_backup(self.db, self.conf(URL=None))
        self.assertEqual([x["action"] for x in backup.recent(self.db)], ["failed", "failed"])
        self.app.config["BACKUP_URL"] = self.nexus.replace("cm-backups", "deny")
        r = self.c.post("/api/admin/backup", headers=self.auth())
        self.assertEqual(r.status_code, 502)
        self.assertIn("HTTP 401", r.get_json()["error"])

    def test_daily_claim(self):
        t = dt.datetime(2026, 9, 30, 2, 5, tzinfo=UTC)
        self.assertEqual(backup.slot_start("02:00", t), dt.datetime(2026, 9, 30, 2, 0, tzinfo=UTC))
        self.assertEqual(backup.slot_start("03:00", t), dt.datetime(2026, 9, 29, 3, 0, tzinfo=UTC))
        self.assertTrue(backup.claim(self.db, "02:00", t))                  # first process wins
        self.assertFalse(backup.claim(self.db, "02:00", t))                 # the others see it in progress
        conn = sqlite3.connect(self.db)
        with conn:
            conn.execute("INSERT INTO event (entity, action, at) VALUES ('backup', 'done', ?)",
                         (backup._sql_time(t + dt.timedelta(minutes=2)),))
        conn.close()
        later = t + dt.timedelta(hours=3)
        self.assertFalse(backup.claim(self.db, "02:00", later))             # done since today's slot
        # a failed run blocks for an hour (in progress), then it's retried
        conn = sqlite3.connect(self.db)
        with conn:
            conn.execute("DELETE FROM event WHERE entity = 'backup'")
        conn.close()
        self.assertTrue(backup.claim(self.db, "02:00", t))
        self.assertFalse(backup.claim(self.db, "02:00", t + dt.timedelta(minutes=30)))
        self.assertTrue(backup.claim(self.db, "02:00", t + dt.timedelta(minutes=61)))
        with self.assertRaises(ValueError):
            backup.start_daily(self.db, {"DAILY_AT": "2am"})


if __name__ == "__main__":
    unittest.main()

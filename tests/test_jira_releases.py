import json
import os
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from cmtrack import create_app
from cmtrack.jira_releases import JiraReleaseSource
from cmtrack.jira_tickets import JiraClient
from cmtrack.releases import ReleaseRecord, SourceConfigError, Unplaced


def v(id, name, date=None, released=False, archived=False, description=None):
    return {"id": str(id), "name": name, "releaseDate": date, "released": released, "archived": archived,
            **({"description": description} if description else {})}


VERSIONS = {
    "NAVL": [v(1, "2027.Q1", "2027-03-10", description="Q1 release"), v(2, "2027.Q1-b1", "2027-01-10", True),
             v(3, "2027.Q1-b2", "2027-02-10"), v(4, "2027.Q1.P1", "2027-04-01"), v(5, "core-tools-3")],
    "NAVX": [v(11, "2027.Q1", "2027-03-15"), v(12, "2027.Q1-b1", "2027-01-12", True), v(13, "2027.Q1-b2"),
             v(14, "maps-data-9")],
}
PARAMS = {"patterns": {"planned": r"(?P<line>\d{4}\.Q\d)", "build": r"(?P<line>\d{4}\.Q\d)-b(?P<n>\d+)",
                       "patch": r"(?P<line>\d{4}\.Q\d)\.P(?P<n>\d+)"},
          "match": r"2\d{3}\..*"}


class FakeJira(BaseHTTPRequestHandler):
    asked = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        project = self.path.split("/")[-2]
        FakeJira.asked.append(project)
        data = json.dumps(VERSIONS.get(project, [])).encode()
        self.send_response(200 if project in VERSIONS else 404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)


class JiraReleaseSourceTests(unittest.TestCase):
    def setUp(self):
        FakeJira.asked = []
        self.server = HTTPServer(("127.0.0.1", 0), FakeJira)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.source = JiraReleaseSource(JiraClient(f"http://127.0.0.1:{self.server.server_port}", token="t"))
        self.ci = {"name": "NAV-SW", "cscs": [{"name": "nav-core", "jira_project": "NAVL"},
                                              {"name": "nav-maps", "jira_project": "NAVX"},
                                              {"name": "nav-two", "jira_project": "NAVL"}, {"name": "docs"}]}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_merges_projects(self):
        out = self.source.releases(self.ci, PARAMS)
        self.assertEqual(FakeJira.asked, ["NAVL", "NAVX"])                       # each CSC project once
        q1 = next(r for r in out if isinstance(r, ReleaseRecord) and r.name == "2027.Q1")
        self.assertEqual((q1.key, q1.target_date, q1.reason, q1.released), ("2027.Q1", "2027-03-15", "Q1 release", False))
        self.assertEqual([(b.name, b.planned_date) for b in q1.builds], [("2027.Q1-b1", "2027-01-12"),   # the latest
                                                                          ("2027.Q1-b2", "2027-02-10")])
        p1 = next(r for r in out if r.name == "2027.Q1.P1")
        self.assertEqual((p1.kind, p1.parent_key), ("patch", "2027.Q1"))               # only in NAVL: still used
        self.assertEqual(sorted(r.name for r in out), ["2027.Q1", "2027.Q1.P1"])      # core-tools-3 etc. don't match
        b1 = self.source.versions(self.ci, PARAMS)[1]
        self.assertEqual((b1.name, b1.released), ("2027.Q1-b1", True))                 # released in both projects

    def test_options(self):
        out = self.source.releases(self.ci, {**PARAMS, "require_all": True})
        self.assertIn(Unplaced("2027.Q1.P1", "2027.Q1.P1", "not in NAVX"), out)
        self.source.releases(self.ci, {**PARAMS, "projects": ["NAVX"]})
        self.assertEqual(FakeJira.asked[-1:], ["NAVX"])
        with self.assertRaises(SourceConfigError):
            self.source.releases({"name": "X", "cscs": []}, PARAMS)                   # no projects
        with self.assertRaises(SourceConfigError):
            self.source.releases(self.ci, {**PARAMS, "match": "("})

    def test_sync_in_cmtrack(self):
        tmp = tempfile.mkdtemp()
        try:
            app = create_app({"DATABASE": os.path.join(tmp, "t.db"), "RELEASE_SOURCES": {"jira": self.source}})
            c = app.test_client()
            call = lambda m, p, j=None: getattr(c, m)("/api" + p, json=j).get_json()
            call("post", "/cis", {"name": "NAV-SW", "release_source": "jira", "source_params": PARAMS})
            call("post", "/cis/NAV-SW/cscs", {"name": "nav-core", "jira_project": "NAVL", "affected_product": "core"})
            call("post", "/cis/NAV-SW/cscs", {"name": "nav-maps", "jira_project": "NAVX", "affected_product": "maps"})
            s = call("post", "/cis/NAV-SW/sync")
            self.assertEqual(s["created"], ["2027.Q1", "2027.Q1-b1", "2027.Q1-b2", "2027.Q1.P1", "2027.Q1.P1"])   # P1:
                                                                                         # release + its build
            self.assertEqual(call("post", "/cis/NAV-SW/sync")["created"], [])           # idempotent
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()

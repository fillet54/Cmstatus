"""The sample Jira ticket source, against a local stand-in for Jira's REST API. The stand-in ignores JQL and
returns every issue, so these tests also show the source filters its results exactly.
Run: python -m unittest discover -s tests"""
import json
import os
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from cmtrack import create_app
from cmtrack.jira_tickets import Fields, JiraClient, JiraError, JiraTicketSource, plain

FIELDS = [{"id": "summary", "name": "Summary", "schema": {"type": "string"}},
          {"id": "customfield_10500", "name": "Parent Ticket", "schema": {"type": "string"}},
          {"id": "customfield_10600", "name": "Affected Product", "schema": {"type": "option"}},
          {"id": "customfield_10700", "name": "Affected CIs", "schema": {"type": "array"}}]


def issue(key, type, status, category="indeterminate", parent=None, product=None, fix=(), cis=None):
    return {"key": key, "fields": {
        "summary": f"{key} summary", "issuetype": {"name": type}, "project": {"key": key.split("-")[0], "name": "x"},
        "status": {"name": status, "statusCategory": {"key": category}} if status else None,
        "fixVersions": [{"name": v} for v in fix], "assignee": {"name": "jdoe", "displayName": "J. Doe"},
        "customfield_10500": parent, "customfield_10600": {"value": product, "id": "1"} if product else None,
        "customfield_10700": [{"value": c} for c in cis] if cis else None}}


ISSUES = [
    issue("PRG-1", "Feature", "In Progress", cis=["NAV-SW"]),
    issue("PRG-2", "Discrepancy", "Done", "done", cis=["NAV-SW"]),
    issue("PRG-3", "Task", "Open", "new"),
    issue("PRG-10", "Feature", "Open", "new", cis=["DISPLAY-SW"]),
    issue("NAVL-1", "Story", "In Review", parent="PRG-1", product="core", fix=["2027.Q1-b1"]),
    issue("NAVL-2", "Story", "In Progress", product="core", fix=["2027.Q1-b1"]),               # no parent
    issue("NAVL-3", "Story", "Closed", "done", parent="PRG-1", product="core"),               # closed, no fix version
    issue("NAVL-4", "Bug", "Done", "done", parent="PRG-10", product="core", fix=["2027.Q1-b2"]),
    issue("NAVX-1", "Story", "Awaiting CCB", parent="PRG-1", product="maps", fix=["2027.Q1-b2"]),
    issue("NAVX-2", "Story", "Done", "done", parent="PRG-1", product="other", fix=["2027.Q1-b2"]),   # not our CSC
]


class FakeJira(BaseHTTPRequestHandler):
    searches, headers = [], []

    def log_message(self, *a):
        pass

    def _send(self, body):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send(FIELDS)

    def do_POST(self):
        q = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeJira.searches.append(q)
        FakeJira.headers.append(dict(self.headers))
        if "bad" in q["jql"]:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"errorMessages": ["bad JQL"]}')
            return
        page = ISSUES[q["startAt"]:q["startAt"] + q["maxResults"]]
        self._send({"total": len(ISSUES), "issues": [{"key": i["key"], "fields": {f: i["fields"].get(f) for f in q["fields"]}}
                                                     for i in page]})


class JiraTicketSourceTests(unittest.TestCase):
    def setUp(self):
        FakeJira.searches = []
        self.server = HTTPServer(("127.0.0.1", 0), FakeJira)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.client = JiraClient(f"http://127.0.0.1:{self.server.server_port}", token="t", page_size=3)
        self.source = JiraTicketSource(self.client, ["PRG"], top_types=["Feature", "Discrepancy"],
                                       fields={"parent": "Parent Ticket", "affected_product": "affected product",
                                               "cis": "Affected CIs"},
                                       status_map={"Awaiting CCB": "blocked"})

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_fields_and_values(self):
        f = Fields(self.client, parent="Parent Ticket", cis="Affected CIs")
        self.assertEqual((f.id("parent"), f.jql("parent"), f.id("fixVersions")), ("customfield_10500", "cf[10500]", "fixVersions"))
        self.assertEqual(f.clause("parent", ["PRG-1"]), 'cf[10500] ~ "PRG-1"'.join("()"))    # text field: ~
        self.assertEqual(f.clause("cis", ["B", "A"]), 'cf[10700] in ("A", "B")')             # others: in
        self.assertEqual(f.get(ISSUES[0], "cis"), ["NAV-SW"])
        with self.assertRaises(JiraError):
            Fields(self.client, x="No Such Field").id("x")
        self.assertEqual([plain(v) for v in ({"value": "a"}, {"key": "K", "name": "n"}, {"displayName": "D", "name": "u"},
                                              [{"name": "v1"}], "s", None)], ["a", "K", "D", ["v1"], "s", None])

    def test_records_and_states(self):
        by = {r.key: r for r in self.source.get_tickets([i["key"] for i in ISSUES] + ["NOPE-1"])}
        self.assertEqual(len(FakeJira.searches), 4)                                        # 10 issues, pages of 3
        self.assertTrue(FakeJira.searches[0]["jql"].startswith('key in ('))
        self.assertIn("customfield_10500", FakeJira.searches[0]["fields"])
        self.assertNotIn("NOPE-1", by)
        r = by["NAVL-1"]
        self.assertEqual((r.parent_key, r.project, r.affected_product, r.fix_versions, r.state, r.status, r.assignee),
                         ("PRG-1", "NAVL", "core", ["2027.Q1-b1"], "peer_review", "In Review", "J. Doe"))
        self.assertEqual((by["PRG-1"].project, by["PRG-1"].parent_key, by["PRG-1"].cis, by["PRG-1"].state),
                         (None, None, ["NAV-SW"], "in_progress"))
        self.assertTrue(by["PRG-1"].url.endswith("/browse/PRG-1"))
        self.assertEqual(by["NAVX-1"].state, "blocked")                                     # status_map from config
        self.assertEqual((by["NAVL-2"].state, by["NAVL-2"].state_reason), ("error", "no parent ticket in 'Parent Ticket'"))
        self.assertEqual(by["NAVL-3"].state_reason, "closed without a fix version")
        self.assertIn("isn't a top-level type", by["PRG-3"].state_reason)

    def test_queries(self):
        cscs = [{"name": "nav-core", "jira_project": "NAVL", "affected_product": "core"},
                {"name": "nav-maps", "jira_project": "NAVX", "affected_product": "maps"}]
        found = self.source.tickets_for_versions({"name": "NAV-SW"}, cscs, ["2027.Q1-b2"])
        self.assertEqual(sorted(r.key for r in found), ["NAVL-4", "NAVX-1"])               # not NAVX-2 (other CSC)
        self.assertEqual(FakeJira.searches[-1]["jql"], 'project in ("NAVL", "NAVX") AND (fixVersion in ("2027.Q1-b2") '
                                                       'AND cf[10600] in ("core", "maps")) ORDER BY key')
        self.assertEqual([r.key for r in self.source.get_children("PRG-1")], ["NAVL-1", "NAVL-3", "NAVX-1", "NAVX-2"])
        self.assertEqual(FakeJira.searches[-1]["jql"], '(cf[10500] ~ "PRG-1") ORDER BY key')  # exact match: not PRG-10's
        self.assertEqual([r.key for r in self.source.top_level_tickets({}, [{"name": "NAV-SW"}])], ["PRG-1"])
        self.assertEqual([r.key for r in self.source.query(["NAVX"])], ["NAVX-1", "NAVX-2"])

    def test_own_session_and_auth(self):
        import requests
        session = requests.Session()
        session.headers["X-Team"] = "cm"
        client = JiraClient(self.client.url, user="svc", password="pw", session=session)
        JiraTicketSource(client, ["PRG"], fields={"parent": "Parent Ticket"}).query(["PRG"])
        sent = FakeJira.headers[-1]
        self.assertEqual(sent["X-Team"], "cm")                                            # your session is used
        self.assertEqual(sent["Authorization"], "Basic c3ZjOnB3")                        # svc:pw

    def test_errors(self):
        with self.assertRaises(JiraError) as e:
            self.source.query(["PRG"], "bad")
        self.assertIn("HTTP 400", str(e.exception))
        with self.assertRaises(ValueError):
            JiraClient("http://x")

    def test_in_cmtrack(self):
        tmp = tempfile.mkdtemp()
        try:
            app = create_app({"DATABASE": os.path.join(tmp, "t.db"), "TICKET_SOURCES": {"jira": self.source}})
            c = app.test_client()
            call = lambda m, p, j=None: getattr(c, m)("/api" + p, json=j).get_json()
            call("post", "/cis", {"name": "NAV-SW"})
            call("post", "/cis/NAV-SW/cscs", {"name": "nav-core", "jira_project": "NAVL", "affected_product": "core"})
            call("post", "/cis/NAV-SW/cscs", {"name": "nav-maps", "jira_project": "NAVX", "affected_product": "maps"})
            call("post", "/cis/NAV-SW/releases", {"name": "2027.Q1", "builds": ["2027.Q1-b1", "2027.Q1-b2"]})
            work = call("get", "/cis/NAV-SW/work?versions=2027.Q1-b1,2027.Q1-b2")
            groups = {(i["parent"] or {}).get("key"): sorted(t["key"] for g in i["cscs"] for t in g["tickets"]) for i in work["items"]}
            self.assertEqual(groups["PRG-1"], ["NAVL-1", "NAVX-1"])
            self.assertEqual(groups["PRG-10"], ["NAVL-4"])
            t = call("get", "/tickets/PRG-1")
            self.assertEqual(t["summary"], "PRG-1 summary")
            call("post", "/backlogs", {"name": "Nav", "cis": ["NAV-SW"]})
            self.assertEqual(call("post", "/backlogs/Nav/pull")["added"], ["PRG-1"])       # open, affects NAV-SW
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()

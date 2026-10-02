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
from cmtrack.jira_tickets import Fields, JiraClient, JiraError, JiraTicketSource, analysis_rule, from_env, plain

FIELDS = [{"id": "summary", "name": "Summary", "schema": {"type": "string"}},
          {"id": "customfield_10500", "name": "Parent Ticket", "schema": {"type": "string"}},
          {"id": "customfield_10600", "name": "Affected Product", "schema": {"type": "option"}},
          {"id": "customfield_10700", "name": "Affected CIs", "schema": {"type": "array"}},
          {"id": "customfield_10800", "name": "Analysis State", "schema": {"type": "option"}}]


def issue(key, type, status, category="indeterminate", parent=None, product=None, fix=(), cis=None, analysis=None,
          labels=(), summary=None):
    return {"key": key, "fields": {"labels": list(labels),
        "customfield_10800": {"value": analysis} if analysis else None,
        "summary": summary or f"{key} summary", "issuetype": {"name": type}, "project": {"key": key.split("-")[0], "name": "x"},
        "status": {"name": status, "statusCategory": {"key": category}} if status else None,
        "fixVersions": [{"name": v} for v in fix], "assignee": {"name": "jdoe", "displayName": "J. Doe"},
        "customfield_10500": parent,
        "customfield_10600": [{"value": p, "id": "1"} for p in product] if isinstance(product, list)
                             else {"value": product, "id": "1"} if product else None,
        "customfield_10700": [{"value": c} for c in cis] if cis else None}}


ISSUES = [
    issue("PRG-1", "Feature", "In Progress", cis=["NAV-SW"], product=["core", "maps"], fix=["2027.Q1-b2"]),
    issue("PRG-2", "Discrepancy", "Done", "done", cis=["NAV-SW"]),
    issue("PRG-3", "Task", "Open", "new"),
    issue("PRG-10", "Feature", "Open", "new", cis=["DISPLAY-SW"], product="maps", fix=["2027.Q1-b2"]),
    issue("PRG-11", "Discrepancy", "Open", "new", product="maps", fix=["2027.Q1-b2"]),         # no CSC tickets yet
    issue("PRG-12", "Feature", "Open", "new", product="display", fix=["2027.Q1-b2"]),          # not our CSC
    issue("PRG-13", "Feature", "Open", "new", product="maps", fix=["2027.Q2-b1"]),             # another version
    issue("PRG-14", "Task", "Open", "new", product="maps", fix=["2027.Q1-b2"]),                # not a top-level type
    issue("NAVL-1", "Story", "In Review", parent="PRG-1", product="core", fix=["2027.Q1-b1"]),
    issue("NAVL-2", "Story", "In Progress", product="core", fix=["2027.Q1-b1"]),               # no parent
    issue("NAVL-3", "Story", "Closed", "done", parent="PRG-1", product="core"),               # closed, no fix version
    issue("NAVL-4", "Bug", "Done", "done", parent="PRG-10", product="core", fix=["2027.Q1-b2"]),
    issue("NAVX-1", "Story", "Awaiting CCB", parent="PRG-1", product="maps", fix=["2027.Q1-b2"]),
    issue("NAVX-2", "Story", "Done", "done", parent="PRG-1", product="other", fix=["2027.Q1-b2"]),   # not our CSC
    issue("NAVX-3", "Story", "Done", "done", parent="PRG-10", product=["other", "maps"], fix=["2027.Q1-b2"]),  # multi
    issue("NAVL-5", "Story", "Done", "done", parent="PRG-10", product=["core", "nav2"], fix=["2027.Q1-b2"]),   # 2 CSCs
    issue("NAVX-4", "Story", "Done", "done", parent="PRG-10", product=[], fix=["2027.Q1-b2"]),     # multi, empty
    issue("NAVX-5", "Story", "In Test", parent="PRG-1", product="maps", fix=["2027.Q1-b2"], summary="VER: PRG-1"),
]


class FakeJira(BaseHTTPRequestHandler):
    searches, headers, issues = [], [], ISSUES

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
        page = FakeJira.issues[q["startAt"]:q["startAt"] + q["maxResults"]]
        self._send({"total": len(FakeJira.issues), "issues": [{"key": i["key"], "fields": {f: i["fields"].get(f) for f in q["fields"]}}
                                                     for i in page]})


class JiraTicketSourceTests(unittest.TestCase):
    def setUp(self):
        FakeJira.searches, FakeJira.issues = [], ISSUES
        self.server = HTTPServer(("127.0.0.1", 0), FakeJira)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.client = JiraClient(f"http://127.0.0.1:{self.server.server_port}", token="t", page_size=3)
        self.source = JiraTicketSource(self.client, ["PRG"], top_types=["Feature", "Discrepancy"],
                                       fields={"parent": "Parent Ticket", "affected_product": "affected product",
                                               "cis": "Affected CIs"},
                                       status_map={"Awaiting CCB": "blocked"}, roles={"verification": {"summary": "VER:"}})

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
        keyed = [q for q in FakeJira.searches if q["jql"].startswith("key in (")]
        self.assertEqual(len(keyed), 6)                                                    # 17 issues, pages of 3
        self.assertEqual(FakeJira.searches[-1]["jql"], "(" + " OR ".join(f'cf[10500] ~ "{k}"' for k in sorted(
            i["key"] for i in ISSUES if i["key"].startswith("PRG-"))) + ") ORDER BY key")     # + one rollup query
        self.assertIn("customfield_10500", FakeJira.searches[0]["fields"])
        self.assertNotIn("NOPE-1", by)
        r = by["NAVL-1"]
        self.assertEqual((r.parent_key, r.project, r.affected_product, r.fix_versions, r.state, r.status, r.assignee),
                         ("PRG-1", "NAVL", "core", ["2027.Q1-b1"], "peer_review", "In Review", "J. Doe"))
        self.assertEqual((by["PRG-1"].project, by["PRG-1"].parent_key, by["PRG-1"].cis), (None, None, ["NAV-SW"]))
        # rolled up: its CSC tickets are in work, and one of them is in error, which wins
        self.assertEqual((by["PRG-1"].state, by["PRG-1"].state_reason), ("error", "NAVL-3: closed without a fix version"))
        self.assertEqual((by["PRG-10"].state, by["PRG-10"].state_reason), ("error", "NAVX-4: no 'affected product' set"))
        self.assertEqual((by["NAVX-3"].affected_product, by["NAVX-3"].attributes["affected_products"]),
                         ("other", ["other", "maps"]))                                     # multi-select: first, and all
        self.assertTrue(by["PRG-1"].url.endswith("/browse/PRG-1"))
        self.assertEqual(by["NAVX-1"].state, "blocked")                                     # status_map from config
        self.assertEqual((by["NAVL-2"].state, by["NAVL-2"].state_reason), ("error", "no parent ticket in 'Parent Ticket'"))
        self.assertEqual(by["NAVL-3"].state_reason, "closed without a fix version")
        self.assertIn("isn't a top-level type", by["PRG-3"].state_reason)

    def test_queries(self):
        cscs = [{"name": "nav-core", "jira_project": "NAVL", "affected_product": "core"},
                {"name": "nav-maps", "jira_project": "NAVX", "affected_product": "maps"}]
        found = self.source.tickets_for_versions({"name": "NAV-SW"}, cscs, ["2027.Q1-b2"])
        self.assertEqual(sorted(r.key for r in found), ["NAVL-4", "NAVL-5", "NAVX-1", "NAVX-3"])   # not NAVX-2, nor
                                                                                       # NAVX-5 (verification)
        self.assertEqual(next(r for r in found if r.key == "NAVX-3").affected_product, "maps")   # the one CSC asked for
        self.assertEqual(FakeJira.searches[-1]["jql"], 'project in ("NAVL", "NAVX") AND (fixVersion in ("2027.Q1-b2") '
                                                       'AND cf[10600] in ("core", "maps")) ORDER BY key')
        cscs.append({"name": "nav-two", "jira_project": "NAVL", "affected_product": "nav2"})
        found = self.source.tickets_for_versions({"name": "NAV-SW"}, cscs, ["2027.Q1-b2"])
        self.assertEqual(sorted(r.affected_product for r in found if r.key == "NAVL-5"), ["core", "nav2"])   # both
        self.assertEqual([r.key for r in self.source.get_children("PRG-1")], ["NAVL-1", "NAVL-3", "NAVX-1", "NAVX-2", "NAVX-5"])
        self.assertEqual(FakeJira.searches[-1]["jql"], '(cf[10500] ~ "PRG-1") ORDER BY key')  # exact match: not PRG-10's
        self.assertEqual([r.key for r in self.source.top_level_tickets({}, [{"name": "NAV-SW"}])], ["PRG-1"])
        self.assertEqual([r.key for r in self.source.query(["NAVX"])], ["NAVX-1", "NAVX-2", "NAVX-3", "NAVX-4", "NAVX-5"])

    def test_version_scope(self):
        cscs = [{"name": "nav-core", "jira_project": "NAVL", "affected_product": "core"},
                {"name": "nav-maps", "jira_project": "NAVX", "affected_product": "maps"}]
        rec = next(iter(self.source.get_parents(["PRG-1"], cscs)))
        self.assertEqual((rec.state, rec.state_reason), ("error", "NAVL-3: closed without a fix version"))
        seen, rollup = [], self.source.rollup
        self.source.rollup = lambda t, own, kids: seen.append((t.versions, [c.key for c in kids])) or rollup(t, own, kids)
        rec = next(iter(self.source.get_parents(["PRG-1"], cscs, ["2027.Q1-b1"])))
        # not NAVX-1 (b2); NAVL-3 has no fix version, so it stays and puts the parent in error; NAVX-5 is
        # verification, which a scope never filters out
        self.assertEqual(seen, [(["2027.Q1-b1"], ["NAVL-1", "NAVL-3", "NAVX-5"])])
        self.assertEqual((rec.state, rec.state_reason), ("error", "NAVL-3: no fix version"))
        self.assertEqual([r.key for r in self.source.children_of(["PRG-1"], versions=["2027.Q1-b2"])],
                         ["NAVL-3", "NAVX-1", "NAVX-2", "NAVX-5"])
        self.source.roles = {"verification": {"summary": "VER:"}, "analysis": {"summary": "Closed"}}
        self.source.rollup = rollup
        FakeJira.issues = [dict(i, fields=dict(i["fields"], summary="Closed analysis")) if i["key"] == "NAVL-3" else i
                           for i in ISSUES]
        rec = next(iter(self.source.get_parents(["PRG-1"], cscs, ["2027.Q1-b1"])))
        self.assertEqual(rec.state, "peer_review")                # an analysis ticket needs no fix version

    def test_top_level_tickets_for_versions(self):
        maps = [{"name": "nav-maps", "jira_project": "NAVX", "affected_product": "maps"}]
        tops = self.source.top_level_tickets_for_versions({"name": "NAV-SW"}, maps, ["2027.Q1-b2"])
        self.assertEqual([(r.key, r.state, r.state_reason) for r in tops],
                         [("PRG-1", "blocked", None),      # rolled up over NAVX-1 only: not NAVL-3 (core) or NAVX-2
                          ("PRG-10", "done", None),        # NAVX-3 only: not NAVX-4 (no product) or the NAVL ones
                          ("PRG-11", "analysis_required", None)])   # found without any CSC ticket
        self.assertEqual(FakeJira.searches[0]["jql"], 'project in ("PRG") AND (fixVersion in ("2027.Q1-b2") AND issuetype '
                                                      'in ("Discrepancy", "Feature") AND cf[10600] in ("maps")) ORDER BY key')
        by = {r.key: r for r in self.source.get_tickets(["PRG-1", "PRG-10"])}             # no scope: every CSC ticket
        self.assertEqual((by["PRG-1"].state, by["PRG-10"].state), ("error", "error"))
        self.assertEqual(self.source.top_level_tickets_for_versions({"name": "NAV-SW"}, maps, ["1999.Q1"]), [])

    def test_unlinked_csc_tickets_for_versions(self):
        core = [{"name": "nav-core", "jira_project": "NAVL", "affected_product": "core"}]
        found = self.source.top_level_tickets_for_versions({"name": "NAV-SW"}, core, ["2027.Q1-b1"])
        # no top-level ticket is fixed in b1, but two core tickets are: NAVL-1 (parent PRG-1 is b2) and NAVL-2 (none)
        self.assertEqual([(r.key, r.state, r.state_reason, r.attributes.get("unlinked")) for r in found],
                         [("NAVL-1", "error", "parent PRG-1 isn't a top-level ticket for these versions", True),
                          ("NAVL-2", "error", "not linked to a top-level ticket", True)])
        found = self.source.top_level_tickets_for_versions({"name": "NAV-SW"}, core, ["2027.Q1-b2"])
        self.assertEqual([r.key for r in found], ["PRG-1", "NAVL-4", "NAVL-5"])   # PRG-10 is maps: its core tickets
        self.assertEqual([r.key for r in found if r.attributes.get("unlinked")], ["NAVL-4", "NAVL-5"])   # show apart
        found = self.source.top_level_tickets_for_versions({"name": "NAV-SW"}, core, ["2027.Q1-b1"], unlinked_error=False)
        self.assertEqual([(r.key, r.state, r.state_reason) for r in found],
                         [("NAVL-1", "peer_review", None), ("NAVL-2", "error", "no parent ticket in 'Parent Ticket'")])
                                                                   # their own states (no parent is its own error)
        self.source.unlinked_error = False                         # the source-wide setting ("unlinked_error" in config)
        self.assertEqual(self.source.top_level_tickets_for_versions({"name": "NAV-SW"}, core, ["2027.Q1-b1"])[0].state,
                         "peer_review")

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
            self.assertEqual(groups["PRG-10"], ["NAVL-4", "NAVL-5", "NAVX-3"])               # NAVX-3 via its 2nd product
            call("post", "/cis/NAV-SW/cscs", {"name": "nav-two", "jira_project": "NAVL", "affected_product": "nav2"})
            work = call("get", "/cis/NAV-SW/work?versions=2027.Q1-b2")
            prg10 = next(i for i in work["items"] if i["parent"]["key"] == "PRG-10")
            self.assertEqual({g["csc"]: [t["key"] for t in g["tickets"]] for g in prg10["cscs"]},
                             {"nav-core": ["NAVL-4", "NAVL-5"], "nav-two": ["NAVL-5"], "nav-maps": ["NAVX-3"]})
            self.assertEqual(prg10["parent"]["state"], "done")         # judged over this CI's CSCs: NAVX-4 isn't one
            self.assertEqual(call("get", "/tickets/PRG-10")["state"], "error")   # the ticket page sees them all
            prg1 = next(i for i in work["items"] if i["parent"]["key"] == "PRG-1")
            self.assertEqual(prg1["parent"]["attributes"]["verification"], {"state": "verification", "done": 0, "total": 1})
            t = call("get", "/tickets/PRG-1")
            self.assertEqual(t["summary"], "PRG-1 summary")
            self.assertEqual([c["key"] for c in t["verification"]["tickets"]], ["NAVX-5"])    # apart from the CSC work
            self.assertNotIn("NAVX-5", [c["key"] for g in t["children"] for c in g["tickets"]])
            self.assertIn("Verified 0 of 1", c.get("/tickets/PRG-1").get_data(as_text=True))
            call("post", "/backlogs", {"name": "Nav", "cis": ["NAV-SW"]})
            self.assertEqual(call("post", "/backlogs/Nav/pull")["added"], ["PRG-1"])       # open, affects NAV-SW
        finally:
            shutil.rmtree(tmp)


RULE_ISSUES = [
    issue("PRG-40", "Feature", "Open", "new", analysis="Ready for Work"),                # analysis done, no work yet
    issue("NAVL-40", "Story", "Done", "done", parent="PRG-40", product="core", labels=["Analysis"]),   # no fix version
    issue("PRG-41", "Feature", "Open", "new", analysis="Ready for Work"),                # one CSC ticket in work
    issue("NAVL-41", "Story", "Done", "done", parent="PRG-41", product="core", labels=["analysis"]),
    issue("NAVL-42", "Story", "In Work", parent="PRG-41", product="core"),
    issue("NAVX-42", "Story", "Open", "new", parent="PRG-41", product="maps"),
    issue("PRG-43", "Feature", "Open", "new", analysis="In Analysis"),
    issue("NAVL-43", "Story", "In Progress", parent="PRG-43", product="core", labels=["Analysis"]),
    issue("PRG-44", "Feature", "Closed", "done", analysis="Ready for Work"),             # Jira closed it
    issue("PRG-45", "Feature", "Open", "new", analysis="Waiting"),                       # not a known value
    issue("PRG-46", "Feature", "Open", "new"),                                            # no analysis state: status
    issue("PRG-47", "Feature", "Open", "new", analysis="Ready for Work"),                # work done, verification open
    issue("NAVL-47", "Story", "Done", "done", parent="PRG-47", product="core", fix=["2027.Q1-b1"]),
    issue("NAVL-48", "Story", "In Work", parent="PRG-47", summary="VER: verify PRG-47"),   # verification: no product
    issue("NAVX-48", "Story", "Done", "done", parent="PRG-47", summary="VER: maps"),
    issue("PRG-49", "Feature", "Open", "new", analysis="Ready for Work"),                # work done, analysis reopened
    issue("NAVL-49", "Story", "Done", "done", parent="PRG-49", product="core", fix=["2027.Q1-b1"]),
    issue("NAVL-50", "Story", "In Progress", parent="PRG-49", product="core", labels=["Analysis"]),
    issue("PRG-51", "Feature", "Open", "new", analysis="Ready for Work"),                # work in progress, analysis too
    issue("NAVL-51", "Story", "In Progress", parent="PRG-51", product="core", labels=["Analysis"]),
    issue("NAVL-52", "Story", "In Work", parent="PRG-51", product="core"),
    issue("PRG-53", "Feature", "Open", "new", analysis="Ready for Work"),                # analysis restarted, no work yet
    issue("NAVL-53", "Story", "In Progress", parent="PRG-53", product="core", labels=["Analysis"]),
    issue("NAVL-54", "Story", "Open", "new", parent="PRG-53", product="core"),
    issue("PRG-55", "Feature", "Open", "new", analysis="Ready for Work"),
    issue("NAVL-55", "Story", "In Work", parent="PRG-55", summary="SKIP: housekeeping"),  # ignored
]
ROLES = {"analysis": {"label": "Analysis"}, "verification": {"summary": "VER:"}, "ignore": {"summary": "SKIP:"}}


class StateRuleTests(unittest.TestCase):
    def setUp(self):
        FakeJira.searches, FakeJira.issues = [], RULE_ISSUES
        self.server = HTTPServer(("127.0.0.1", 0), FakeJira)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.fields = {"parent": "Parent Ticket", "affected_product": "Affected Product", "analysis_state": "Analysis State"}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        FakeJira.issues = ISSUES

    def states(self, source):
        return {r.key: r.state for r in source.get_tickets([i["key"] for i in RULE_ISSUES])}

    def test_analysis_rule_and_rollup(self):
        source = JiraTicketSource(JiraClient(self.url, token="t"), ["PRG"], self.fields, state_rule=analysis_rule,
                                  roles=ROLES)
        recs = {r.key: r for r in source.get_tickets([i["key"] for i in RULE_ISSUES])}
        got = {k: r.state for k, r in recs.items()}
        self.assertEqual({k: got[k] for k in ("NAVL-40", "NAVL-41", "NAVL-42", "NAVX-42", "NAVL-43")},
                         {"NAVL-40": "done", "NAVL-41": "done",                     # analysis done: done, no fix version needed
                          "NAVL-42": "in_progress", "NAVX-42": "analysis_required", "NAVL-43": "in_analysis"})
        self.assertEqual(recs["NAVL-40"].role, "analysis")
        self.assertEqual(got["PRG-40"], "ready_for_work")        # the Analysis State field; no work started
        self.assertEqual(got["PRG-41"], "in_progress")           # one work ticket in work
        self.assertEqual(got["PRG-43"], "in_analysis")           # an analysis ticket active, no work
        self.assertEqual(got["PRG-44"], "done")                  # closed in Jira wins over the field
        self.assertEqual(got["PRG-45"], "error")
        self.assertIn("Waiting", recs["PRG-45"].state_reason)
        self.assertEqual(got["PRG-46"], "analysis_required")     # no field: the Jira status
        self.assertEqual((recs["PRG-49"].state, recs["PRG-49"].state_reason), ("in_progress", "NAVL-50: analysis reopened"))
        self.assertEqual(got["PRG-51"], "in_progress")           # work in progress: analysis doesn't pull it back
        self.assertEqual(got["PRG-53"], "in_analysis")           # analysis restarted before any work started

    def test_verification(self):
        source = JiraTicketSource(JiraClient(self.url, token="t"), ["PRG"], self.fields, state_rule=analysis_rule,
                                  roles=ROLES)
        recs = {r.key: r for r in source.get_tickets(["PRG-47", "NAVL-48", "NAVX-48", "PRG-41"])}
        self.assertEqual((recs["NAVL-48"].role, recs["NAVL-48"].state), ("verification", "in_progress"))   # no product: fine
        self.assertEqual(recs["NAVX-48"].state, "done")                                  # no fix version: fine
        self.assertEqual(recs["PRG-47"].state, "done")                                   # all work done...
        self.assertEqual(recs["PRG-47"].attributes["verification"], {"state": "in_progress", "done": 1, "total": 2})
        self.assertNotIn("verification", recs["PRG-41"].attributes)                      # ...no verification tickets
        maps = [{"name": "nav-maps", "jira_project": "NAVX", "affected_product": "maps"}]
        scoped = next(iter(source.get_parents(["PRG-47"], maps)))
        self.assertEqual(scoped.attributes["verification"]["total"], 2)                 # scope doesn't filter verification
        self.assertEqual(scoped.state, "ready_for_work")                                  # no maps work: its own state

    def test_roles(self):
        source = JiraTicketSource(JiraClient(self.url, token="t"), ["PRG"], self.fields, state_rule=analysis_rule,
                                  roles=ROLES)
        recs = {r.key: r for r in source.get_tickets(["NAVL-55", "PRG-55"])}
        self.assertEqual((recs["NAVL-55"].state, recs["NAVL-55"].role), ("ignored", "work"))
        self.assertEqual(recs["PRG-55"].state, "ready_for_work")                         # in work, but ignored
        source.roles = lambda t: "verification" if t.summary.startswith("VER") else None   # a function works too
        self.assertEqual(next(iter(source.get_tickets(["NAVL-48"]))).role, "verification")
        source.roles = {}                                                                 # no roles: all work
        got = self.states(source)
        self.assertEqual((got["NAVL-48"], got["PRG-47"]), ("error", "error"))          # a work ticket with no product

    def test_registering_rules(self):
        source = JiraTicketSource(JiraClient(self.url, token="t"), ["PRG"], self.fields)
        got = self.states(source)                                 # defaults: status rule, work rollup, "Analysis" label
        self.assertEqual((got["PRG-46"], got["PRG-40"], got["NAVL-40"]), ("analysis_required",) * 2 + ("done",))
        source.state_rule = lambda t: ("blocked", "CCB") if t.role == "analysis" else None   # None: status_rule
        got = self.states(source)
        self.assertEqual((got["NAVL-41"], got["NAVL-42"]), ("blocked", "in_progress"))
        source.rollup = None                                      # no rollup: parents keep their own state
        self.assertEqual(self.states(source)["PRG-41"], "analysis_required")
        source.rollup = lambda t, own, children: ("verification", f"{len(children)} children")
        rec = next(r for r in source.get_tickets(["PRG-41"]))
        self.assertEqual((rec.state, rec.state_reason), ("verification", "3 children"))

    def test_scope(self):
        source = JiraTicketSource(JiraClient(self.url, token="t"), ["PRG"], self.fields, state_rule=analysis_rule)
        maps = [{"name": "nav-maps", "jira_project": "NAVX", "affected_product": "maps"}]
        rec = next(iter(source.get_parents(["PRG-41"], maps)))
        self.assertEqual(rec.state, "ready_for_work")             # NAVL-42 (core, in work) is out of scope
        seen = []
        source.state_rule = lambda t: seen.append((t.key, tuple(c["name"] for c in t.cscs), t.scope is maps)) or None
        source.get_parents(["PRG-41"], maps)
        family = sorted({s for s in seen if s[0] in ("PRG-41", "NAVL-41", "NAVL-42", "NAVX-42")})   # the fake ignores JQL
        self.assertEqual(family, [("NAVL-41", (), True), ("NAVL-42", (), True), ("NAVX-42", ("nav-maps",), True),
                                  ("PRG-41", (), True)])

    def test_rules_from_config(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "jira.json")
            with open(path, "w") as f:
                json.dump({"top_projects": ["PRG"], "fields": self.fields, "roles": ROLES,
                           "state_rule": "cmtrack.jira_tickets:analysis_rule"}, f)
            env = {"CMTRACK_JIRA_URL": self.url, "CMTRACK_JIRA_TOKEN": "t", "CMTRACK_JIRA_CONFIG": path}
            old = {k: os.environ.get(k) for k in env}
            os.environ.update(env)
            try:
                source = from_env()
            finally:
                for k, v in old.items():
                    os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
            self.assertIs(source.state_rule, analysis_rule)
            self.assertEqual(self.states(source)["PRG-41"], "in_progress")
            self.assertEqual(source.get_tickets(["NAVL-48"])[0].role, "verification")
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()

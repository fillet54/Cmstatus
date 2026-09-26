"""Version lineage (DAG), ticket ingestion and work-item reports.  Run: python -m unittest discover -s tests"""
import os
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest

from cmtrack import create_app
from cmtrack.demo import DEMO_TICKETS, seed
from cmtrack.tickets import STATES, StaticSource, TicketRecord, load_sources, normalize_state, rollup



class WorkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.source = StaticSource(DEMO_TICKETS, name="jira")
        self.app = create_app({"DATABASE": os.path.join(self.tmp, "t.db"),
                               "TICKET_SOURCES": {"jira": self.source}})
        self.c = self.app.test_client()
        seed(self.c)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def call(self, method, path, json=None, status=200):
        r = getattr(self.c, method)("/api" + path, json=json)
        self.assertEqual(r.status_code, status, r.get_json())
        return r.get_json()

    def vid(self, name, ci="NAV-SW"):
        return self.call("get", f"/cis/{ci}/versions?to={name}")[-1]["id"]

    def names(self, to, frm=None, ci="NAV-SW"):
        q = f"/cis/{ci}/versions?to={to}" + (f"&from={frm}" if frm else "")
        return [v["name"] for v in self.call("get", q)]

    # ------------------------------------------------------------------ lineage

    def test_auto_lineage(self):
        # chain inside a release, rejected b3 included (b4 was built on it), then the next quarter
        self.assertEqual(self.names("2026.Q4-b4"), ["2026.Q4-b1", "2026.Q4-b2", "2026.Q4-b3", "2026.Q4-b4"])
        # a patch builds on its base version (the line's effective version when the sync first could tell)
        p1 = self.call("get", f"/versions/{self.vid('2026.Q4.P1')}/lineage")
        self.assertEqual([p["name"] for p in p1["parents"]], ["2026.Q4-b4"])
        # Q2 builds on Q1's head (its last build, since Q1 hasn't shipped)
        q2 = self.call("get", f"/versions/{self.vid('2027.Q2-b1')}/lineage")
        self.assertEqual([p["name"] for p in q2["parents"]], ["2027.Q1-b3"])
        self.assertEqual(q2["lineage"], "auto")

    def test_merge_range_and_reset(self):
        q1b1 = self.vid("2027.Q1-b1")
        self.assertEqual(self.call("get", f"/versions/{q1b1}/lineage")["lineage"], "manual")   # demo merged ER1
        self.assertEqual(self.names("2027.Q1-b2", "2026.Q4-b4"), ["2026.Q4.ER1", "2027.Q1-b1", "2027.Q1-b2"])
        self.assertEqual(self.names("2027.Q1-b2", "2026.Q4.ER1"), ["2027.Q1-b1", "2027.Q1-b2"])
        q1 = next(r for r in self.call("get", "/cis/NAV-SW/releases") if r["name"] == "2027.Q1")
        self.assertEqual(self.call("get", f"/releases/{q1['id']}")["unabsorbed"], [])

        # back to automatic: ER1 drops out of Q1's lineage and is flagged as not absorbed
        lin = self.call("delete", f"/versions/{q1b1}/parents")
        self.assertEqual((lin["lineage"], [p["name"] for p in lin["parents"]]), ("auto", ["2026.Q4-b4"]))
        self.assertEqual(self.names("2027.Q1-b2", "2026.Q4-b4"), ["2027.Q1-b1", "2027.Q1-b2"])
        unabsorbed = self.call("get", f"/releases/{q1['id']}")["unabsorbed"]
        self.assertEqual([(u["name"], u["reason"]) for u in unabsorbed], [("2026.Q4.ER1", "CR-1234")])
        q2 = next(r for r in self.call("get", "/cis/NAV-SW/releases") if r["name"] == "2027.Q2")
        self.assertEqual(len(self.call("get", f"/releases/{q2['id']}")["unabsorbed"]), 1)

    def test_parents_validation(self):
        b1 = self.vid("2026.Q4-b1")
        err = self.call("put", f"/versions/{b1}/parents", {"parents": ["2027.Q1-b2"]}, 400)   # Q1-b2 descends from b1
        self.assertIn("its own ancestor", err["error"])
        self.call("put", f"/versions/{b1}/parents", {"parents": ["3.2.0"]}, 404)             # other CI's version
        self.call("put", f"/versions/{b1}/parents", {}, 400)
        self.call("get", "/cis/NAV-SW/versions", status=400)                                   # 'to' required

    def test_lineage_survives_resync(self):
        # adding an ad hoc build to Q1 moves Q2's first parent to it; the manual merge on Q1-b1 is kept
        q1 = next(r for r in self.call("get", "/cis/NAV-SW/releases") if r["name"] == "2027.Q1")
        self.call("post", f"/releases/{q1['id']}/versions", {}, 201)
        q2 = self.call("get", f"/versions/{self.vid('2027.Q2-b1')}/lineage")
        self.assertEqual([p["name"] for p in q2["parents"]], ["2027.Q1-b4"])
        self.assertEqual(len(self.call("get", f"/versions/{self.vid('2027.Q1-b1')}/lineage")["parents"]), 2)
        s = self.call("post", "/cis/NAV-SW/sync")
        self.assertEqual((s["created"], s["updated"], s["missing"]), ([], [], []))
        self.assertEqual(len(self.call("get", f"/versions/{self.vid('2027.Q1-b1')}/lineage")["parents"]), 2)
        self.assertEqual(self.call("get", f"/versions/{self.vid('2027.Q2-b1')}/lineage")["parents"][0]["name"],
                         "2027.Q1-b4")

    # ------------------------------------------------------------------ tickets

    def test_work_report_groups_parent_csc_ticket(self):
        w = self.call("get", "/cis/NAV-SW/work?from=2026.Q4-b2&to=2027.Q1-b2")
        p = w["progress"]
        self.assertEqual(list(p), list(STATES) + ["total"])                    # every state, workflow order
        self.assertEqual({k: v for k, v in p.items() if v},
                         {"analysis_required": 1, "in_progress": 1, "peer_review": 1, "blocked": 1,
                          "verification": 2, "done": 2, "total": 8})
        by_parent = {(i["parent"] or {}).get("key"): i for i in w["items"]}
        self.assertEqual(list(by_parent), ["PRG-10", "PRG-12", "PRG-15", "PRG-18", None])   # orphans last
        blocked = next(t for g in by_parent["PRG-10"]["cscs"] for t in g["tickets"] if t["key"] == "NAVL-106")
        self.assertEqual((blocked["state"], blocked["status"]), ("blocked", "Ready to Merge"))
        self.assertIn("NAVX-205", blocked["state_reason"])
        self.assertEqual([(g["csc"], [t["key"] for t in g["tickets"]]) for g in by_parent["PRG-12"]["cscs"]],
                         [("nav-maps", ["NAVX-210", "NAVX-211"])])
        # explicit version list instead of a range
        w = self.call("get", "/cis/NAV-SW/work?versions=2026.Q4-b1,2026.Q4.ER1")
        self.assertEqual(sorted(t["key"] for i in w["items"] for g in i["cscs"] for t in g["tickets"]),
                         ["NAVL-101", "NAVL-120"])
        self.call("get", "/cis/NAV-SW/work", status=400)

    def test_ticket_detail_spans_cis(self):
        t = self.call("get", "/tickets/PRG-10")
        self.assertEqual([(g["ci"], g["csc"]) for g in t["children"]],
                         [("DISPLAY-SW", "hud"), ("NAV-SW", "nav-core"), ("NAV-SW", "nav-maps")])
        self.assertEqual((t["progress"]["done"], t["progress"]["total"]), (3, 6))
        self.assertEqual(t["state"], "in_progress")
        child = self.call("get", "/tickets/NAVL-105")
        self.assertEqual((child["parent"]["key"], child["csc"], [v["name"] for v in child["versions"]]),
                         ("PRG-10", "nav-core", ["2027.Q1-b1"]))
        self.call("get", "/tickets/NOPE-1", status=404)

    def test_source_is_definitive(self):
        """Nothing is stored: every request asks the source, so a change there shows up immediately."""
        calls = []
        orig = self.source.tickets_for_versions
        self.source.tickets_for_versions = lambda *a: calls.append(a[2]) or orig(*a)
        rng = "/cis/NAV-SW/work?from=2026.Q4-b4&to=2027.Q1-b2"
        state = lambda: next(t["state"] for i in self.call("get", rng)["items"] for g in i["cscs"]
                             for t in g["tickets"] if t["key"] == "NAVL-106")
        self.assertEqual(state(), "blocked")
        next(r for r in self.source.records if r.key == "NAVL-106").state = "verification"
        self.assertEqual(state(), "verification")
        self.assertEqual(calls, [["2026.Q4.ER1", "2027.Q1-b1", "2027.Q1-b2"]] * 2)   # the range, asked twice
        with sqlite3.connect(os.path.join(self.tmp, "t.db")) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertFalse({"ticket", "ticket_version"} & tables)

    def test_records_that_dont_fit(self):
        self.source.records += [
            TicketRecord(key="NAVL-300", parent_key="PRG-99", project="NAVL", affected_product="core",
                         fix_versions=["2027.Q1-b3", "nope"], state="Peer Review"),
            TicketRecord(key="NAVX-301", project="NAVX", affected_product="maps", fix_versions=["2027.Q1-b3"],
                         state="blocked-ish", state_reason="two open sub-tasks"),
        ]
        w = self.call("get", "/cis/NAV-SW/work?versions=2027.Q1-b3")
        tickets = {t["key"]: t for i in w["items"] for g in i["cscs"] for t in g["tickets"]}
        self.assertEqual((tickets["NAVL-300"]["state"], [v["name"] for v in tickets["NAVL-300"]["versions"]]),
                         ("peer_review", ["2027.Q1-b3"]))                    # only fix versions in the set
        self.assertEqual((tickets["NAVX-301"]["state"], tickets["NAVX-301"]["state_reason"]),
                         ("error", "source sent unknown state 'blocked-ish'; two open sub-tasks"))
        parent = next(i["parent"] for i in w["items"] if i["parent"] and i["parent"]["key"] == "PRG-99")
        self.assertEqual((parent["state"], parent["state_reason"]), ("error", "parent ticket not found in the source"))

        detail = self.call("get", "/tickets/NAVL-300")
        self.assertEqual([(v["name"], v["id"] is None) for v in detail["versions"]], [("2027.Q1-b3", False), ("nope", True)])
        self.assertIn("NAVL-300: fix version 'nope' is not a version of NAV-SW", detail["warnings"])

        # a source that answers with tickets outside the request is reported, not trusted
        self.source.records.append(TicketRecord(key="ZZZ-1", project="ZZZ", affected_product="x",
                                                fix_versions=["2027.Q1-b3"], state="done"))
        orig = self.source.tickets_for_versions
        self.source.tickets_for_versions = lambda *a: orig(*a) + [self.source.records[-1]]
        w = self.call("get", "/cis/NAV-SW/work?versions=2027.Q1-b3")
        self.assertIn("ZZZ-1: no CSC mapped to (ZZZ, x)", w["warnings"])

    def test_source_failures(self):
        def boom(*a):
            raise ConnectionError("jira is down")
        self.source.tickets_for_versions = boom
        err = self.call("get", "/cis/NAV-SW/work?to=2027.Q1-b1", status=502)
        self.assertIn("jira is down", err["error"])
        page = self.c.get("/cis/NAV-SW/work?to=2027.Q1-b1")
        self.assertEqual(page.status_code, 200)                              # the page still renders
        self.assertIn("Couldn't get tickets", page.get_data(as_text=True))
        version = self.c.get(f"/versions/{self.vid('2027.Q1-b1')}")
        self.assertEqual(version.status_code, 200)
        self.assertIn("jira is down", version.get_data(as_text=True))
        self.call("get", "/tickets/PRG-10?source=nope", status=400)

    def test_no_source_configured(self):
        app = create_app({"DATABASE": os.path.join(self.tmp, "t.db"), "TICKET_SOURCES": {}})
        c = app.test_client()
        r = c.get("/api/cis/NAV-SW/work?to=2027.Q1-b1")
        self.assertEqual((r.status_code, r.get_json()["error"]),
                         (400, "no ticket source configured (set CMTRACK_TICKET_SOURCES)"))
        self.assertIn("no ticket source configured", c.get("/cis/NAV-SW/work?to=2027.Q1-b1").get_data(as_text=True))

    # ------------------------------------------------------------------ views

    def test_views(self):
        page = self.c.get("/cis/NAV-SW/work?from=2026.Q4-b4&to=2027.Q1-b2").get_data(as_text=True)
        self.assertIn("<html", page)
        self.assertIn("NAVL-120", page)                          # merged emergency fix is in the range
        frag = self.c.get("/cis/NAV-SW/work?from=2026.Q4.ER1&to=2027.Q1-b2",
                          headers={"HX-Request": "true"}).get_data(as_text=True)
        self.assertNotIn("<html", frag)
        self.assertNotIn("NAVL-120", frag)
        page = self.c.get("/cis/NAV-SW/work").get_data(as_text=True)
        self.assertNotIn("What's new in", page)
        self.assertIn('<optgroup label="HSCMs (the version they list)">', page)
        self.assertIn("data-picker", page)
        by_hscm = self.c.get("/cis/NAV-SW/work?from=hscm:IFC-1/Build 1&to=release:2027.Q1").get_data(as_text=True)
        by_name = self.c.get("/cis/NAV-SW/work?from=2026.Q4-b4&to=2027.Q1-b2").get_data(as_text=True)
        self.assertIn('value="hscm:IFC-1/Build 1" selected', by_hscm)                # names in the URL...
        self.assertEqual(by_hscm.count("NAVL-"), by_name.count("NAVL-"))            # ...mean the versions they stand for
        self.assertIn("IFC-1 · Build 1</a>", by_hscm)                               # shown as IFC · HSCM › release › version
        self.assertIn(">2027.Q1</a>", by_hscm)
        self.assertIn('id="work-lineage"', by_name)                                 # the range as a timeline:
        self.assertIn(">from b4</text>", by_name)                                   # where it starts, faded...
        self.assertIn(">ER1</text>", by_name)                                       # ...the merged fix...
        self.assertIn('class="ui-tl__note"', by_name)                               # ...and the HSCMs that list them
        frag = self.c.get("/cis/NAV-SW/work/lineage?from=2026.Q4-b4&to=2027.Q1-b2&zoom=weeks&width=900")
        self.assertIn('id="work-lineage"', frag.get_data(as_text=True))
        bad = self.c.get("/cis/NAV-SW/work?to=release:NOPE").get_data(as_text=True)
        self.assertIn("can&#39;t use &#39;release:NOPE&#39;", bad)
        self.assertIn("How each CSC implemented it", self.c.get("/tickets/PRG-10").get_data(as_text=True))
        self.assertIn("NAVX-222 is still open", self.c.get("/tickets/NAVX-221").get_data(as_text=True))
        self.assertEqual([s["state"] for s in self.call("get", "/ticket-states")], list(STATES))
        version = self.c.get(f"/versions/{self.vid('2027.Q1-b1')}").get_data(as_text=True)
        self.assertIn("Built from", version)
        self.assertIn("NAVL-105", version)


class TicketUnitTests(unittest.TestCase):
    def test_rollup(self):
        self.assertEqual(rollup(["done", "done"]), "done")
        self.assertEqual(rollup(["done", "cancelled"]), "done")                         # cancelled ones don't count
        self.assertEqual(rollup(["cancelled", "cancelled"]), "cancelled")
        self.assertEqual(rollup(["verification", "done"]), "verification")              # the least advanced
        self.assertEqual(rollup(["analysis_required", "in_analysis"]), "analysis_required")
        self.assertEqual(rollup(["in_analysis", "peer_review"]), "in_progress")          # work has started
        self.assertEqual(rollup(["done", "blocked", "in_progress"]), "blocked")
        self.assertEqual(rollup([]), None)
        src = StaticSource([{"key": "FEAT-1"}, {"key": "A-1", "parent_key": "FEAT-1", "state": "done"},
                            {"key": "B-1", "parent_key": "FEAT-1", "state": "peer_review"}])
        self.assertEqual(src.get_tickets(["FEAT-1"])[0].state, "peer_review")          # a parent without a state

    def test_normalize_state(self):
        self.assertEqual(normalize_state("Merge Blocked"), ("blocked", None))              # old names still read
        self.assertEqual(normalize_state("Canceled"), ("cancelled", None))
        self.assertEqual(normalize_state("peer-review", "r"), ("peer_review", "r"))
        self.assertEqual(normalize_state(None), ("error", "source did not supply a state"))
        self.assertEqual(normalize_state("error", "linked epic missing"), ("error", "linked epic missing"))
        self.assertEqual(TicketRecord(key="A-1", state="Nope").state, "error")

    def test_load_sources(self):
        mod = types.ModuleType("fake_jira")
        mod.Jira = lambda: StaticSource([])
        sys.modules["fake_jira"] = mod
        try:
            sources = load_sources("jira=fake_jira:Jira")
            self.assertEqual((list(sources), sources["jira"].name), (["jira"], "jira"))
            self.assertEqual(load_sources(""), {})
            with self.assertRaises(ValueError):
                load_sources("jira")
        finally:
            del sys.modules["fake_jira"]


if __name__ == "__main__":
    unittest.main()

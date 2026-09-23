"""Version lineage (DAG), ticket ingestion and work-item reports.  Run: python -m unittest discover -s tests"""
import os
import shutil
import sys
import tempfile
import types
import unittest

from cmtrack import create_app
from cmtrack.demo import DEMO_TICKETS, seed
from cmtrack.tickets import StaticSource, TicketRecord, load_sources, status_category

HERE = os.path.dirname(os.path.abspath(__file__))


class WorkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        shutil.copy(os.path.join(HERE, "..", "policies", "display-sw.txt"), self.tmp)
        self.source = StaticSource(DEMO_TICKETS, name="jira")
        self.app = create_app({"DATABASE": os.path.join(self.tmp, "t.db"), "POLICY_DIR": self.tmp,
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
        # a patch builds on its base version (the family's effective version when it was spawned)
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

    def test_lineage_survives_replanning(self):
        # adding an ad hoc build to Q1 moves Q2's first parent to it; the manual merge on Q1-b1 is kept
        q1 = next(r for r in self.call("get", "/cis/NAV-SW/releases") if r["name"] == "2027.Q1")
        self.call("post", f"/releases/{q1['id']}/versions", {}, 201)
        q2 = self.call("get", f"/versions/{self.vid('2027.Q2-b1')}/lineage")
        self.assertEqual([p["name"] for p in q2["parents"]], ["2027.Q1-b4"])
        self.assertEqual(len(self.call("get", f"/versions/{self.vid('2027.Q1-b1')}/lineage")["parents"]), 2)
        self.call("post", "/cis/NAV-SW/plan", {"start": "2026-10-01", "end": "2027-07-01"})
        self.assertEqual(len(self.call("get", f"/versions/{self.vid('2027.Q1-b1')}/lineage")["parents"]), 2)

    # ------------------------------------------------------------------ tickets

    def test_work_report_groups_parent_csc_ticket(self):
        w = self.call("get", "/cis/NAV-SW/work?from=2026.Q4-b2&to=2027.Q1-b2")
        self.assertEqual(w["progress"], {"todo": 1, "in_progress": 1, "done": 4, "total": 6})
        by_parent = {(i["parent"] or {}).get("key"): i for i in w["items"]}
        self.assertEqual(list(by_parent), ["PRG-10", "PRG-12", "PRG-15", "PRG-18", None])   # orphans last
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
        self.assertEqual((t["progress"]["done"], t["progress"]["total"]), (3, 4))
        child = self.call("get", "/tickets/NAVL-105")
        self.assertEqual((child["parent"]["key"], child["csc"], [v["name"] for v in child["versions"]]),
                         ("PRG-10", "nav-core", ["2027.Q1-b1"]))
        self.call("get", "/tickets/NOPE-1", status=404)

    def test_push_upsert_warnings_and_stubs(self):
        out = self.call("post", "/tickets", {"records": [
            {"key": "NAVL-300", "summary": "new", "parent_key": "PRG-99", "project": "NAVL",
             "affected_product": "core", "fix_versions": ["2027.Q1-b3", "nope"], "status_category": "indeterminate",
             "sprint": "S12"},
            {"key": "ZZZ-1", "project": "ZZZ", "affected_product": "x", "fix_versions": ["2027.Q1-b3"]},
        ]})
        self.assertEqual(out["created"], ["NAVL-300", "ZZZ-1"])
        self.assertEqual(out["missing_parents"], ["PRG-99"])
        self.assertTrue(any("'nope' is not a version of NAV-SW" in w for w in out["warnings"]))
        self.assertTrue(any("no CSC mapped to (ZZZ, x)" in w for w in out["warnings"]))
        t = self.call("get", "/tickets/NAVL-300")
        self.assertEqual((t["status_category"], t["attributes"], [v["name"] for v in t["versions"]]),
                         ("in_progress", {"sprint": "S12"}, ["2027.Q1-b3"]))
        self.assertIsNone(self.call("get", "/tickets/PRG-99")["synced_at"])                  # stub

        # update in place; fix_versions omitted leaves links alone, [] clears them
        out = self.call("post", "/tickets", {"records": [{"key": "NAVL-300", "summary": "renamed",
                                                          "project": "NAVL", "affected_product": "core"}]})
        self.assertEqual((out["created"], out["updated"]), ([], ["NAVL-300"]))
        self.assertEqual(len(self.call("get", "/tickets/NAVL-300")["versions"]), 1)
        self.call("post", "/tickets", {"records": [{"key": "NAVL-300", "project": "NAVL", "affected_product": "core",
                                                    "fix_versions": []}]})
        self.assertEqual(self.call("get", "/tickets/NAVL-300")["versions"], [])
        self.call("post", "/tickets", {"records": [{"summary": "no key"}]}, 400)
        self.call("post", "/tickets", {"records": "nope"}, 400)

    def test_pull_sync_fetches_parents(self):
        self.source.records.append(TicketRecord(key="NAVX-400", summary="pulled", parent_key="PRG-40",
                                                project="NAVX", affected_product="maps", fix_versions=["2027.Q1-b3"]))
        self.source.records.append(TicketRecord(key="PRG-40", summary="Pulled parent", type="Feature"))
        out = self.call("post", "/cis/NAV-SW/tickets/sync", {"from": "2027.Q1-b2", "to": "2027.Q1-b3"})
        self.assertEqual(out["versions"], ["2027.Q1-b3"])
        self.assertIn("NAVX-400", out["created"])
        self.assertIn("PRG-40", out["created"])                 # filled in via fetch_by_keys
        self.assertEqual(out["missing_parents"], [])
        self.assertEqual(self.call("get", "/tickets/PRG-40")["summary"], "Pulled parent")
        out = self.call("post", "/cis/NAV-SW/tickets/sync", {"versions": ["2026.Q4-b1"]})
        self.assertEqual(out["updated"], ["NAVL-101"])
        self.call("post", "/cis/NAV-SW/tickets/sync", {"source": "nope", "to": "2026.Q4-b1"}, 400)

    # ------------------------------------------------------------------ views

    def test_views(self):
        page = self.c.get("/cis/NAV-SW/work?from=2026.Q4-b4&to=2027.Q1-b2").get_data(as_text=True)
        self.assertIn("<html", page)
        self.assertIn("NAVL-120", page)                          # merged emergency fix is in the range
        frag = self.c.get("/cis/NAV-SW/work?from=2026.Q4.ER1&to=2027.Q1-b2",
                          headers={"HX-Request": "true"}).get_data(as_text=True)
        self.assertNotIn("<html", frag)
        self.assertNotIn("NAVL-120", frag)
        self.assertIn("<html", self.c.get("/cis/NAV-SW/work").get_data(as_text=True))
        self.assertIn("How each CSC implemented it", self.c.get("/tickets/PRG-10").get_data(as_text=True))
        version = self.c.get(f"/versions/{self.vid('2027.Q1-b1')}").get_data(as_text=True)
        self.assertIn("Built from", version)
        self.assertIn("NAVL-105", version)


class TicketUnitTests(unittest.TestCase):
    def test_status_category(self):
        self.assertEqual([status_category(x) for x in ("new", "Indeterminate", "DONE", None, "weird")],
                         ["todo", "in_progress", "done", "todo", "todo"])

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

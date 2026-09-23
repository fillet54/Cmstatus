"""End-to-end scenario through the HTTP API.  Run: python -m unittest discover -s tests"""
import os
import shutil
import tempfile
import unittest

from cmtrack import create_app
from cmtrack.policies import CadencePolicy, ManualPolicy, PolicyError
import datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))


class PolicyUnitTests(unittest.TestCase):
    def test_quarterly_monthly(self):
        rels = CadencePolicy({}).plan(dt.date(2026, 10, 1), dt.date(2027, 4, 1))
        self.assertEqual([r.name for r in rels], ["2026.Q4", "2027.Q1"])
        self.assertEqual([v.name for v in rels[0].versions], ["2026.Q4-b1", "2026.Q4-b2", "2026.Q4-b3"])
        self.assertEqual(rels[0].target_date, "2026-12-15")

    def test_fiscal_year_tokens(self):
        rels = CadencePolicy({"anchor_month": 10, "release_format": "FY{fy}Q{quarter}"}).plan(
            dt.date(2026, 11, 5), dt.date(2027, 2, 1))
        self.assertEqual([r.name for r in rels], ["FY2027Q1", "FY2027Q2"])

    def test_manual_parse_errors(self):
        with self.assertRaises(PolicyError):
            ManualPolicy.parse("version 1.0")
        with self.assertRaises(PolicyError):
            ManualPolicy.parse("release 1.0 not-a-date")
        with self.assertRaises(PolicyError):
            CadencePolicy({"release_months": 3, "build_months": 2})
        with self.assertRaises(PolicyError):
            CadencePolicy({"bogus": 1})


class ApiFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        shutil.copy(os.path.join(HERE, "..", "policies", "display-sw.txt"), self.tmp)
        self.app = create_app({"DATABASE": os.path.join(self.tmp, "t.db"), "POLICY_DIR": self.tmp})
        self.c = self.app.test_client()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def call(self, method, path, json=None, status=200, **kw):
        r = getattr(self.c, method)("/api" + path, json=json, **kw)
        self.assertEqual(r.status_code, status, r.get_json())
        return r.get_json()

    def _version(self, release, name):
        return next(v for v in release["versions"] if v["name"] == name)

    def test_full_flow(self):
        # --- policies & CIs
        self.call("post", "/policies", {"name": "quarterly-monthly", "type": "cadence"}, 201)
        self.call("post", "/policies", {"name": "display-manual", "type": "manual",
                                        "params": {"path": "display-sw.txt"}}, 201)
        self.call("post", "/cis", {"name": "NAV-SW", "policy": "quarterly-monthly"}, 201)
        self.call("post", "/cis", {"name": "DISPLAY-SW", "policy": "display-manual"}, 201)
        self.call("post", "/cis/NAV-SW/cscs", {"name": "lib1", "jira_project": "NAVL", "affected_product": "lib1"}, 201)
        self.call("post", "/cis/NAV-SW/cscs", {"name": "lib2", "jira_project": "NAVX", "affected_product": "lib2"}, 201)
        self.call("post", "/cis/DISPLAY-SW/cscs", {"name": "lib1", "jira_project": "NAVL",
                                                   "affected_product": "lib1"}, 409)   # pair already mapped
        self.assertEqual(self.call("get", "/cscs/lookup?project=NAVX&product=lib2")["ci_name"], "NAV-SW")

        # --- planning is idempotent
        s = self.call("post", "/cis/NAV-SW/plan", {"start": "2026-10-01", "end": "2027-04-01"})
        self.assertEqual(s["created_releases"], ["2026.Q4", "2027.Q1"])
        self.assertEqual(len(s["created_versions"]), 6)
        s = self.call("post", "/cis/NAV-SW/plan", {"start": "2026-10-01", "end": "2027-04-01"})
        self.assertEqual(s["created_releases"] + s["created_versions"], [])
        s = self.call("post", "/cis/DISPLAY-SW/plan", {})
        self.assertEqual(s["created_releases"], ["3.2.0", "3.3.0"])

        # --- monthly builds; b3 fails, an unplanned b4 becomes the release
        q4_id = self.call("get", "/cis/NAV-SW/releases")[0]["id"]
        q4 = self.call("get", f"/releases/{q4_id}")
        for name in ("2026.Q4-b1", "2026.Q4-b2", "2026.Q4-b3"):
            self.call("patch", f"/versions/{self._version(q4, name)['id']}", {"status": "built"})
        b3 = self._version(q4, "2026.Q4-b3")["id"]
        err = self.call("post", f"/versions/{b3}/release", status=400)       # policy requires 'tested'
        self.assertIn("must be tested", err["problems"][0])
        self.call("patch", f"/versions/{b3}", {"status": "rejected"})
        b4 = self.call("post", f"/releases/{q4_id}/versions", {}, 201)
        self.assertEqual((b4["name"], b4["planned"]), ("2026.Q4-b4", 0))
        self.call("patch", f"/versions/{b4['id']}", {"status": "built"})
        self.call("patch", f"/versions/{b4['id']}", {"status": "tested"})
        rel = self.call("post", f"/versions/{b4['id']}/release")
        self.assertEqual((rel["status"], rel["released_version_id"]), ("released", b4["id"]))
        summary = self.call("get", "/cis/NAV-SW/releases")[0]
        self.assertEqual((summary["planned_versions"], summary["unplanned_versions"]), (3, 1))

        # --- patch and emergency spawn from the promoted release
        q1_id = self.call("get", "/cis/NAV-SW/releases")[1]["id"]
        err = self.call("post", f"/releases/{q1_id}/spawn", {"kind": "emergency", "reason": "x"}, 400)
        self.assertIn("not been promoted", err["error"])                    # Q1 not released yet
        patch = self.call("post", f"/releases/{q4_id}/spawn", {"kind": "patch"}, 201)
        self.assertEqual((patch["name"], patch["base_version"], patch["parent"]),
                         ("2026.Q4.P1", "2026.Q4-b4", "2026.Q4"))
        self.call("post", f"/releases/{q4_id}/spawn", {"kind": "emergency"}, 400)   # needs reason
        emer = self.call("post", f"/releases/{q4_id}/spawn", {"kind": "emergency", "reason": "CR-1234"}, 201)
        self.assertEqual(emer["name"], "2026.Q4.ER1")

        # duplicate guards: same CR -> same release (200); different CR while ER1 open -> 409
        again = self.call("post", f"/releases/{q4_id}/spawn", {"kind": "emergency", "reason": " CR-1234 "}, 200)
        self.assertEqual((again["id"], again["spawned"]), (emer["id"], False))
        err = self.call("post", f"/releases/{q4_id}/spawn", {"kind": "emergency", "reason": "CR-9999"}, 409)
        self.assertEqual(err["problems"], ["2026.Q4.ER1 (CR-1234)"])

        def ship(vid):
            for status in ("built", "tested"):
                self.call("patch", f"/versions/{vid}", {"status": status})
            self.call("post", f"/versions/{vid}/release")

        e1 = emer["versions"][0]["id"]
        ship(e1)
        q4 = self.call("get", f"/releases/{q4_id}")
        self.assertEqual((q4["released_version"], q4["effective_version"]), ("2026.Q4-b4", "2026.Q4.ER1"))
        self.assertEqual(len(q4["children"]), 2)

        # --- DISPLAY release (manual policy: 'built' is enough)
        d32 = self.call("get", "/cis/DISPLAY-SW/releases")[0]
        d32v = self._version(self.call("get", f"/releases/{d32['id']}"), "3.2.0")["id"]
        self.call("patch", f"/versions/{d32v}", {"status": "built"})
        self.call("post", f"/versions/{d32v}/release")

        # --- composite suite pins NAV + DISPLAY releases
        self.call("post", "/cis", {"name": "SUITE", "kind": "composite", "policy": "quarterly-monthly"}, 201)
        self.call("post", "/cis/SUITE/plan", {"start": "2026-10-01", "end": "2026-12-31"})
        suite_q4 = self.call("get", f"/releases/{self.call('get', '/cis/SUITE/releases')[0]['id']}")
        sv = suite_q4["versions"][-1]["id"]
        self.call("patch", f"/versions/{sv}", {"status": "built"})
        self.call("patch", f"/versions/{sv}", {"status": "tested"})
        self.call("put", f"/versions/{sv}/manifest",
                  {"children": [{"ci": "NAV-SW", "version": "2026.Q4-b1"}, {"ci": "DISPLAY-SW", "version": "3.2.0"}]})
        err = self.call("post", f"/versions/{sv}/release", status=400)       # b1 is only built
        self.assertIn("NAV-SW 2026.Q4-b1", err["problems"][0])
        self.call("put", f"/versions/{sv}/manifest",
                  {"children": [{"ci": "NAV-SW", "version": "2026.Q4.ER1"}, {"ci": "DISPLAY-SW", "version": "3.2.0"}]})
        self.call("post", f"/versions/{sv}/release")

        # --- IFCs: parent/child, scraped HSCM with placeholders
        self.call("post", "/ifcs", {"name": "IFC-2"}, 201)
        self.call("post", "/ifcs", {"name": "IFC-2.1", "parent": "IFC-2"}, 201)
        self.call("patch", "/ifcs/IFC-2", {"parent": "IFC-2.1"}, status=400)        # cycle
        csv_text = "ci,version,type\nNAV-SW,2026.Q4-b4,CSCI\nRADAR-SW,7.4,\nANTENNA,Rev C,HWCI\nNAV-SW,,\n"
        imp = self.call("post", "/ifcs/IFC-2.1/hscm?name=HSCM-A&source_ref=DOC-001", status=201,
                        data=csv_text, content_type="text/csv")
        self.assertEqual(sorted(imp["placeholders_created"]), ["ANTENNA", "RADAR-SW"])
        self.assertEqual(imp["baseline"]["status"], "approved")
        self.assertIn("row 4: missing ci or version; skipped", imp["warnings"])
        self.assertFalse(self.call("get", "/cis/RADAR-SW")["managed"])
        self.assertEqual(self.call("get", "/cis?managed=0")[0]["name"], "ANTENNA")

        # --- manual next baseline: clone, swap NAV to E1, add the suite, approve, diff
        a_id = imp["baseline"]["id"]
        b = self.call("post", f"/baselines/{a_id}/clone", {"name": "HSCM-B"}, 201)
        entries = [{"ci": e["ci"], "version": e["version"]} for e in b["entries"] if e["ci"] != "NAV-SW"]
        entries += [{"ci": "NAV-SW", "version": "2026.Q4.ER1"}, {"ci": "SUITE", "version": str(sv)}]
        self.call("put", f"/baselines/{b['id']}/entries", {"entries": entries})
        self.call("post", f"/baselines/{b['id']}/approve")
        diff = self.call("get", f"/baselines/{a_id}/diff/{b['id']}")
        self.assertEqual(diff["changed"]["NAV-SW"], {"from": "2026.Q4-b4", "to": "2026.Q4.ER1"})
        self.assertIn("SUITE", diff["added"])
        ifc = self.call("get", "/ifcs/IFC-2.1")
        self.assertEqual((ifc["ancestors"], ifc["current_hscm"]["name"]), (["IFC-2"], "HSCM-B"))
        self.assertEqual([x["status"] for x in ifc["baselines"]], ["superseded", "approved"])

        # draft with an unreleased version cannot be approved
        c = self.call("post", "/ifcs/IFC-2.1/baselines",
                      {"name": "HSCM-C", "entries": [{"ci": "NAV-SW", "version": "2026.Q4-b2"}]}, 201)
        self.call("post", f"/baselines/{c['id']}/approve", status=400)

        # --- where-used goes through the composite
        wu = self.call("get", f"/versions/{e1}/where-used")
        self.assertEqual([x["ci"] for x in wu["composites"]], ["SUITE"])
        self.assertEqual({x["via_version"] for x in wu["baselines"]}, {"2026.Q4.ER1", suite_q4["versions"][-1]["name"]})

        # --- second emergency, spawned from ER1: numbered off the Q4 root, based on ER1
        er2 = self.call("post", f"/releases/{emer['id']}/spawn", {"kind": "emergency", "reason": "CR-1300"}, 201)
        self.assertEqual((er2["name"], er2["parent"], er2["base_version"]),
                         ("2026.Q4.ER2", "2026.Q4", "2026.Q4.ER1"))
        self.call("post", f"/releases/{q4_id}/spawn",
                  {"kind": "patch", "base_version": "2026.Q4-b1"}, 400)      # b1 never released
        ship(er2["versions"][0]["id"])
        q4 = self.call("get", f"/releases/{q4_id}")
        self.assertEqual((q4["released_version"], q4["effective_version"]), ("2026.Q4-b4", "2026.Q4.ER2"))
        self.assertEqual([(b["name"], b["fielded_version"]) for b in q4["baselines_behind"]],
                         [("HSCM-B", "2026.Q4.ER1")])

        # --- abandoned emergency: cancel it, then the next one can be spawned; numbers aren't reused
        er3 = self.call("post", f"/releases/{q4_id}/spawn", {"kind": "emergency", "reason": "CR-1400"}, 201)
        self.call("post", f"/releases/{q4_id}/spawn", {"kind": "emergency", "reason": "CR-1500"}, 409)
        cancelled = self.call("post", f"/releases/{er3['id']}/cancel", {"note": "fix folded into 2027.Q1"})
        self.assertEqual((cancelled["status"], cancelled["versions"][0]["status"]), ("cancelled", "rejected"))
        er4 = self.call("post", f"/releases/{q4_id}/spawn", {"kind": "emergency", "reason": "CR-1500"}, 201)
        self.assertEqual(er4["name"], "2026.Q4.ER4")
        self.call("post", f"/releases/{q4_id}/cancel", status=400)          # released releases can't be cancelled

        # --- audit trail exists
        self.assertTrue(any(e["action"] == "released" for e in self.call("get", "/events?entity=release")))


if __name__ == "__main__":
    unittest.main()

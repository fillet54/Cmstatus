"""End-to-end scenarios through the HTTP API.  Run: python -m unittest discover -s tests"""
import os
import shutil
import tempfile
import unittest

from cmtrack import create_app
from cmtrack.releases import (PatternSource, ReleaseRecord, SourceConfigError, SourceVersion, StaticVersionSource,
                              Unplaced)

LINE = r"(?P<line>\d{4}\.Q\d)"
PARAMS = {"project": "NAV", "patterns": {"planned": LINE, "build": LINE + r"-b(?P<n>\d+)",
                                         "patch": LINE + r"\.P(?P<n>\d+)", "emergency": LINE + r"\.ER(?P<n>\d+)"}}


def v(key, name, date=None, description=None, **kw):
    return {"key": key, "name": name, "date": date, "description": description, **kw}


def nav_versions():
    return [v("1", "2026.Q4", "2026-12-15"), v("2", "2026.Q4-b1", "2026-10-15"), v("3", "2026.Q4-b2", "2026-11-15"),
            v("4", "2026.Q4-b3", "2026-12-15"), v("10", "2027.Q1", "2027-03-15"), v("11", "2027.Q1-b1", "2027-01-15"),
            v("12", "2027.Q1-b2", "2027-02-15")]


class PatternSourceTests(unittest.TestCase):
    def releases(self, versions, params=PARAMS):
        return StaticVersionSource({"NAV": versions}).releases({"name": "NAV-SW"}, params)

    def test_sorts_versions_into_releases(self):
        out = self.releases([v("9", "2026.Q4-b10"), v("1", "2026.Q4", "2026-12-15"), v("2", "2026.Q4-b2"),
                             v("3", "2026.Q4-b1"), v("4", "2026.Q4.ER1", description="CR-1"), v("5", "Triage"),
                             v("6", "2027.Q1-b1"), v("7", "2027.Q2.P1")])
        recs = {r.name: r for r in out if isinstance(r, ReleaseRecord)}
        self.assertEqual([b.name for b in recs["2026.Q4"].builds], ["2026.Q4-b1", "2026.Q4-b2", "2026.Q4-b10"])
        er = recs["2026.Q4.ER1"]
        self.assertEqual((er.kind, er.parent_key, er.reason, [b.name for b in er.builds]),
                         ("emergency", "1", "CR-1", ["2026.Q4.ER1"]))              # patches are their own build
        self.assertNotIn("Triage", recs)                                          # matches no pattern: ignored
        unplaced = {u.name: u.why for u in out if isinstance(u, Unplaced)}
        self.assertEqual(set(unplaced), {"2027.Q1-b1", "2027.Q2.P1"})
        self.assertIn("no planned release for line=2027.Q1", unplaced["2027.Q1-b1"])

    def test_self_build_and_ambiguity(self):
        params = {"patterns": {"planned": r"(?P<rel>\d+\.\d+\.\d+)", "build": r"(?P<rel>\d+\.\d+\.\d+)-rc(?P<n>\d+)"},
                  "self_build": ["planned", "patch", "emergency"], "project": "NAV"}
        rel = self.releases([v("1", "3.2.0", "2026-11-30"), v("2", "3.2.0-rc1")], params)[0]
        self.assertEqual([b.name for b in rel.builds], ["3.2.0-rc1", "3.2.0"])      # the release is its last build
        both = {"patterns": {"planned": r"(?P<x>\d+)", "build": r"(?P<x>\d)"}, "project": "NAV"}
        self.assertIn("more than one pattern", self.releases([v("1", "7")], both)[0].why)

    def test_bad_params(self):
        for params in ({"project": "NAV"}, {"project": "NAV", "patterns": {"planned": "("}},
                       {"project": "NAV", "patterns": {"planned": "x", "hotfix": "y"}},
                       {"project": "NAV", "patterns": {"planned": "x"}, "self_build": ["bogus"]}):
            with self.assertRaises(SourceConfigError):
                self.releases([], params)
        with self.assertRaises(SourceConfigError):
            self.releases([], {**PARAMS, "project": "NOPE"})


class Api(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.source = StaticVersionSource({"NAV": nav_versions()}, name="jira")
        self.app = create_app({"DATABASE": os.path.join(self.tmp, "t.db"), "RELEASE_SOURCES": {"jira": self.source}})
        self.c = self.app.test_client()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def call(self, method, path, json=None, status=200, **kw):
        r = getattr(self.c, method)("/api" + path, json=json, **kw)
        self.assertEqual(r.status_code, status, r.get_json())
        return r.get_json()

    def rel(self, name, ci="NAV-SW"):
        rid = next(r["id"] for r in self.call("get", f"/cis/{ci}/releases") if r["name"] == name)
        return self.call("get", f"/releases/{rid}")

    def vid(self, name, ci="NAV-SW"):
        return self.call("get", f"/cis/{ci}/versions?to={name}")[-1]["id"]

    def ship(self, vid, statuses=("built", "tested")):
        for status in statuses:
            self.call("patch", f"/versions/{vid}", {"status": status})
        return self.call("post", f"/versions/{vid}/release")


class ManualFlowTests(Api):
    """A CI with no release source: releases, builds, patches and emergencies entered by hand."""

    def test_full_flow(self):
        self.call("post", "/cis", {"name": "NAV-SW"}, 201)
        self.call("post", "/cis", {"name": "DISPLAY-SW", "require_tested": False}, 201)
        self.call("post", "/cis/NAV-SW/cscs", {"name": "lib1", "jira_project": "NAVL", "affected_product": "lib1"}, 201)
        self.call("post", "/cis/NAV-SW/cscs", {"name": "lib2", "jira_project": "NAVX", "affected_product": "lib2"}, 201)
        self.call("post", "/cis/DISPLAY-SW/cscs", {"name": "lib1", "jira_project": "NAVL",
                                                   "affected_product": "lib1"}, 409)   # pair already mapped
        self.assertEqual(self.call("get", "/cscs/lookup?project=NAVX&product=lib2")["ci_name"], "NAV-SW")
        self.call("post", "/cis/NAV-SW/sync", status=400)                               # no release source

        # --- releases by hand
        q4 = self.call("post", "/cis/NAV-SW/releases", {"name": "2026.Q4", "target_date": "2026-12-15",
                                                        "builds": ["2026.Q4-b1", "2026.Q4-b2", "2026.Q4-b3"]}, 201)
        self.call("post", "/cis/NAV-SW/releases", {"name": "2026.Q4"}, 409)
        self.call("post", "/cis/NAV-SW/releases", {"kind": "planned"}, 400)              # needs a name
        self.call("post", "/cis/NAV-SW/releases", {"name": "2027.Q1", "target_date": "2027-03-15",
                                                   "builds": ["2027.Q1-b1"]}, 201)
        self.call("post", "/cis/DISPLAY-SW/releases", {"name": "3.2.0", "builds": ["3.2.0-rc1", "3.2.0"]}, 201)

        # --- builds; b3 fails, an ad hoc b4 becomes the release (the CI's gate wants 'tested')
        ids = {x["name"]: x["id"] for x in q4["versions"]}
        for name in ("2026.Q4-b1", "2026.Q4-b2", "2026.Q4-b3"):
            self.call("patch", f"/versions/{ids[name]}", {"status": "built"})
        err = self.call("post", f"/versions/{ids['2026.Q4-b3']}/release", status=400)
        self.assertIn("must be tested", err["problems"][0])
        self.call("patch", f"/versions/{ids['2026.Q4-b3']}", {"status": "rejected"})
        b4 = self.call("post", f"/releases/{q4['id']}/versions", {}, 201)
        self.assertEqual((b4["name"], b4["planned"]), ("2026.Q4-b4", 0))
        rel = self.ship(b4["id"])
        self.assertEqual((rel["status"], rel["released_version_id"]), ("released", b4["id"]))

        # --- patch and emergency by hand: named off the line, built on its effective version
        self.call("post", "/cis/NAV-SW/releases", {"kind": "emergency", "parent": "2026.Q4"}, 400)   # needs reason
        self.call("post", "/cis/NAV-SW/releases", {"kind": "patch"}, 400)                             # needs parent
        patch = self.call("post", "/cis/NAV-SW/releases", {"kind": "patch", "parent": "2026.Q4"}, 201)
        self.assertEqual((patch["name"], patch["base_version"], patch["parent"], [x["name"] for x in patch["versions"]]),
                         ("2026.Q4.P1", "2026.Q4-b4", "2026.Q4", ["2026.Q4.P1"]))
        emer = self.call("post", "/cis/NAV-SW/releases", {"kind": "emergency", "parent": "2026.Q4", "reason": "CR-1234"}, 201)
        self.assertEqual(emer["name"], "2026.Q4.ER1")
        self.ship(emer["versions"][0]["id"])
        q4 = self.call("get", f"/releases/{q4['id']}")
        self.assertEqual((q4["released_version"], q4["effective_version"]), ("2026.Q4-b4", "2026.Q4.ER1"))
        er2 = self.call("post", "/cis/NAV-SW/releases",
                        {"kind": "emergency", "parent": "2026.Q4.ER1", "reason": "CR-1300"}, 201)  # a child: its root
        self.assertEqual((er2["name"], er2["parent"], er2["base_version"]), ("2026.Q4.ER2", "2026.Q4", "2026.Q4.ER1"))
        self.call("post", "/cis/NAV-SW/releases", {"kind": "patch", "parent": "2026.Q4",
                                                   "base_version": "2026.Q4-b1"}, 400)             # never released
        self.assertEqual(self.call("get", "/cis/NAV-SW/attention"), [])
        er3 = self.call("post", "/cis/NAV-SW/releases", {"kind": "emergency", "parent": "2026.Q4", "reason": "CR-1400"}, 201)
        self.assertEqual([(a["kind"], a["name"], a["text"]) for a in self.call("get", "/cis/NAV-SW/attention")],
                         [("open_children", "2026.Q4", "2 open emergency releases on this line")])
        self.call("post", f"/releases/{er3['id']}/cancel", {"note": "duplicate of CR-1300"})

        # --- DISPLAY ('built' is enough) and a composite suite pinning both
        d = self.rel("3.2.0", "DISPLAY-SW")
        self.ship(d["versions"][1]["id"], ("built",))
        self.call("post", "/cis", {"name": "SUITE", "kind": "composite"}, 201)
        suite = self.call("post", "/cis/SUITE/releases", {"name": "2026.Q4", "builds": ["2026.Q4-b1"]}, 201)
        sv = suite["versions"][0]["id"]
        self.call("patch", f"/versions/{sv}", {"status": "built"})
        self.call("patch", f"/versions/{sv}", {"status": "tested"})
        self.call("put", f"/versions/{sv}/manifest",
                  {"children": [{"ci": "NAV-SW", "version": "2026.Q4-b1"}, {"ci": "DISPLAY-SW", "version": "3.2.0"}]})
        err = self.call("post", f"/versions/{sv}/release", status=400)       # b1 is only built
        self.assertIn("NAV-SW 2026.Q4-b1", err["problems"][0])
        self.call("put", f"/versions/{sv}/manifest",
                  {"children": [{"ci": "NAV-SW", "version": "2026.Q4.ER1"}, {"ci": "DISPLAY-SW", "version": "3.2.0"}]})
        self.call("post", f"/versions/{sv}/release")

        # --- IFCs: parent/child, scraped HSCM with placeholders, a manual successor, diff
        self.call("post", "/ifcs", {"name": "IFC-2"}, 201)
        self.call("post", "/ifcs", {"name": "IFC-2.1", "parent": "IFC-2"}, 201)
        self.call("patch", "/ifcs/IFC-2", {"parent": "IFC-2.1"}, status=400)        # cycle
        csv_text = "ci,version,type\nNAV-SW,2026.Q4-b4,CSCI\nRADAR-SW,7.4,\nANTENNA,Rev C,HWCI\nNAV-SW,,\n"
        imp = self.call("post", "/ifcs/IFC-2.1/hscm?name=HSCM-A&source_ref=DOC-001", status=201,
                        data=csv_text, content_type="text/csv")
        self.assertEqual(sorted(imp["placeholders_created"]), ["ANTENNA", "RADAR-SW"])
        self.assertIn("row 4: missing ci or version; skipped", imp["warnings"])
        self.assertFalse(self.call("get", "/cis/RADAR-SW")["managed"])
        a_id = imp["baseline"]["id"]
        b = self.call("post", f"/baselines/{a_id}/clone", {"name": "HSCM-B"}, 201)
        entries = [{"ci": e["ci"], "version": e["version"]} for e in b["entries"] if e["ci"] != "NAV-SW"]
        entries += [{"ci": "NAV-SW", "version": "2026.Q4.ER1"}, {"ci": "SUITE", "version": str(sv)}]
        self.call("put", f"/baselines/{b['id']}/entries", {"entries": entries})
        self.call("post", f"/baselines/{b['id']}/approve")
        diff = self.call("get", f"/baselines/{a_id}/diff/{b['id']}")
        self.assertEqual(diff["changed"]["NAV-SW"], {"from": "2026.Q4-b4", "to": "2026.Q4.ER1"})
        c = self.call("post", "/ifcs/IFC-2.1/baselines",
                      {"name": "HSCM-C", "entries": [{"ci": "NAV-SW", "version": "2026.Q4-b2"}]}, 201)
        self.call("post", f"/baselines/{c['id']}/approve", status=400)       # b2 was never released
        wu = self.call("get", f"/versions/{emer['versions'][0]['id']}/where-used")
        self.assertEqual([x["ci"] for x in wu["composites"]], ["SUITE"])

        # --- ER2 ships: HSCM-B now fields an older version of the line
        self.ship(er2["versions"][0]["id"])
        q4 = self.call("get", f"/releases/{q4['id']}")
        self.assertEqual(q4["effective_version"], "2026.Q4.ER2")
        self.assertEqual([(x["name"], x["fielded_version"]) for x in q4["baselines_behind"]], [("HSCM-B", "2026.Q4.ER1")])

        # --- cancel an abandoned patch; released releases can't be cancelled; numbers aren't reused
        cancelled = self.call("post", f"/releases/{patch['id']}/cancel", {"note": "folded into 2027.Q1"})
        self.assertEqual((cancelled["status"], cancelled["versions"][0]["status"]), ("cancelled", "rejected"))
        self.assertEqual(self.call("post", "/cis/NAV-SW/releases", {"kind": "patch", "parent": "2026.Q4"}, 201)["name"],
                         "2026.Q4.P2")
        self.call("post", f"/releases/{q4['id']}/cancel", status=400)
        self.assertTrue(any(e["action"] == "released" for e in self.call("get", "/events?entity=release")))

    def test_date_corrections(self):
        self.call("post", "/cis", {"name": "NAV-SW"}, 201)
        rel = self.call("post", "/cis/NAV-SW/releases", {"name": "2026.Q4", "builds": ["2026.Q4-b1"]}, 201)
        vid, rid = rel["versions"][0]["id"], rel["id"]
        self.call("patch", f"/versions/{vid}", {"built_at": "2026-09-01", "note": "x"}, 400)          # not built yet
        ver = self.call("patch", f"/versions/{vid}", {"status": "built", "built_at": "2026-09-04"})
        self.assertEqual(ver["built_at"], "2026-09-04T00:00:00+00:00")
        self.call("patch", f"/versions/{vid}", {"built_at": "2026-09-03"}, 400)                         # needs a note
        self.call("patch", f"/versions/{vid}", {"built_at": "2099-01-01", "note": "typo"}, 400)         # future
        ver = self.call("patch", f"/versions/{vid}", {"built_at": "2026-09-03 17:30", "note": "ran Friday"})
        self.assertEqual(ver["built_at"], "2026-09-03T17:30:00+00:00")
        self.call("patch", f"/versions/{vid}", {"status": "tested"})
        self.call("post", f"/versions/{vid}/release", {"released_at": "2026-09-01"}, 400)               # before built
        out = self.call("post", f"/versions/{vid}/release", {"released_at": "2026-09-05"})
        self.assertEqual(out["released_at"], "2026-09-05T00:00:00+00:00")
        self.call("patch", f"/releases/{rid}", {"released_at": "2026-09-06"}, 400)                      # needs a note
        self.call("patch", f"/releases/{rid}", {"released_at": "2026-09-02", "note": "x"}, 400)         # before built
        out = self.call("patch", f"/releases/{rid}", {"released_at": "2026-09-06T08:00:00Z", "note": "CCB date"})
        self.assertEqual((out["released_at"], out["pinned"]), ("2026-09-06T08:00:00+00:00", []))       # not a sync field
        self.call("patch", f"/versions/{vid}", {"built_at": "2026-09-07", "note": "x"}, 400)            # after release
        corrected = [e for e in self.call("get", "/events?entity=release") if e["action"] == "corrected"]
        self.assertEqual(corrected[0]["detail"]["note"], "CCB date")
        rel = self.call("patch", f"/releases/{rid}", {"target_date": "2026-12-20", "name": "2026.Q4 (FY27)"})
        self.assertEqual((rel["target_date"], rel["name"], rel["pinned"]), ("2026-12-20", "2026.Q4 (FY27)", []))
        self.call("patch", f"/releases/{rid}", {"target_date": "soon"}, 400)
        self.call("patch", f"/releases/{rid}", {"parent": "2026.Q4"}, 400)                              # planned


class SyncTests(Api):
    """A CI whose releases come from its release source."""

    def setUp(self):
        super().setUp()
        self.call("post", "/cis", {"name": "NAV-SW", "release_source": "jira", "source_params": PARAMS}, 201)

    def sync(self, **kw):
        return self.call("post", "/cis/NAV-SW/sync", kw)

    def names(self):
        return [r["name"] for r in self.call("get", "/cis/NAV-SW/releases")]

    def test_sync_is_idempotent_and_updates(self):
        s = self.sync()
        self.assertEqual(s["created"], ["2026.Q4", "2026.Q4-b1", "2026.Q4-b2", "2026.Q4-b3", "2027.Q1", "2027.Q1-b1",
                                        "2027.Q1-b2"])
        again = self.sync()
        self.assertEqual([again[k] for k in ("created", "updated", "missing", "issues")], [[], [], [], []])
        self.source.projects["NAV"][0].date = "2026-12-18"                     # rescheduled in Jira
        self.source.projects["NAV"][1].name = "2026.Q4-b01"                    # renamed in Jira (same key)
        s = self.sync()
        self.assertEqual(sorted(u["name"] for u in s["updated"]), ["2026.Q4", "2026.Q4-b01"])
        self.assertEqual(s["missing"], [])
        self.assertEqual(self.rel("2026.Q4")["target_date"], "2026-12-18")
        self.assertEqual(self.call("get", "/cis/NAV-SW")["last_sync"]["counts"]["updated"], 2)
        # lineage follows the source's order
        self.assertEqual([x["name"] for x in self.call("get", "/cis/NAV-SW/versions?to=2027.Q1-b1")][-2:],
                         ["2026.Q4-b3", "2027.Q1-b1"])

    def test_recreated_in_the_source_is_rekeyed(self):
        self.sync()
        q1 = self.rel("2027.Q1")
        self.source.projects["NAV"][4].key = "99"                              # deleted and re-created in Jira
        s = self.sync()
        self.assertEqual((s["rekeyed"], s["created"], s["missing"]), (["2027.Q1"], [], []))
        self.assertEqual((self.rel("2027.Q1")["id"], self.rel("2027.Q1")["source_key"]), (q1["id"], "99"))

    def test_dry_run_changes_nothing(self):
        s = self.sync(dry_run=True)
        self.assertEqual((s["dry_run"], len(s["created"])), (True, 7))
        self.assertEqual(self.names(), [])
        self.assertIsNone(self.call("get", "/cis/NAV-SW")["last_sync"])

    def test_patches_come_from_the_source(self):
        self.source.projects["NAV"] += [v("20", "2026.Q4.ER1", "2027-01-20", "CR-1234"), v("21", "2027.Q9-b1")]
        s = self.sync()
        self.assertEqual([i["name"] for i in s["issues"]], ["2027.Q9-b1"])
        er = self.rel("2026.Q4.ER1")
        self.assertEqual((er["kind"], er["parent"], er["reason"], er["base_version"]),
                         ("emergency", "2026.Q4", "CR-1234", None))
        kinds = {a["kind"] for a in self.call("get", "/cis/NAV-SW/attention")}
        self.assertEqual(kinds, {"unplaced", "no_base"})
        q4 = self.rel("2026.Q4")
        self.ship(next(x["id"] for x in q4["versions"] if x["name"] == "2026.Q4-b3"))
        s = self.sync()                                                         # the line released: base filled in
        self.assertEqual(s["bases"], [{"name": "2026.Q4.ER1", "base": "2026.Q4-b3"}])
        self.assertEqual(self.rel("2026.Q4.ER1")["base_version"], "2026.Q4-b3")
        # the source believes something is released that cmtrack hasn't released
        self.source.projects["NAV"][4].released = True
        self.assertIn("the source says it's released", self.sync()["issues"][-1]["why"])

    def test_rename_then_remap(self):
        self.sync()
        q1 = self.rel("2027.Q1")
        b1 = next(x for x in q1["versions"] if x["name"] == "2027.Q1-b1")
        self.call("patch", f"/versions/{b1['id']}", {"status": "built"})
        self.call("put", f"/versions/{b1['id']}/parents", {"parents": ["2026.Q4-b3"]})             # hand-set lineage
        # Jira: the Q1 release and its builds are replaced by new versions with new keys (a re-created project)
        nav = [x for x in self.source.projects["NAV"] if not x.name.startswith("2027")]
        self.source.projects["NAV"] = nav + [SourceVersion("30", "FY27.Q2", "2027-03-15"),
                                             SourceVersion("31", "FY27.Q2-b1", "2027-01-15"),
                                             SourceVersion("32", "FY27.Q2-b2", "2027-02-15")]
        params = {**PARAMS, "patterns": {k: p.replace(LINE, r"(?P<line>\d{4}\.Q\d|FY\d\d\.Q\d)")
                                         for k, p in PARAMS["patterns"].items()}}
        self.call("patch", "/cis/NAV-SW", {"source_params": params})
        s = self.sync()
        self.assertEqual(sorted(s["missing"]), ["2027.Q1", "2027.Q1-b1", "2027.Q1-b2"])
        self.assertEqual(s["created"], ["FY27.Q2", "FY27.Q2-b1", "FY27.Q2-b2"])
        att = self.call("get", "/cis/NAV-SW/attention")
        self.assertEqual([(a["kind"], a["name"]) for a in att], [("missing_release", "2027.Q1")])  # builds roll up

        new = self.rel("FY27.Q2")
        err = self.call("post", f"/releases/{q1['id']}/remap", {"to": "2026.Q4"}, 409)            # not a fresh release
        self.assertIn("has hand-set lineage", err["problems"])
        out = self.call("post", f"/releases/{q1['id']}/remap", {"to": new["name"]})
        self.assertEqual((out["id"], out["name"], out["source_key"], out["source_state"]), (q1["id"], "FY27.Q2", "30", "synced"))
        self.assertEqual([(x["name"], x["status"], x["source_key"]) for x in out["versions"]],
                         [("FY27.Q2-b1", "built", "31"), ("FY27.Q2-b2", "planned", "32")])
        self.assertEqual(out["versions"][0]["id"], b1["id"])                                      # same row, kept
        self.assertEqual(self.call("get", f"/versions/{b1['id']}/lineage")["lineage"], "manual")
        self.assertNotIn("2027.Q1", self.names())
        self.assertEqual(self.names().count("FY27.Q2"), 1)                                        # the new one is gone
        again = self.sync()
        self.assertEqual([again[k] for k in ("created", "updated", "missing")], [[], [], []])
        self.assertEqual(self.call("get", "/cis/NAV-SW/attention"), [])

    def test_detach_and_phantoms(self):
        self.sync()
        self.source.projects["NAV"] = [x for x in self.source.projects["NAV"] if not x.name.startswith("2026")]
        self.sync()
        q1 = self.call("get", f"/versions/{self.vid('2027.Q1-b1')}/lineage")
        self.assertEqual(q1["parents"], [])                          # Q4 is missing and was never built: skipped
        q4 = self.rel("2026.Q4")
        out = self.call("post", f"/releases/{q4['id']}/detach")
        self.assertEqual((out["source_key"], out["source_state"], out["source"]), (None, None, "manual"))
        self.assertTrue(all(x["source_key"] is None for x in out["versions"]))
        self.assertEqual(self.call("get", "/cis/NAV-SW/attention"), [])
        self.assertEqual(self.call("get", f"/versions/{self.vid('2027.Q1-b1')}/lineage")["parents"][0]["name"],
                         "2026.Q4-b3")                              # detached = kept by hand, back in the chain

    def test_pins_survive_syncs(self):
        self.sync()
        q4 = self.rel("2026.Q4")
        out = self.call("patch", f"/releases/{q4['id']}", {"target_date": "2026-12-20", "reason": q4["reason"]})
        self.assertEqual(out["pinned"], ["target_date"])                                   # only what changed
        s = self.sync()
        self.assertEqual(s["pinned"], [{"name": "2026.Q4", "field": "target_date", "source": "2026-12-15",
                                        "kept": "2026-12-20"}])
        self.assertEqual(self.rel("2026.Q4")["target_date"], "2026-12-20")
        b2 = self.vid("2026.Q4-b2")
        self.assertEqual(self.call("patch", f"/versions/{b2}", {"planned_date": "2026-11-20"})["pinned"], ["planned_date"])
        self.sync()
        self.assertEqual(self.call("get", f"/versions/{b2}")["planned_date"], "2026-11-20")
        self.call("patch", f"/releases/{q4['id']}", {"unpin": ["bogus"]}, 400)
        self.call("patch", f"/releases/{q4['id']}", {"unpin": ["target_date"]})
        self.call("patch", f"/versions/{b2}", {"unpin": ["planned_date"]})
        self.sync()
        self.assertEqual(self.rel("2026.Q4")["target_date"], "2026-12-15")
        self.assertEqual(self.call("get", f"/versions/{b2}")["planned_date"], "2026-11-15")

    def test_adopts_hand_entered_rows(self):
        self.call("patch", "/cis/NAV-SW", {"release_source": None})
        mine = self.call("post", "/cis/NAV-SW/releases", {"name": "2026.Q4", "builds": ["2026.Q4-b1", "2026.Q4-x"]}, 201)
        self.call("patch", "/cis/NAV-SW", {"release_source": "jira"})
        s = self.sync()
        self.assertEqual(s["adopted"], ["2026.Q4", "2026.Q4-b1"])
        q4 = self.rel("2026.Q4")
        self.assertEqual(q4["id"], mine["id"])
        self.assertEqual([(x["name"], x["source_key"]) for x in q4["versions"]],
                         [("2026.Q4-b1", "2"), ("2026.Q4-x", None), ("2026.Q4-b2", "3"), ("2026.Q4-b3", "4")])

    def test_source_problems(self):
        self.call("patch", "/cis/NAV-SW", {"source_params": {"project": "NAV"}})
        self.assertIn("patterns", self.call("post", "/cis/NAV-SW/sync", {}, 400)["error"])
        self.call("patch", "/cis/NAV-SW", {"source_params": PARAMS, "release_source": "nope"})
        self.assertIn("not configured", self.call("post", "/cis/NAV-SW/sync", {}, 400)["error"])

        class Broken(PatternSource):
            def versions(self, ci, params):
                raise ConnectionError("jira is down")
        self.app.config["RELEASE_SOURCES"]["nope"] = Broken()
        err = self.call("post", "/cis/NAV-SW/sync", {}, 502)
        self.assertIn("jira is down", err["error"])
        self.assertEqual(self.call("get", "/release-sources")[0]["name"], "jira")


if __name__ == "__main__":
    unittest.main()

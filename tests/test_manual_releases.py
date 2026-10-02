"""The "manual" release source: versions kept in cmtrack, sorted by name patterns.  Run: python -m unittest discover -s tests"""
import os
import shutil
import tempfile
import unittest

from cmtrack import create_app
from cmtrack.manual_releases import ManualReleaseSource

LINE = r"(?P<line>\d{4}\.Q\d)"
PARAMS = {"patterns": {"planned": LINE, "build": LINE + r"-b(?P<n>\d+)", "patch": LINE + r"\.P(?P<n>\d+)",
                       "emergency": LINE + r"\.ER(?P<n>\d+)"}}


class ManualSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.app = create_app({"DATABASE": os.path.join(self.tmp, "t.db"), "RELEASE_SOURCES": {}})
        self.c = self.app.test_client()
        self.call("post", "/cis", {"name": "DISP-SW", "release_source": "manual", "source_params": PARAMS})

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def call(self, method, path, json=None, status=None):
        r = getattr(self.c, method)("/api" + path, json=json)
        if status is not None:
            self.assertEqual(r.status_code, status, r.get_json())
        return r.get_json()

    def add(self, name, **kw):
        return self.call("post", "/cis/DISP-SW/manual-versions", {"name": name, **kw}, 201)

    def releases(self):
        return {r["name"]: r for r in self.call("get", "/cis/DISP-SW/releases")}

    def test_registered_by_default(self):
        self.assertIsInstance(self.app.config["RELEASE_SOURCES"]["manual"], ManualReleaseSource)
        self.assertIn({"name": "manual", "type": "ManualReleaseSource"}, self.call("get", "/release-sources"))
        own = ManualReleaseSource(name="manual")
        app = create_app({"DATABASE": os.path.join(self.tmp, "u.db"), "RELEASE_SOURCES": {"manual": own}})
        self.assertIs(app.config["RELEASE_SOURCES"]["manual"], own)                # one already named: kept

    def test_versions_become_releases_and_builds(self):
        out = self.add("2027.Q1", date="2027-03-15")
        self.assertEqual((out["version"]["kind"], out["sync"]["created"]), ("planned", ["2027.Q1"]))   # synced
        self.add("2027.Q1-b1", date="2027-01-15")
        self.add("2027.Q1-b2", date="2027-02-15")
        self.add("2027.Q1.P1", description="CR-77")
        rels = self.releases()
        self.assertEqual(sorted(rels), ["2027.Q1", "2027.Q1.P1"])
        q1 = self.call("get", f"/releases/{rels['2027.Q1']['id']}")
        self.assertEqual([(v["name"], v["planned_date"]) for v in q1["versions"]],
                         [("2027.Q1-b1", "2027-01-15"), ("2027.Q1-b2", "2027-02-15")])
        self.assertEqual((rels["2027.Q1.P1"]["kind"], rels["2027.Q1.P1"]["reason"]), ("patch", "CR-77"))
        listed = self.call("get", "/cis/DISP-SW/manual-versions")
        self.assertEqual([(m["name"], m["kind"]) for m in listed],
                         [("2027.Q1-b1", "build"), ("2027.Q1-b2", "build"), ("2027.Q1", "planned"), ("2027.Q1.P1", "patch")])

    def test_checks(self):
        self.add("2027.Q1")
        self.assertIn("matches none", self.call("post", "/cis/DISP-SW/manual-versions", {"name": "nightly"}, 400)["error"])
        self.assertIn("already has", self.call("post", "/cis/DISP-SW/manual-versions", {"name": "2027.Q1"}, 409)["error"])
        self.assertIn("YYYY-MM-DD", self.call("post", "/cis/DISP-SW/manual-versions",
                                              {"name": "2027.Q2", "date": "soon"}, 400)["error"])
        self.call("post", "/cis", {"name": "NAV-SW"})
        self.assertIn("doesn't use the manual", self.call("post", "/cis/NAV-SW/manual-versions", {"name": "2027.Q1"},
                                                          400)["error"])
        self.assertEqual([m["name"] for m in self.call("get", "/cis/DISP-SW/manual-versions")], ["2027.Q1"])  # rolled back

    def test_rename_and_remove(self):
        self.add("2027.Q1")
        b1 = self.add("2027.Q1-b1")["version"]
        rid = self.releases()["2027.Q1"]["id"]
        vid = self.call("get", f"/releases/{rid}")["versions"][0]["id"]
        out = self.call("patch", f"/manual-versions/{b1['id']}", {"name": "2027.Q1-b9", "released": True}, 200)
        self.assertEqual((out["version"]["name"], out["version"]["released"]), ("2027.Q1-b9", True))
        self.assertEqual(self.call("get", f"/versions/{vid}")["name"], "2027.Q1-b9")        # same build, renamed
        out = self.call("delete", f"/manual-versions/{b1['id']}", status=200)
        self.assertEqual(out["sync"]["missing"], ["2027.Q1-b9"])                            # flagged, not deleted
        self.assertEqual(self.call("get", f"/versions/{vid}")["source_state"], "missing")
        self.call("patch", "/manual-versions/999", {"name": "x"}, 404)

    def test_ui_forms(self):
        self.add("2027.Q1")
        page = self.c.get("/cis/DISP-SW").get_data(as_text=True)
        self.assertIn('action="/cis/DISP-SW/manual-versions"', page)
        self.assertNotIn("Add release", page)                                    # no by-hand release form here
        r = self.c.post("/cis/DISP-SW/manual-versions", data={"name": "2027.Q1-b1", "date": "2027-01-15",
                                                              "flags": ["", "released"]})
        self.assertEqual((r.status_code, r.headers["Location"]), (303, "/cis/DISP-SW"))
        b1 = next(m for m in self.call("get", "/cis/DISP-SW/manual-versions") if m["name"] == "2027.Q1-b1")
        self.assertTrue(b1["released"])
        self.c.post(f"/manual-versions/{b1['id']}/edit", data={"name": "2027.Q1-b1", "date": "", "description": "",
                                                               "flags": [""]})
        b1 = next(m for m in self.call("get", "/cis/DISP-SW/manual-versions") if m["name"] == "2027.Q1-b1")
        self.assertEqual((b1["released"], b1["date"]), (False, None))            # unchecked box clears the flag
        rid = self.releases()["2027.Q1"]["id"]
        panel = self.c.get(f"/releases/{rid}", headers={"HX-Request": "true"}).get_data(as_text=True)
        self.assertIn('action="/cis/DISP-SW/manual-versions"', panel)          # "Add a build" adds a version
        self.c.post(f"/manual-versions/{b1['id']}/delete")
        self.assertEqual([m["name"] for m in self.call("get", "/cis/DISP-SW/manual-versions")], ["2027.Q1"])


if __name__ == "__main__":
    unittest.main()

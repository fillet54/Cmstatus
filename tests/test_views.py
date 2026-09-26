"""HTML views: every page renders, htmx requests get fragments.  Run: python -m unittest discover -s tests"""
import os
import re
import shutil
import tempfile
import unittest

from cmtrack import create_app
from cmtrack.demo import NAV_VERSIONS, seed
from cmtrack.releases import StaticVersionSource

HX = {"HX-Request": "true"}


class ViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.app = create_app({"DATABASE": os.path.join(cls.tmp, "t.db")})
        seed(cls.app.test_client())

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def setUp(self):
        self.c = self.app.test_client()
        self.api = lambda path: self.c.get("/api" + path).get_json()

    def get(self, path, headers=None, status=200):
        r = self.c.get(path, headers=headers or {})
        self.assertEqual(r.status_code, status, path)
        return r.get_data(as_text=True)

    def test_pages_render(self):
        q4 = self.api("/cis/NAV-SW/releases")[0]["id"]
        b = self.api("/ifcs/IFC-1")["current_hscm"]
        for path in ("/", "/cis", "/cis/NAV-SW", "/cis/SUITE", "/cis/RADAR-SW", f"/releases/{q4}",
                     f"/versions/{b['entries'][0]['version_id']}", "/ifcs", "/ifcs/IFC-1", "/ifcs/IFC-2",
                     f"/baselines/{b['id']}", "/events"):
            self.assertIn("<html", self.get(path), path)

    def test_fragments(self):
        rows = self.get("/cis?q=nav", HX)
        self.assertNotIn("<html", rows)
        self.assertIn("NAV-SW", rows)
        self.assertNotIn("DISPLAY-SW", rows)
        self.assertIn("ANTENNA", self.get("/cis?managed=0", HX))
        # boosted navigation and history restores get the full page
        self.assertIn("<html", self.get("/cis", {**HX, "HX-Boosted": "true"}))
        self.assertIn("<html", self.get("/cis", {**HX, "HX-History-Restore-Request": "true"}))

        q4 = self.api("/cis/NAV-SW/releases")[0]["id"]
        panel = self.get(f"/releases/{q4}", HX)
        self.assertNotIn("<html", panel)
        self.assertIn("2026.Q4.ER1", panel)                   # effective version
        self.assertIn("still field an older version", panel)  # IFC-1 Build 2 fields b4

    def test_baseline_staleness_and_diff(self):
        ifc = self.api("/ifcs/IFC-1")
        a, b = ifc["baselines"][0]["id"], ifc["current_hscm"]["id"]
        page = self.get(f"/baselines/{b}")
        self.assertIn("behind:", page)
        diff = self.get(f"/baselines/{b}/diff?other={a}", HX)
        self.assertIn("added", diff)
        self.assertIn("SUITE", diff)

    def test_release_table_sorting(self):
        rows = lambda html: re.findall(r'hx-boost="false" onclick="event.preventDefault\(\)">([^<]+)</a>', html)
        page = self.get("/cis/NAV-SW")
        self.assertEqual(rows(page)[:3], ["2027.Q2", "2027.Q1", "2026.Q4"])               # latest first
        self.assertNotIn(">Reset<", page)
        frag = self.get("/cis/NAV-SW/releases-table?sort=name&dir=asc", HX)
        self.assertNotIn("<html", frag)
        self.assertEqual(rows(frag)[:3], ["2026.Q4", "2026.Q4.ER1", "2026.Q4.P1"])        # fixes stay under their line
        self.assertIn('aria-sort="ascending"', frag)
        self.assertIn("Reset", frag)
        self.assertIn("sort=name&amp;dir=desc", frag)                                     # clicking again flips it

    def test_events_paging(self):
        first = self.get("/events", HX)
        self.assertIn('hx-trigger="revealed"', first)          # demo has more than one page
        last_id = min(e["id"] for e in self.api("/events?limit=50"))
        rest = self.get(f"/events?before={last_id}", HX)
        self.assertNotIn(f">{last_id}<", rest)
        self.assertIn("release #", self.get("/events?entity=release", HX))

    def test_every_page_uses_the_ui_layout(self):
        rid = self.api("/cis/NAV-SW/releases")[0]["id"]
        vid = self.api(f"/releases/{rid}")["versions"][0]["id"]
        bid = self.api("/ifcs/IFC-1")["current_hscm"]["id"]
        for path in ("/", "/cis", "/cis/NAV-SW", f"/releases/{rid}", f"/versions/{vid}", "/cis/NAV-SW/work",
                     "/backlogs", "/backlogs/Nav & Display", "/ifcs", "/ifcs/IFC-1", "/ifcs/IFC-2", f"/baselines/{bid}", "/events",
                     "/ui"):
            page = self.get(path)
            self.assertIn("/static/ui.css", page, path)
            for old in ("daisyui", "tailwindcss", "X-UI-Layout"):
                self.assertNotIn(old, page, path)
            for pos in ("top", "bottom"):                                         # marking banners
                self.assertEqual(page.count(f"ui-marking--{pos}"), 1, (path, pos))
        self.assertIn("/static/ui.css", self.get("/cis/NOPE", status=404))        # the error page too
        ci = self.get("/cis/NAV-SW")
        self.assertIn('hx-trigger="load"', ci)                                    # a release panel opens by default
        self.assertIn("ui-row--selected", ci)
        panel = self.get(f"/releases/{rid}", HX)
        self.assertIn('aria-label="Release ', panel)
        self.assertNotIn("<html", panel)
        self.assertIn("/static/backlog.js", self.get("/backlogs/Nav & Display"))

    def test_not_found_is_html_outside_api(self):
        self.assertIn("CI &#39;NOPE&#39; not found", self.get("/cis/NOPE", status=404))
        self.assertEqual(self.c.get("/api/cis/NOPE").get_json(), {"error": "CI 'NOPE' not found"})


class FormTests(unittest.TestCase):
    """The release and version forms: sync, preview, add, edit (pins), corrections, remap, detach."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.source = StaticVersionSource({"NAV": [dict(v) for v in NAV_VERSIONS]}, name="jira")
        self.app = create_app({"DATABASE": os.path.join(self.tmp, "t.db"), "RELEASE_SOURCES": {"jira": self.source}})
        self.c = self.app.test_client()
        seed(self.c)
        self.api = lambda path: self.c.get("/api" + path).get_json()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def rel(self, name, ci="NAV-SW"):
        return next(r for r in self.api(f"/cis/{ci}/releases") if r["name"] == name)

    def post(self, path, data=None, status=303):
        r = self.c.post(path, data=data or {})
        self.assertEqual(r.status_code, status, r.get_data(as_text=True)[-600:])
        return r

    def test_ci_page_sync_and_attention(self):
        page = self.c.get("/cis/NAV-SW").get_data(as_text=True)
        self.assertIn("Sync from jira", page)
        self.assertIn('aria-label="Needs attention"', page)
        self.assertIn("2027.Q3-b1", page)                                   # the build Jira has no release for
        self.assertIn("Last synced", page)
        manual = self.c.get("/cis/DISPLAY-SW").get_data(as_text=True)
        self.assertNotIn("Sync from", manual)
        self.assertIn("entered by hand", manual)

        self.source.projects["NAV"][0].date = "2026-12-18"
        preview = self.post("/cis/NAV-SW/sync", {"dry_run": "1"}, 200).get_data(as_text=True)
        self.assertIn("Sync preview", preview)
        self.assertIn("2026-12-15 → 2026-12-18", preview)
        self.assertEqual(self.rel("2026.Q4")["target_date"], "2026-12-15")      # nothing changed yet
        self.assertEqual(self.post("/cis/NAV-SW/sync").headers["Location"], "/cis/NAV-SW")
        self.assertEqual(self.rel("2026.Q4")["target_date"], "2026-12-18")

    def test_rename_remap_through_the_page(self):
        q2 = self.rel("2027.Q2")
        # Jira: Q2 and its builds are gone; a Q3 appears (and picks up the Q3 build the last sync couldn't place)
        nav = self.source.projects["NAV"]
        self.source.projects["NAV"] = [v for v in nav if not v.name.startswith("2027.Q2")]
        self.source.projects["NAV"].append(type(nav[0])("20030", "2027.Q3", "2027-09-15"))
        self.post("/cis/NAV-SW/sync")
        page = self.c.get("/cis/NAV-SW").get_data(as_text=True)
        self.assertIn("no longer in the release source", page)
        self.assertIn(f'action="/releases/{q2["id"]}/remap"', page)
        r = self.post(f"/releases/{q2['id']}/remap", {"to": "2027.Q3", "next": "/cis/NAV-SW"})
        self.assertEqual(r.headers["Location"], "/cis/NAV-SW")
        moved = self.api(f"/releases/{q2['id']}")
        self.assertEqual((moved["name"], [v["name"] for v in moved["versions"]]),
                         ("2027.Q3", ["2027.Q3-b1", "2027.Q2-b2", "2027.Q2-b3"]))
        kinds = [(a["kind"], a["name"]) for a in self.api("/cis/NAV-SW/attention")]
        self.assertEqual(kinds, [("missing_version", "2027.Q2-b2"), ("missing_version", "2027.Q2-b3")])
        b2 = moved["versions"][1]["id"]
        self.post(f"/versions/{b2}/detach")
        self.assertEqual(len(self.api("/cis/NAV-SW/attention")), 1)

    def test_edit_pin_correct(self):
        q1 = self.rel("2027.Q1")
        panel = self.c.get(f"/releases/{q1['id']}", headers={"HX-Request": "true"}).get_data(as_text=True)
        self.assertIn(f'action="/releases/{q1["id"]}/edit"', panel)
        self.post(f"/releases/{q1['id']}/edit", {"name": "2027.Q1", "target_date": "2027-03-20", "reason": ""})
        detail = self.api(f"/releases/{q1['id']}")
        self.assertEqual((detail["target_date"], detail["pinned"]), ("2027-03-20", ["target_date"]))
        self.assertIn("Unpin", self.c.get(f"/releases/{q1['id']}").get_data(as_text=True))
        self.post(f"/releases/{q1['id']}/unpin", {"field": "target_date"})
        self.post("/cis/NAV-SW/sync")
        self.assertEqual(self.api(f"/releases/{q1['id']}")["target_date"], "2027-03-15")

        q4 = self.rel("2026.Q4")
        released = self.api(f"/releases/{q4['id']}")["released_at"]
        r = self.c.post(f"/releases/{q4['id']}/correct", data={"released_at": "2026-12-16", "note": ""})
        self.assertEqual(r.status_code, 400)
        self.assertIn("needs a note", r.get_data(as_text=True))              # the error page, htmx swaps it too
        self.assertNotEqual(self.api(f"/releases/{q4['id']}")["released_at"], "2026-12-16T00:00:00+00:00")
        self.assertTrue(released)
        vid = self.api(f"/releases/{q4['id']}")["released_version_id"]
        built = self.api(f"/versions/{vid}")["built_at"]
        self.post(f"/versions/{vid}/correct", {"built_at": built[:10], "note": "clock skew"})
        self.assertIn("corrected", self.c.get(f"/versions/{vid}").get_data(as_text=True))
        self.assertIn("htmx-config", self.c.get("/").get_data(as_text=True))

    def test_add_release_and_build_by_hand(self):
        self.post("/cis/DISPLAY-SW/releases", {"kind": "planned", "name": "3.4.0", "target_date": "2027-08-31",
                                               "builds": "3.4.0-rc1, 3.4.0"})
        rel = self.rel("3.4.0", "DISPLAY-SW")
        self.assertEqual((rel["target_date"], rel["planned_versions"] + rel["unplanned_versions"]), ("2027-08-31", 2))
        self.post(f"/releases/{rel['id']}/builds", {"name": "", "planned_date": "2027-08-15", "next": "/cis/DISPLAY-SW"})
        names = [v["name"] for v in self.api(f"/releases/{rel['id']}")["versions"]]
        self.assertEqual(names, ["3.4.0-rc1", "3.4.0", "3.4.0-b3"])
        self.post("/cis/DISPLAY-SW/releases", {"kind": "patch", "parent": "3.2.0"})
        self.assertEqual(self.rel("3.2.0.P1", "DISPLAY-SW")["base_version_id"],
                         self.api(f"/releases/{self.rel('3.2.0', 'DISPLAY-SW')['id']}")["released_version_id"])


if __name__ == "__main__":
    unittest.main()

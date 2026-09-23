"""HTML views: every page renders, htmx requests get fragments.  Run: python -m unittest discover -s tests"""
import os
import shutil
import tempfile
import unittest

from cmtrack import create_app
from cmtrack.demo import seed

HERE = os.path.dirname(os.path.abspath(__file__))
HX = {"HX-Request": "true"}


class ViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        shutil.copy(os.path.join(HERE, "..", "policies", "display-sw.txt"), cls.tmp)
        cls.app = create_app({"DATABASE": os.path.join(cls.tmp, "t.db"), "POLICY_DIR": cls.tmp})
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
        b = self.api("/ifcs/IFC-2.1")["current_hscm"]
        for path in ("/", "/cis", "/cis/NAV-SW", "/cis/SUITE", "/cis/RADAR-SW", f"/releases/{q4}",
                     f"/versions/{b['entries'][0]['version_id']}", "/ifcs", "/ifcs/IFC-2", "/ifcs/IFC-2.1",
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
        self.assertIn("still field an older version", panel)  # HSCM-B fields b4

    def test_baseline_staleness_and_diff(self):
        ifc = self.api("/ifcs/IFC-2.1")
        a, b = ifc["baselines"][0]["id"], ifc["current_hscm"]["id"]
        page = self.get(f"/baselines/{b}")
        self.assertIn("behind:", page)
        diff = self.get(f"/baselines/{b}/diff?other={a}", HX)
        self.assertIn("added", diff)
        self.assertIn("SUITE", diff)

    def test_events_paging(self):
        first = self.get("/events", HX)
        self.assertIn('hx-trigger="revealed"', first)          # demo has more than one page
        last_id = min(e["id"] for e in self.api("/events?limit=50"))
        rest = self.get(f"/events?before={last_id}", HX)
        self.assertNotIn(f">{last_id}<", rest)
        self.assertIn("release #", self.get("/events?entity=release", HX))

    def test_not_found_is_html_outside_api(self):
        self.assertIn("CI &#39;NOPE&#39; not found", self.get("/cis/NOPE", status=404))
        self.assertEqual(self.c.get("/api/cis/NOPE").get_json(), {"error": "CI 'NOPE' not found"})


if __name__ == "__main__":
    unittest.main()

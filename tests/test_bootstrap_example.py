"""examples/bootstrap.py: creates the two CIs once, and leaves existing ones alone.  Run: python -m unittest discover -s tests"""
import importlib.util
import os
import shutil
import tempfile
import unittest
from pathlib import Path

PATH = Path(__file__).resolve().parent.parent / "examples" / "bootstrap.py"


class BootstrapExampleTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("bootstrap_example", PATH)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.tmp = tempfile.mkdtemp()
        self.mod.DATABASE = os.path.join(self.tmp, "t.db")
        self.mod.JIRA_TOKEN_FILE = Path(self.tmp) / "no-token"

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_once(self, app):
        said = []
        self.mod.bootstrap(app, said.append)
        return said

    def test_creates_then_leaves_alone(self):
        app = self.mod.make_app()
        self.assertEqual(sorted(app.config["RELEASE_SOURCES"]), ["jira", "manual"])
        first = self.run_once(app)
        self.assertIn("CI NAV-SW: created (releases from jira)", first)
        self.assertIn("  synced: 2026.01, 2026.01.01.00, 2026.01.01.01, 2026.01.02.00, 2026.01.03.00, "
                      "2026.01 snapshots, 2026.01.00.00", first)
        c = app.test_client()
        cis = {ci["name"]: ci for ci in c.get("/api/cis").get_json()}
        self.assertEqual((cis["NAV-SW"]["release_source"], cis["DISP-SW"]["release_source"]), ("jira", "manual"))
        self.assertEqual(len(c.get("/api/cis/DISP-SW").get_json()["cscs"]), 3)
        self.assertEqual(sorted(r["name"] for r in c.get("/api/cis/DISP-SW/releases").get_json()),
                         ["2026.01", "2026.01 snapshots"])
        c.patch("/api/cis/NAV-SW", json={"description": "changed by hand"})
        again = self.run_once(app)
        self.assertFalse([line for line in again if "created" in line or "synced" in line], again)
        self.assertEqual(c.get("/api/cis/NAV-SW").get_json()["description"], "changed by hand")   # not overwritten
        self.assertEqual(c.get("/cis/DISP-SW").status_code, 200)


if __name__ == "__main__":
    unittest.main()

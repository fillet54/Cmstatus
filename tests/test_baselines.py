"""IFC and baseline editing by hand, baseline lineage, and the lineage graphs.  Run: python -m unittest discover -s tests"""
import os
import shutil
import sqlite3
import tempfile
import unittest

from cmtrack import create_app, db, graph
from cmtrack.demo import seed


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.app = create_app({"DATABASE": os.path.join(self.tmp, "t.db")})
        self.c = self.app.test_client()
        seed(self.c)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def api(self, path):
        return self.c.get("/api" + path).get_json()

    def post(self, path, data, status=303):
        r = self.c.post(path, data=data)
        self.assertEqual(r.status_code, status, r.get_data(as_text=True)[:500])
        return r

    def baseline(self, ifc, name):
        return next(b for b in self.api(f"/ifcs/{ifc}")["baselines"] if b["name"] == name)

    def test_ifc_forms(self):
        r = self.post("/ifcs", {"name": "IFC-3", "parent": "", "description": "Sensors"})
        self.assertTrue(r.headers["Location"].endswith("/ifcs/IFC-3"))
        self.post("/ifcs/IFC-3/edit", {"parent": "IFC-2", "description": "Sensor increment"})
        ifc = self.api("/ifcs/IFC-3")
        self.assertEqual((ifc["ancestors"], ifc["description"]), (["IFC-2"], "Sensor increment"))
        self.assertEqual(self.c.post("/ifcs/IFC-2/edit", data={"parent": "IFC-3"}).status_code, 400)   # a cycle
        self.post("/ifcs/IFC-3/edit", {"parent": ""})
        self.assertEqual(self.api("/ifcs/IFC-3")["ancestors"], [])

    def test_draft_from_a_baseline_and_approval(self):
        a, b = self.baseline("IFC-2.1", "HSCM-A"), self.baseline("IFC-2.1", "HSCM-B")
        self.assertEqual(b["derived_from_id"], a["id"])                     # clone records the lineage
        r = self.post("/ifcs/IFC-2.1/baselines", {"name": "HSCM-C", "from": b["id"]})
        c = self.api("/baselines/" + r.headers["Location"].rsplit("/", 1)[1])
        self.assertEqual((c["derived_from"], c["supersedes"], c["status"]), ("HSCM-B", None, "draft"))
        self.assertEqual(len(c["entries"]), len(self.api(f"/baselines/{b['id']}")["entries"]))

        # edit entries: NAV-SW is behind the emergency; bring it up to date, drop the antenna, add display
        page = self.c.get(f"/baselines/{c['id']}").get_data(as_text=True)
        self.assertIn("Bring 1 up to date", page)
        self.post(f"/baselines/{c['id']}/refresh", {})
        self.post(f"/baselines/{c['id']}/entries/ANTENNA/remove", {})
        self.post(f"/baselines/{c['id']}/entries", {"ci": "DISPLAY-SW", "version": "3.2.0"})
        entries = {e["ci"]: e["version"] for e in self.api(f"/baselines/{c['id']}")["entries"]}
        self.assertEqual(entries["NAV-SW"], "2026.Q4.ER1")
        self.assertNotIn("ANTENNA", entries)
        self.assertEqual(entries["DISPLAY-SW"], "3.2.0")

        # an unreleased version blocks approval
        self.post(f"/baselines/{c['id']}/entries", {"ci": "NAV-SW", "version": "2027.Q1-b1"})
        self.assertEqual(self.c.post(f"/baselines/{c['id']}/approve").status_code, 400)
        self.post(f"/baselines/{c['id']}/entries", {"ci": "NAV-SW", "version": "2026.Q4.ER1"})
        self.post(f"/baselines/{c['id']}/approve", {})
        c = self.api(f"/baselines/{c['id']}")
        self.assertEqual((c["status"], c["supersedes"]), ("approved", "HSCM-B"))   # set on approval
        self.assertEqual(self.api(f"/baselines/{b['id']}")["status"], "superseded")
        self.assertEqual(self.c.post(f"/baselines/{c['id']}/entries/NAV-SW/remove").status_code, 400)  # frozen

    def test_branches_and_discard(self):
        a = self.baseline("IFC-2.1", "HSCM-A")
        r = self.post(f"/baselines/{a['id']}/branch", {"name": "HSCM-A.1"})
        a1 = int(r.headers["Location"].rsplit("/", 1)[1])
        self.post(f"/baselines/{a1}/branch", {"name": "HSCM-A.1.1"})
        self.assertEqual([d["name"] for d in self.api(f"/baselines/{a['id']}")["derivations"]], ["HSCM-B", "HSCM-A.1"])
        self.assertEqual(self.c.post(f"/baselines/{a1}/discard").status_code, 400)   # something built on it
        leaf = self.baseline("IFC-2.1", "HSCM-A.1.1")["id"]
        r = self.post(f"/baselines/{leaf}/discard", {})
        self.assertTrue(r.headers["Location"].endswith("/ifcs/IFC-2.1"))
        self.assertEqual(self.c.get(f"/api/baselines/{leaf}").status_code, 404)
        self.assertEqual(self.c.post("/api/ifcs/IFC-2.2/baselines", json={"name": "X", "from": a["id"]}).status_code, 400)

        page = self.c.get("/ifcs/IFC-2.1").get_data(as_text=True)
        self.assertIn('class="ui-graph"', page)
        self.assertEqual(page.count('<li class="ui-graph__row'), 3)                       # A, B, A.1

    def test_import_hscm_form(self):
        self.post("/ifcs/IFC-2.1/hscm", {"name": "HSCM-S", "source_ref": "DOC-002", "approve": "1",
                                         "csv": "ci,version,type\nNAV-SW,2026.Q4.ER1,CSCI\nNEW-HW,Rev A,HWCI\n"})
        s = self.baseline("IFC-2.1", "HSCM-S")
        self.assertEqual((s["status"], s["derived_from_id"]), ("approved", self.baseline("IFC-2.1", "HSCM-B")["id"]))
        self.assertEqual(self.api("/cis/NEW-HW")["managed"], 0)
        self.post("/ifcs/IFC-2.1/hscm", {"name": "HSCM-T", "csv": "ci,version\nNAV-SW,9.9\n"})
        t = self.baseline("IFC-2.1", "HSCM-T")
        self.assertEqual(t["status"], "draft")
        self.assertIn("not in tracker", self.c.get(f"/baselines/{t['id']}").get_data(as_text=True))

    def test_version_options_and_lineage_page(self):
        opts = self.c.get("/fragments/version-options?ci=NAV-SW").get_data(as_text=True)
        self.assertIn("2026.Q4.ER1 (released)", opts)
        self.assertNotIn("2026.Q4-b3", opts)                                          # rejected
        page = self.c.get("/cis/NAV-SW/lineage").get_data(as_text=True)
        self.assertIn('class="ui-graph"', page)
        self.assertIn("ui-graph__merge", page)                                        # Q1-b1 merges ER1
        rows = page.split('class="ui-graph__rows"')[1]
        self.assertLess(rows.index("2027.Q2-b3"), rows.index("2026.Q4-b1"))           # newest first

    def test_migration_backfills_lineage(self):
        path = os.path.join(self.tmp, "old.db")
        old = db.SCHEMA.read_text().replace(
            "    derived_from_id INTEGER REFERENCES baseline(id),  -- lineage: the baseline this one started from (NULL = from scratch)\n", "")
        self.assertNotIn("derived_from_id", old)
        conn = sqlite3.connect(path)
        conn.executescript(old)
        conn.execute("INSERT INTO ifc (id, name) VALUES (1, 'I')")
        conn.execute("INSERT INTO baseline (id, ifc_id, name) VALUES (1, 1, 'A')")
        conn.execute("INSERT INTO baseline (id, ifc_id, name, supersedes_id) VALUES (2, 1, 'B', 1)")
        conn.commit()
        conn.close()
        conn = db.connect(path)
        db.init_db(conn)
        self.assertEqual([tuple(r) for r in conn.execute("SELECT id, derived_from_id FROM baseline ORDER BY id")],
                         [(1, None), (2, 1)])
        conn.close()


class GraphLayoutTests(unittest.TestCase):
    def test_lanes(self):
        nodes = [{"id": "q1b2", "parents": ["q1b1"]}, {"id": "p1", "parents": ["q4b4"]},
                 {"id": "q1b1", "parents": ["q4b4", "er1"]}, {"id": "er1", "parents": ["q4b4"]},
                 {"id": "q4b4", "parents": ["gone"]}]
        g = graph.layout(nodes)
        lanes = {n["id"]: n["lane"] for n in g["nodes"]}
        self.assertEqual(lanes, {"q1b2": 0, "p1": 1, "q1b1": 0, "er1": 2, "q4b4": 0})
        self.assertEqual(len(g["edges"]), 5)                                          # 'gone' is not drawn
        self.assertEqual(sum(e["merge"] for e in g["edges"]), 1)
        self.assertEqual(g["width"], 3 * graph.LANE)

    def test_newest_first_keeps_children_above_parents(self):
        nodes = [{"id": 1, "parents": [], "t": 3}, {"id": 2, "parents": [1], "t": 1}, {"id": 3, "parents": [], "t": 2}]
        order = [n["id"] for n in graph.newest_first(nodes, key=lambda n: n["t"])]
        self.assertEqual(order, [2, 1, 3])


if __name__ == "__main__":
    unittest.main()

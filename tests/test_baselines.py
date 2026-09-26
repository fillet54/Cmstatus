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
        b2 = self.baseline("IFC-1", "Build 2")["id"]
        r = self.post("/ifcs", {"name": "IFC-3", "spawned_from": b2, "description": "Sensors"})
        self.assertTrue(r.headers["Location"].endswith("/ifcs/IFC-3"))
        self.post("/ifcs/IFC-3/edit", {"spawned_from": b2, "description": "Sensor increment"})
        ifc = self.api("/ifcs/IFC-3")
        self.assertEqual((ifc["spawned_from"]["name"], ifc["ancestors"], ifc["description"]),
                         ("Build 2", ["IFC-1"], "Sensor increment"))
        self.assertEqual([c["name"] for c in self.api("/ifcs/IFC-1")["spawned"]], ["IFC-2", "IFC-3"])

        # Build 1 starts from the spawn point; re-pointing the spawn point re-points Build 1's lineage
        self.post("/ifcs/IFC-3/baselines", {"name": ""})
        self.assertEqual(self.baseline("IFC-3", "Build 1")["derived_from_id"], b2)
        b1 = self.baseline("IFC-1", "Build 1")["id"]
        self.post("/ifcs/IFC-3/edit", {"spawned_from": b1})
        self.assertEqual(self.baseline("IFC-3", "Build 1")["derived_from_id"], b1)

        # no cycles: IFC-1 can't spawn from an HSCM of an IFC that descends from it
        self.post(f"/baselines/{self.baseline('IFC-3', 'Build 1')['id']}/approve", {})
        self.assertEqual(self.c.post("/ifcs/IFC-1/edit",
                                     data={"spawned_from": self.baseline("IFC-3", "Build 1")["id"]}).status_code, 400)
        page = self.c.get("/ifcs/IFC-1").get_data(as_text=True)
        self.assertNotIn("IFC-3 Build 1", page.split('name="spawned_from"')[1].split("</select>")[0])

    def test_builds_approve_final_reopen(self):
        d1 = self.baseline("IFC-2", "Build 1")
        self.assertEqual(d1["status"], "draft")
        page = self.c.get("/ifcs/IFC-2").get_data(as_text=True)
        self.assertIn("is still a draft", page)                                        # no second build yet
        self.assertEqual(self.c.post("/ifcs/IFC-2/baselines", data={}).status_code, 400)

        # NAV-SW (from IFC-1 Build 2) is behind the emergency's line? it fields ER1 already: nothing to refresh
        self.post(f"/baselines/{d1['id']}/refresh", {})
        self.post(f"/baselines/{d1['id']}/entries/ANTENNA/remove", {})
        self.post(f"/baselines/{d1['id']}/entries", {"ci": "NAV-SW", "version": "2027.Q1-b1"})
        self.assertEqual(self.c.post(f"/baselines/{d1['id']}/approve").status_code, 400)   # not released
        self.post(f"/baselines/{d1['id']}/entries", {"ci": "NAV-SW", "version": "2026.Q4.ER1"})
        self.post(f"/baselines/{d1['id']}/approve", {})
        self.assertNotIn("ANTENNA", {e["ci"] for e in self.api(f"/baselines/{d1['id']}")["entries"]})

        r = self.post("/ifcs/IFC-2/baselines", {"name": ""})                            # Build 2
        d2 = self.api("/baselines/" + r.headers["Location"].rsplit("/", 1)[1])
        self.assertEqual((d2["name"], d2["seq"], d2["derived_from"]["name"]), ("Build 2", 2, "Build 1"))
        self.post(f"/baselines/{d2['id']}/discard", {})
        self.post("/ifcs/IFC-2/final", {})
        ifc = self.api("/ifcs/IFC-2")
        self.assertEqual((ifc["final"], ifc["final_id"]), ("Build 1", d1["id"]))
        self.assertEqual(self.c.post("/ifcs/IFC-2/baselines", data={}).status_code, 400)  # closed
        self.assertIn("is closed", self.c.get("/ifcs/IFC-2").get_data(as_text=True))
        self.post("/ifcs/IFC-2/reopen", {})
        r = self.post("/ifcs/IFC-2/baselines", {"name": "HSC 2"})
        self.assertEqual(self.api("/baselines/" + r.headers["Location"].rsplit("/", 1)[1])["name"], "HSC 2")
        self.assertEqual(self.c.post(f"/baselines/{d1['id']}/entries/NAV-SW/remove").status_code, 400)  # frozen

    def test_import_hscm_form(self):
        csv = "ci,version,type\nNAV-SW,2026.Q4.ER1,CSCI\nNEW-HW,Rev A,HWCI\n"
        self.assertEqual(self.c.post("/ifcs/IFC-2/hscm", data={"csv": csv}).status_code, 400)   # draft open
        self.post("/ifcs", {"name": "IFC-3", "spawned_from": self.baseline("IFC-1", "Build 2")["id"]})
        self.post("/ifcs/IFC-3/hscm", {"source_ref": "DOC-002", "approve": "1", "csv": csv})
        s = self.baseline("IFC-3", "Build 1")
        self.assertEqual((s["status"], s["derived_from_id"]), ("approved", self.baseline("IFC-1", "Build 2")["id"]))
        self.assertEqual(self.api("/cis/NEW-HW")["managed"], 0)
        self.post("/ifcs/IFC-3/hscm", {"name": "HSC 2", "csv": "ci,version\nNAV-SW,9.9\n"})
        t = self.baseline("IFC-3", "HSC 2")
        self.assertEqual((t["status"], t["seq"]), ("draft", 2))
        self.assertIn("not in tracker", self.c.get(f"/baselines/{t['id']}").get_data(as_text=True))

    def test_lineage_views(self):
        page = self.c.get("/").get_data(as_text=True)
        self.assertEqual(page.count('class="ui-strip"'), 1)                               # one graph for all IFCs
        current = self.baseline("IFC-1", "Build 2")["id"]
        self.assertIn(f'<a href="/baselines/{current}" class="ui-strip__node is-current" aria-current="true">', page)
        self.assertEqual(page.count("is-current"), 1)                                     # IFC-2 has only a draft
        self.assertIn(">final</text>", page)
        for name in ("IFC-1", "IFC-2"):
            self.assertIn(f'ui-strip__caption', page)
            self.assertIn(f">{name}</text>", page)
        page = self.c.get("/ifcs").get_data(as_text=True)
        self.assertEqual(page.count('<li class="ui-graph__row'), 3)                       # IFC-1 B1, B2, IFC-2 B1
        page = self.c.get("/ifcs/IFC-2").get_data(as_text=True)
        self.assertIn("spawned from", page)                                               # the spawn point row
        self.assertEqual(page.count('<li class="ui-graph__row'), 2)

    def test_version_options_and_lineage_page(self):
        opts = self.c.get("/fragments/version-options?ci=NAV-SW").get_data(as_text=True)
        self.assertIn("2026.Q4.ER1 (released)", opts)
        self.assertNotIn("2026.Q4-b3", opts)                                          # rejected
        page = self.c.get("/cis/NAV-SW/lineage").get_data(as_text=True)
        self.assertIn('class="ui-graph"', page)
        self.assertIn("ui-graph__merge", page)                                        # Q1-b1 merges ER1
        rows = page.split('class="ui-graph__rows"')[1]
        self.assertLess(rows.index("2027.Q2-b3"), rows.index("2026.Q4-b1"))           # newest first

    def test_migration_to_spawned_ifcs_and_builds(self):
        path = os.path.join(self.tmp, "old.db")
        old = db.SCHEMA.read_text()
        for line in ("    spawned_from_id INTEGER REFERENCES baseline(id),", "    final_id    INTEGER REFERENCES baseline(id),",
                     "    seq           INTEGER NOT NULL DEFAULT 0,", "    derived_from_id INTEGER REFERENCES baseline(id),"):
            start = old.index(line)
            old = old[:start] + old[old.index("\n", start) + 1:]
        old = old.replace("CREATE TABLE IF NOT EXISTS ifc (\n", "CREATE TABLE IF NOT EXISTS ifc (\n    parent_id INTEGER REFERENCES ifc(id),\n")
        conn = sqlite3.connect(path)
        conn.executescript(old)
        conn.execute("INSERT INTO ifc (id, name) VALUES (1, 'P')")
        conn.execute("INSERT INTO ifc (id, name, parent_id) VALUES (2, 'C', 1)")
        conn.execute("INSERT INTO baseline (id, ifc_id, name, status) VALUES (1, 1, 'A', 'superseded')")
        conn.execute("INSERT INTO baseline (id, ifc_id, name, status, supersedes_id) VALUES (2, 1, 'B', 'approved', 1)")
        conn.execute("INSERT INTO baseline (id, ifc_id, name, status) VALUES (3, 1, 'X', 'draft')")
        conn.execute("INSERT INTO baseline (id, ifc_id, name) VALUES (4, 2, 'C1')")
        conn.commit()
        conn.close()
        conn = db.connect(path)
        db.init_db(conn)
        self.assertEqual(conn.execute("SELECT spawned_from_id FROM ifc WHERE id = 2").fetchone()[0], 2)   # P's approved
        self.assertEqual([tuple(r) for r in conn.execute("SELECT id, seq, derived_from_id FROM baseline ORDER BY id")],
                         [(1, 1, None), (2, 2, 1), (3, 3, 2), (4, 1, 2)])
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

    def test_horizontal_runs_oldest_to_newest(self):
        nodes = [{"id": "c", "parents": ["a"]}, {"id": "b", "parents": ["a"]}, {"id": "a", "parents": []}]
        g = graph.layout(nodes, horizontal=True)
        xy = {n["id"]: (n["x"], n["y"]) for n in g["nodes"]}
        self.assertLess(xy["a"][0], xy["b"][0])
        self.assertLess(xy["b"][0], xy["c"][0])                                      # newest on the right
        self.assertEqual(xy["a"][1], xy["c"][1])                                     # c continues a's lane
        self.assertGreater(xy["b"][1], xy["a"][1])                                   # b branches below
        self.assertEqual((g["width"], g["height"]), (3 * graph.COL, 2 * graph.HLANE))

    def test_swimlanes_keep_a_lane_per_line(self):
        nodes = [{"id": 1, "parents": [], "ifc": 0}, {"id": 2, "parents": [1], "ifc": 1},
                 {"id": 3, "parents": [1], "ifc": 0}, {"id": 4, "parents": [2], "ifc": 1}]
        g = graph.swimlanes(nodes, lambda n: n["ifc"], horizontal=True)
        xy = {n["id"]: (n["x"], n["y"]) for n in g["nodes"]}
        self.assertEqual([xy[i][0] for i in (1, 2, 3, 4)], sorted(xy[i][0] for i in (1, 2, 3, 4)))  # time order
        self.assertEqual((xy[1][1], xy[3][1]), (xy[1][1], xy[1][1]))
        self.assertEqual(xy[2][1], xy[4][1])
        self.assertGreater(xy[2][1], xy[1][1])
        self.assertEqual(len(g["edges"]), 3)

    def test_newest_first_keeps_children_above_parents(self):
        nodes = [{"id": 1, "parents": [], "t": 3}, {"id": 2, "parents": [1], "t": 1}, {"id": 3, "parents": [], "t": 2}]
        order = [n["id"] for n in graph.newest_first(nodes, key=lambda n: n["t"])]
        self.assertEqual(order, [2, 1, 3])


if __name__ == "__main__":
    unittest.main()

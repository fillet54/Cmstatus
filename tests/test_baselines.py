"""IFC and baseline editing by hand, baseline lineage, and the lineage graphs.  Run: python -m unittest discover -s tests"""
import datetime as dt
import os
import shutil
import tempfile
import unittest

from cmtrack import create_app, graph, history
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
        self.assertEqual(page.count('class="ui-timeline"'), 1)                            # one timeline for all IFCs
        self.assertIn(">B1<", page)                                                       # short labels
        self.assertIn(">B2<", page)
        self.assertEqual(page.count("is-current"), 0)       # IFC-1 is final (ringed, not highlighted), IFC-2 a draft
        self.assertIn("ui-graph__ring", page)
        self.assertIn(">IFC-2</text>", page)                                              # lane captions
        for zoom in ("years", "quarters", "months", "weeks"):
            frag = self.c.get(f"/fragments/ifc-timeline?zoom={zoom}").get_data(as_text=True)
            self.assertIn('id="ifc-timeline"', frag)
            self.assertNotIn("<html", frag)
        self.assertIn('class="ui-timeline is-dense"', self.c.get("/fragments/ifc-timeline?zoom=years").get_data(as_text=True))
        wide = self.c.get("/fragments/ifc-timeline?zoom=years&width=1500").get_data(as_text=True)
        self.assertIn('<svg class="ui-timeline__svg" width="1500"', wide)                  # fitted to the box
        months = self.c.get("/fragments/ifc-timeline?zoom=months&width=300").get_data(as_text=True)
        self.assertNotIn('width="300"', months)                                          # never squeezed
        self.assertIn('class="ui-timeline"', self.c.get("/ifcs").get_data(as_text=True))

    def test_version_options_and_lineage_page(self):
        opts = self.c.get("/fragments/version-options?ci=NAV-SW").get_data(as_text=True)
        self.assertIn("2026.Q4.ER1 (released)", opts)
        self.assertNotIn("2026.Q4-b3", opts)                                          # rejected
        ci = self.c.get("/cis/NAV-SW").get_data(as_text=True)
        self.assertIn('hx-get="/cis/NAV-SW/lineage" hx-trigger="toggle once"', ci)       # loaded on demand
        frag = self.c.get("/cis/NAV-SW/lineage?zoom=weeks").get_data(as_text=True)
        self.assertNotIn("<html", frag)
        self.assertIn('id="ci-lineage"', frag)
        self.assertIn("ui-graph__merge", frag)                                        # Q1-b1 merges ER1
        self.assertIn(">2026.Q4.ER1</text>", frag)                                    # the fix's lane is captioned
        self.assertIn(">ER1</text>", frag)                                            # ...and its build labelled
        self.assertLess(frag.index(">2026.Q4<"), frag.index(">2027.Q1<"))               # oldest first, left to right

    def test_backdating(self):
        api = lambda m, p, d=None: getattr(self.c, m)("/api" + p, json=d)
        api("post", "/ifcs", {"name": "IFC-9"})
        rows = [{"ci": "NAV-SW", "version": "2026.Q4-b4"}]
        r = api("post", "/ifcs/IFC-9/hscm", {"rows": rows, "date": "2021-05-01"})
        self.assertEqual(r.get_json()["baseline"]["approved_at"], "2021-05-01T00:00:00+00:00")
        r = api("post", "/ifcs/IFC-9/hscm", {"rows": rows, "date": "2021-06-01", "approve": False})
        b2 = r.get_json()["baseline"]
        self.assertTrue(b2["created_at"].startswith("2021-06-01"))
        self.assertEqual(api("post", f"/baselines/{b2['id']}/approve", {"approved_at": "2021-04-01"}).status_code, 400)
        future = (dt.date.today() + dt.timedelta(days=30)).isoformat()
        self.assertEqual(api("post", f"/baselines/{b2['id']}/approve", {"approved_at": future}).status_code, 400)
        self.assertEqual(api("post", f"/baselines/{b2['id']}/approve", {"approved_at": "2021-07-02"}).get_json()
                         ["approved_at"], "2021-07-02T00:00:00+00:00")

    def test_history_generate_and_load(self):
        data = history.generate(seed=3, today=dt.date(2024, 1, 1))
        self.assertTrue(all(e["at"] <= "2024-01-01" for e in data["events"]))
        names = {i["name"] for i in data["ifcs"]}
        for i in data["ifcs"]:
            if i["spawned_from"]:
                self.assertIn(i["spawned_from"]["ifc"], names)
        path = os.path.join(self.tmp, "t.db")
        history.load(data, history.InProcess(path), out=lambda *_: None)
        ifc = self.api("/ifcs/IFC%20A%201.0.2.1")
        self.assertEqual(ifc["spawned_from"]["ifc"], "IFC A 1.0.2")
        self.assertEqual([b["name"] for b in ifc["baselines"]][:1], ["HSC1"])        # long versions: HSCs only
        self.assertEqual([b["name"] for b in self.api("/ifcs/IFC%20A%201.0")["baselines"]][:4],
                         ["Build 1", "Build 2", "Build 3", "HSC1"])
        self.assertEqual(self.c.get("/").status_code, 200)
        engine = self.api("/cis/ENGINE-SW")                                            # a managed CSCI
        self.assertEqual((engine["managed"], engine["release_source"], len(engine["cscs"])), (1, None, 2))
        released = [r for r in engine["releases"] if r["status"] == "released"]
        self.assertTrue(any(r["kind"] == "planned" for r in released))
        self.assertIn('class="ui-timeline', self.c.get("/cis/ENGINE-SW/lineage").get_data(as_text=True))

class GraphLayoutTests(unittest.TestCase):
    def test_timeline(self):
        # a: 1-2-7-5 (7 merges b's 4 back in); b: 3-4 off 2; c: 6 off 5, reusing b's lane once b's merge is done
        nodes = [{"id": 1, "name": "Build 1", "parents": [], "g": "a", "date": "2020-01-01", "caption": "A"},
                 {"id": 2, "name": "HSC1", "parents": [1], "g": "a", "date": "2020-03-01"},
                 {"id": 3, "name": "HSC1", "parents": [2], "g": "b", "date": "2020-04-01", "caption": "B"},
                 {"id": 4, "name": "HSC1.1", "parents": [3], "g": "b", "date": "2020-06-01"},
                 {"id": 7, "name": "HSC1.2", "parents": [2, 4], "g": "a", "date": "2020-07-01"},
                 {"id": 5, "name": "HSC1.3", "parents": [7], "g": "a", "date": "2021-05-01"},
                 {"id": 6, "name": "HSC1", "parents": [5], "g": "c", "date": "2021-06-01", "caption": "C", "label": "c1"}]
        t = graph.timeline(nodes, lambda n: n["g"], dt.date(2021, 7, 1), 1.0)
        at = {n["id"]: n for n in t["nodes"]}
        self.assertEqual([at[i]["label"] for i in (1, 2, 4, 6)], ["B1", "H1", "H1.1", "c1"])
        self.assertEqual(at[2]["x"] - at[1]["x"], 60)                                   # 60 days at 1 px/day
        self.assertEqual((at[1]["lane"], at[3]["lane"], at[6]["lane"]), (0, 1, 1))     # c reuses b's lane
        merge = [e for e in t["edges"] if e["merge"]]
        self.assertEqual(len(merge), 1)                                                # 4 merges into 7...
        self.assertTrue(merge[0]["d"].startswith(f"M{at[4]['x']} {at[4]['y']} L"))    # ...along b's lane first
        self.assertEqual(t["today_x"] - at[6]["x"], 30)
        self.assertEqual([c["text"] for c in t["captions"]], ["A", "B", "C"])
        self.assertEqual(len(t["edges"]), 7)
        self.assertIn("2021", [k["label"] for k in t["ticks"] if k["major"]])
        self.assertEqual(graph.short_label("Build 12"), "B12")
        self.assertEqual(graph.short_label("Delta drop"), "Delta d")

    def test_newest_first_keeps_children_above_parents(self):
        nodes = [{"id": 1, "parents": [], "t": 3}, {"id": 2, "parents": [1], "t": 1}, {"id": 3, "parents": [], "t": 2}]
        order = [n["id"] for n in graph.newest_first(nodes, key=lambda n: n["t"])]
        self.assertEqual(order, [2, 1, 3])


if __name__ == "__main__":
    unittest.main()

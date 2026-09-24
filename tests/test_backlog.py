"""Lexorank and shared backlogs.  Run: python -m unittest discover -s tests"""
import os
import random
import shutil
import tempfile
import unittest

from cmtrack import create_app
from cmtrack.demo import DEMO_TICKETS, seed
from cmtrack.rank import RankError, between, spread, validate
from cmtrack.tickets import StaticSource, TicketSource

HERE = os.path.dirname(os.path.abspath(__file__))
B = "/backlogs/Nav & Display"


class RankTests(unittest.TestCase):
    def test_between_orders_and_never_collides(self):
        for seed_ in range(3):
            rng, items = random.Random(seed_), []
            for _ in range(2000):
                i = rng.randint(0, len(items))
                lo, hi = (items[i - 1] if i else None), (items[i] if i < len(items) else None)
                r = validate(between(lo, hi))
                self.assertTrue((lo is None or lo < r) and (hi is None or r < hi), (lo, r, hi))
                items.insert(i, r)
            self.assertEqual(items, sorted(items))
            self.assertEqual(len(set(items)), len(items))

    def test_appends_and_prepends_stay_short(self):
        bottom, top = [], []
        for _ in range(300):
            bottom.append(between(bottom[-1] if bottom else None, None))
            top.insert(0, between(None, top[0] if top else None))
        self.assertEqual((bottom, top), (sorted(bottom), sorted(top)))
        self.assertLessEqual(max(map(len, bottom + top)), 10)

    def test_edges_and_errors(self):
        self.assertEqual(between("a", "b"), "ai")
        self.assertLess(between(None, "01"), "01")
        self.assertGreater(between("z", None), "z")
        for bad in (("b", "a"), ("a", "a"), ("A", None), (None, "a0"), ("", None)):
            with self.assertRaises(RankError):
                between(*bad)

    def test_spread(self):
        s = spread(500)
        self.assertEqual((s, len(set(s))), (sorted(s), 500))
        self.assertTrue(all(validate(r) for r in s))
        self.assertEqual(spread(0), [])


class BacklogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.source = StaticSource(DEMO_TICKETS, name="jira")
        self.app = create_app({"DATABASE": os.path.join(self.tmp, "t.db"),
                               "TICKET_SOURCES": {"jira": self.source}})
        self.c = self.app.test_client()
        seed(self.c)       # creates "Nav & Display", pulls, moves PRG-20 to the top and PRG-15 to the bottom

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def call(self, method, path, json=None, status=200):
        r = getattr(self.c, method)("/api" + path, json=json)
        self.assertEqual(r.status_code, status, r.get_json())
        return r.get_json()

    def order(self):
        return [i["key"] for i in self.call("get", B)["items"]]

    def test_pull_is_scoped_and_idempotent(self):
        b = self.call("get", B)
        self.assertEqual(self.order(), ["PRG-20", "PRG-10", "PRG-12", "PRG-18", "PRG-21", "PRG-22", "PRG-15"])
        self.assertNotIn("PRG-23", self.order())                       # RADAR-SW only: not this backlog's CIs
        self.assertEqual((b["teams"], b["cis"]), (["Nav", "Maps", "Display"], ["DISPLAY-SW", "NAV-SW"]))
        first = b["items"][0]
        self.assertEqual((first["state"], first["cis"], first["position"]),
                         ("analysis_required", ["NAV-SW", "DISPLAY-SW"], 1))
        self.assertEqual(self.call("post", B + "/pull"), {"added": [], "offered": 7, "already": 7})
        next(r for r in self.source.records if r.key == "PRG-23").cis = ["NAV-SW"]   # the source changes its mind
        self.assertEqual(self.call("post", B + "/pull")["added"], ["PRG-23"])
        self.assertEqual(self.order()[-1], "PRG-23")

    def test_move_by_neighbours(self):
        ranks = lambda: {i["key"]: i["rank"] for i in self.call("get", B)["items"]}
        before = ranks()
        out = self.call("post", B + "/items/PRG-21/move", {"after": "PRG-20", "before": "PRG-10"})   # drag & drop
        self.assertEqual(self.order()[:3], ["PRG-20", "PRG-21", "PRG-10"])
        after = ranks()
        self.assertEqual({k for k in before if before[k] != after[k]}, {"PRG-21"})    # only the moved item changed
        self.assertEqual(out["rank"], after["PRG-21"])
        self.call("post", B + "/items/PRG-12/move", {"before": "PRG-20"})               # to the top
        self.assertEqual(self.order()[0], "PRG-12")
        self.call("post", B + "/items/PRG-12/move", {"after": "PRG-15"})                # to the bottom
        self.assertEqual(self.order()[-1], "PRG-12")
        self.call("post", B + "/items/PRG-18/move", {"after": "PRG-20"})                # just below an item
        self.assertEqual(self.order()[:3], ["PRG-20", "PRG-18", "PRG-21"])

    def test_move_errors(self):
        err = self.call("post", B + "/items/PRG-21/move", {"after": "PRG-10", "before": "PRG-20"}, 409)
        self.assertIn("reload", err["error"])                                            # stale neighbours
        self.call("post", B + "/items/PRG-21/move", {"after": "PRG-21"}, 400)
        self.call("post", B + "/items/PRG-21/move", {}, 400)
        self.call("post", B + "/items/NOPE-1/move", {"after": "PRG-10"}, 404)
        self.call("post", B + "/items/PRG-21/move", {"after": "NOPE-1"}, 404)

    def test_add_remove(self):
        self.call("post", B + "/items/PRG-12", status=405)
        self.assertEqual(self.call("post", B + "/items", {"key": "PRG-23", "position": "top"}, 201)["key"], "PRG-23")
        self.assertEqual(self.order()[0], "PRG-23")
        self.call("post", B + "/items", {"key": "PRG-23"}, 409)                          # already there
        err = self.call("post", B + "/items", {"key": "NAVL-105"}, 400)
        self.assertIn("CSC ticket under PRG-10", err["error"])
        self.call("post", B + "/items", {"key": "NOPE-1"}, 404)
        self.call("post", B + "/items", {"key": "PRG-23", "position": "middle"}, 400)
        self.call("delete", B + "/items/PRG-23")
        self.call("delete", B + "/items/PRG-23", status=404)
        self.assertNotIn("PRG-23", self.order())

    def test_rebalance_keeps_order(self):
        for _ in range(20):                                  # keep dropping right below PRG-20: the gap halves each time
            self.call("post", B + "/items/PRG-21/move", {"after": "PRG-20"})
            self.call("post", B + "/items/PRG-22/move", {"after": "PRG-20"})
        order, long_before = self.order(), self.call("get", B)["max_rank_length"]
        added = {i["key"]: i["added_at"] for i in self.call("get", B)["items"]}
        out = self.call("post", B + "/rebalance")
        self.assertEqual(self.order(), order)
        self.assertLess(out["max_rank_length"], long_before)
        self.assertEqual({i["key"]: i["added_at"] for i in self.call("get", B)["items"]}, added)

    def test_ticket_data_is_live(self):
        next(r for r in self.source.records if r.key == "PRG-21").state = "done"
        self.assertEqual(next(i for i in self.call("get", B)["items"] if i["key"] == "PRG-21")["state"], "done")
        self.source.records = [r for r in self.source.records if r.key != "PRG-21"]    # deleted in Jira
        gone = next(i for i in self.call("get", B)["items"] if i["key"] == "PRG-21")
        self.assertEqual((gone["state"], gone["state_reason"]), ("error", "not found in the ticket source"))

    def test_backlog_crud(self):
        b = self.call("post", "/backlogs", {"name": "Radar", "teams": "Radar, RF", "cis": ["RADAR-SW"]}, 201)
        self.assertEqual((b["teams"], b["cis"], b["item_count"]), (["Radar", "RF"], ["RADAR-SW"], 0))
        self.call("post", "/backlogs", {"name": "Radar"}, 409)
        self.call("post", "/backlogs", {"name": "123"}, 400)
        self.call("post", "/backlogs", {"name": "x", "cis": ["NOPE"]}, 404)
        self.assertEqual(self.call("post", "/backlogs/Radar/pull")["added"], ["PRG-23"])
        b = self.call("patch", "/backlogs/Radar", {"name": "Radar & RF", "cis": []})
        self.assertEqual((b["name"], b["cis"], b["item_count"]), ("Radar & RF", [], 1))
        self.assertEqual([x["name"] for x in self.call("get", "/backlogs?ci=NAV-SW")], ["Nav & Display"])

    def test_source_without_pull_support(self):
        class KeysOnly(TicketSource):
            name = "jira"
            def get_tickets(self, keys):
                return []
        self.app.config["TICKET_SOURCES"] = {"jira": KeysOnly()}
        err = self.call("post", B + "/pull", status=400)
        self.assertIn("can't list top-level tickets", err["error"])

    def test_views(self):
        page = self.c.get(B).get_data(as_text=True)
        self.assertIn('id="backlog-list"', page)
        self.assertIn('draggable="true" data-key="PRG-20"', page)
        self.assertIn("/api/backlogs/Nav%20&amp;%20Display/items/__KEY__/move", page)
        hx = {"HX-Request": "true"}
        frag = self.c.get(B, headers=hx).get_data(as_text=True)
        self.assertNotIn("<html", frag)
        r = self.c.post(B + "/add", data={"key": "prg-23", "position": "top"}, headers=hx).get_data(as_text=True)
        self.assertIn("Added PRG-23 at the top", r)
        self.assertLess(r.index('data-key="PRG-23"'), r.index('data-key="PRG-20"'))
        self.assertIn("already in", self.c.post(B + "/add", data={"key": "PRG-23"}, headers=hx).get_data(as_text=True))
        self.assertIn("Removed PRG-23", self.c.post(B + "/items/PRG-23/remove", headers=hx).get_data(as_text=True))
        self.assertIn("Nothing new", self.c.post(B + "/pull", headers=hx).get_data(as_text=True))
        self.assertIn("Re-spaced 7 ranks", self.c.post(B + "/rebalance", headers=hx).get_data(as_text=True))
        # create through the form: htmx gets a redirect header, plain posts a 303
        r = self.c.post("/backlogs", data={"name": "Radar", "teams": "Radar", "cis": ["NAV-SW"]}, headers=hx)
        self.assertEqual((r.status_code, r.headers["HX-Redirect"]), (204, "/backlogs/Radar"))
        r = self.c.post("/backlogs", data={"name": "Radar"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("already exists", r.get_data(as_text=True))
        self.assertIn("Nav &amp; Display", self.c.get("/cis/NAV-SW").get_data(as_text=True))
        # the ticket source failing doesn't take the page down
        self.app.config["TICKET_SOURCES"] = {"jira": type("Down", (TicketSource,), {
            "name": "jira", "get_tickets": lambda self, keys: 1 / 0})()}
        r = self.c.get(B)
        self.assertEqual(r.status_code, 200)
        self.assertIn("read tickets:</b> ticket source &#39;jira&#39; failed in get_tickets", r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()

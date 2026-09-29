"""Backlog rank stores: every backlog test again with the order kept in a Jira custom field, Jira-specific
behaviour (ties, junk values, failures), and the REST client against a local stand-in for Jira.
Run: python -m unittest discover -s tests"""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from cmtrack.backlog.stores import JiraRankStore, JiraRestClient, MemoryJira, StoreError
import test_backlog                                  # a module import, so its tests aren't collected twice

B = test_backlog.B

FIELD = "customfield_10500"


class JiraBacklogTests(test_backlog.BacklogTests):
    """The whole BacklogTests suite, after moving "Nav & Display" from SQLite into a Jira field."""

    def setUp(self):
        self.jira = MemoryJira()
        super().setUp()
        self.app.config["BACKLOG_STORES"] = {"jira": JiraRankStore(self.jira)}
        before = self.order()
        b = self.call("patch", B, {"store": "jira", "store_params": {"rank_field": FIELD}})
        self.assertEqual((b["store"], b["store_params"], b["item_count"]), ("jira", {"rank_field": FIELD}, None))
        self.assertEqual(self.order(), before)                                    # moved with its ranks
        self.assertEqual(len(self.jira.find(FIELD)), 7)
        self.assertEqual(self.app.test_client().get("/api/backlogs").get_json()[0]["store"], "jira")

    def test_rebalance_keeps_order(self):
        # SQLite keeps added_at; a Jira field has only the rank, so this checks order and length only
        for _ in range(20):
            self.call("post", B + "/items/PRG-21/move", {"after": "PRG-20"})
            self.call("post", B + "/items/PRG-22/move", {"after": "PRG-20"})
        order, long_before = self.order(), self.call("get", B)["max_rank_length"]
        out = self.call("post", B + "/rebalance")
        self.assertEqual(self.order(), order)
        self.assertLess(out["max_rank_length"], long_before)

    def test_ties_and_junk_in_the_field(self):
        ranks = {i["key"]: i["rank"] for i in self.call("get", B)["items"]}
        self.jira.issues["PRG-12"][FIELD] = ranks["PRG-10"]              # two moves at once: a shared rank
        self.jira.issues["PRG-18"][FIELD] = "Top priority!"              # someone typed in the field
        view = self.call("get", B)
        self.assertEqual(self.order()[-1], "PRG-18")                      # invalid sorts last
        self.assertEqual(self.order()[1:3], ["PRG-10", "PRG-12"])         # tie broken by key
        self.assertTrue(any("PRG-18 has no valid rank" in w for w in view["warnings"]))
        self.assertTrue(any("share a rank" in w for w in view["warnings"]))
        err = self.call("post", B + "/items/PRG-21/move", {"after": "PRG-10", "before": "PRG-12"}, 409)
        self.assertIn("share a rank", err["error"])
        self.call("post", B + "/items/PRG-21/move", {"after": "PRG-18"}, 400)   # neighbour without a rank
        self.assertIn("Rebalance ranks", self.c.get(B).get_data(as_text=True))  # the page offers the fix
        self.call("post", B + "/rebalance")
        view = self.call("get", B)
        self.assertEqual(view["warnings"], [])
        self.assertEqual(len({i["rank"] for i in view["items"]}), 7)

    def test_jira_failure_is_reported(self):
        def down(*a):
            raise ConnectionError("jira is down")
        self.jira.find = down
        self.assertIn("jira is down", self.call("get", B, status=502)["error"])
        page = self.c.get(B)
        self.assertEqual(page.status_code, 200)                           # the page shows it, doesn't crash
        self.assertIn("jira is down", page.get_data(as_text=True))

    def test_jira_unreachable(self):
        from cmtrack.backlog.stores import JiraRestClient
        self.app.config["BACKLOG_STORES"]["jira"] = JiraRankStore(JiraRestClient("http://127.0.0.1:9", token="t"))
        self.assertIn("backlog store", self.call("get", B, status=502)["error"])        # connection refused -> 502

    def test_store_params(self):
        self.call("post", "/backlogs", {"name": "X", "store": "jira"}, 400)                          # no field
        self.call("post", "/backlogs", {"name": "X", "store": "jira", "store_params": {"rank_field": "Rank"}}, 400)
        self.call("post", "/backlogs", {"name": "X", "store": "nope"}, 400)
        err = self.call("post", "/backlogs", {"name": "X", "store": "jira", "store_params": {"rank_field": FIELD}}, 409)
        self.assertIn("each backlog needs its own field", err["error"])
        x = self.call("post", "/backlogs", {"name": "X", "store": "jira",
                                           "store_params": {"rank_field": "customfield_10501", "scope": "project = PRG"}}, 201)
        self.assertEqual(x["store_params"], {"rank_field": "customfield_10501", "scope": "project = PRG"})
        self.call("post", "/backlogs", {"name": "Y", "store_params": {"rank_field": FIELD}}, 400)   # sqlite takes none

    def test_move_back_to_sqlite(self):
        order = self.order()
        self.call("patch", B, {"store": "sqlite", "store_params": {}})
        self.assertEqual(self.order(), order)
        self.assertEqual(self.jira.find(FIELD), [])                       # the Jira field is cleared
        self.assertEqual(self.call("get", "/backlogs")[0]["item_count"], 7)


class FakeJiraServer(BaseHTTPRequestHandler):
    """Just enough of Jira's REST API v2 for JiraRestClient: search (paged) and issue update."""
    issues, requests = {}, []

    def log_message(self, *a):
        pass

    def _send(self, code, body=None):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        if body is not None:
            self.wfile.write(json.dumps(body).encode())

    def _body(self):
        return json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")

    def do_POST(self):
        body = self._body()
        self.requests.append(("POST", self.path, body, self.headers["Authorization"]))
        field = body["fields"][0]
        hits = [(k, f) for k, f in sorted(self.issues.items()) if f.get(field)]
        page = hits[body["startAt"]:body["startAt"] + body["maxResults"]]
        self._send(200, {"total": len(hits), "issues": [{"key": k, "fields": {field: f[field]}} for k, f in page]})

    def do_PUT(self):
        body = self._body()
        self.requests.append(("PUT", self.path, body, self.headers["Authorization"]))
        key = self.path.rsplit("/", 1)[1]
        if key not in self.issues:
            return self._send(404, {"errorMessages": ["Issue does not exist"]})
        self.issues[key].update(body["fields"])
        self._send(204)


class JiraRestClientTests(unittest.TestCase):
    def setUp(self):
        FakeJiraServer.issues = {f"PRG-{n}": {} for n in range(1, 6)}
        FakeJiraServer.requests = []
        self.server = HTTPServer(("127.0.0.1", 0), FakeJiraServer)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_find_pages_and_set(self):
        client = JiraRestClient(self.url, token="t0k", page_size=2)
        for n, r in ((1, "i"), (3, "c"), (4, "x")):
            client.set(f"PRG-{n}", FIELD, r)
        self.assertEqual(sorted(client.find(FIELD, "project = PRG")), [("PRG-1", "i"), ("PRG-3", "c"), ("PRG-4", "x")])
        searches = [r for r in FakeJiraServer.requests if r[0] == "POST"]
        self.assertEqual(len(searches), 2)                                             # 3 hits, pages of 2
        self.assertEqual(searches[0][2]["jql"], "cf[10500] is not EMPTY AND (project = PRG)")
        self.assertEqual(searches[0][3], "Bearer t0k")
        put = next(r for r in FakeJiraServer.requests if r[0] == "PUT")
        self.assertEqual((put[1], put[2]), ("/rest/api/2/issue/PRG-1", {"fields": {FIELD: "i"}}))
        client.set("PRG-1", FIELD, None)                                               # clearing = removing
        self.assertEqual([k for k, _ in client.find(FIELD)], ["PRG-3", "PRG-4"])

    def test_errors_and_auth(self):
        client = JiraRestClient(self.url, user="svc", password="pw")
        with self.assertRaises(StoreError) as e:
            client.set("NOPE-1", FIELD, "i")
        self.assertIn("HTTP 404", str(e.exception))
        self.assertTrue(FakeJiraServer.requests[-1][3].startswith("Basic "))
        with self.assertRaises(ValueError):
            JiraRestClient(self.url)                                                   # no credentials

    def test_store_over_rest(self):
        store = JiraRankStore(JiraRestClient(self.url, token="t"))
        backlog = {"id": 1, "name": "B", "store_params": {"rank_field": FIELD}}
        store.add(None, backlog, [("PRG-2", "m"), ("PRG-5", "c")])
        store.set_rank(None, backlog, "PRG-2", "a")
        self.assertEqual(sorted((i.key, i.rank) for i in store.items(None, backlog)), [("PRG-2", "a"), ("PRG-5", "c")])
        store.remove(None, backlog, "PRG-5")
        self.assertEqual([i.key for i in store.items(None, backlog)], ["PRG-2"])


if __name__ == "__main__":
    unittest.main()

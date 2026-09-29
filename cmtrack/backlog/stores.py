"""Rank stores: where a backlog's (ticket key, rank) pairs live.

    sqlite  (default) the backlog_item table: transactional, unique ranks, remembers added_at.
    jira    a plain text custom field per backlog on each issue (no Jira app needed). An issue is on the
            backlog when the field holds a rank; removing it clears the field.
            Backlog params: {"rank_field": "customfield_12345", "scope": "<optional JQL>"}.

A store is any object with these methods (``conn`` is cmtrack's connection; ``b`` the backlog dict, with
``id`` and decoded ``store_params``):

    check_params(params) -> params     validate a backlog's store_params (raise ValueError)
    items(conn, b) -> [Item]           every item, any order (the service sorts by rank, then key)
    add(conn, b, [(key, rank), ...])   remove(conn, b, key)
    set_rank(conn, b, key, rank)       set_ranks(conn, b, {key: rank})  (a rebalance: every item)

Jira has no transactions or unique constraints: two moves at once can leave a shared rank, and anyone can type
into the field. The service sorts ties by key, puts invalid values last and offers a rebalance.
"""
import os
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

import requests


class StoreError(RuntimeError):
    """The store couldn't read or write (e.g. Jira refused). The service reports it as a 502."""


@dataclass
class Item:
    key: str
    rank: Optional[str]
    added_at: Optional[str] = None


class SqliteStore:
    def check_params(self, params):
        if params:
            raise ValueError("the sqlite store takes no parameters")
        return {}

    def items(self, conn, b):
        return [Item(*r) for r in conn.execute(
            "SELECT ticket_key, rank, added_at FROM backlog_item WHERE backlog_id = ?", (b["id"],))]

    def add(self, conn, b, pairs):
        conn.executemany("INSERT INTO backlog_item (backlog_id, ticket_key, rank) VALUES (?, ?, ?)",
                         [(b["id"], k, r) for k, r in pairs])

    def remove(self, conn, b, key):
        conn.execute("DELETE FROM backlog_item WHERE backlog_id = ? AND ticket_key = ?", (b["id"], key))

    def set_rank(self, conn, b, key, rank):
        self.set_ranks(conn, b, {key: rank})

    def set_ranks(self, conn, b, ranks):
        if len(ranks) > 1:          # first move them all out of the way: UNIQUE(backlog_id, rank)
            conn.execute("UPDATE backlog_item SET rank = '~' || ticket_key WHERE backlog_id = ?", (b["id"],))
        conn.executemany("UPDATE backlog_item SET rank = ? WHERE backlog_id = ? AND ticket_key = ?",
                         [(r, b["id"], k) for k, r in ranks.items()])


class JiraRankStore:
    """Ranks in a text custom field, through a client with two calls (JiraRestClient, or your own code):
    find(field, scope) -> [(key, value)] for issues where the field is set; set(key, field, value|None)."""

    def __init__(self, client):
        self.client = client

    def check_params(self, params):
        field, scope = str(params.get("rank_field") or ""), str(params.get("scope") or "").strip()
        if not re.fullmatch(r"customfield_\d+", field) or set(params) - {"rank_field", "scope"}:
            raise ValueError("the jira store takes rank_field (a text custom field id, customfield_12345) "
                             "and an optional scope (JQL)")
        return {"rank_field": field, **({"scope": scope} if scope else {})}

    def items(self, conn, b):
        p = b["store_params"]
        return [Item(k, (v or "").strip() or None) for k, v in self.client.find(p["rank_field"], p.get("scope"))]

    def add(self, conn, b, pairs):
        self.set_ranks(conn, b, dict(pairs))

    def remove(self, conn, b, key):
        self.client.set(key, b["store_params"]["rank_field"], None)

    def set_rank(self, conn, b, key, rank):
        self.client.set(key, b["store_params"]["rank_field"], rank)

    def set_ranks(self, conn, b, ranks):
        for key, r in ranks.items():
            self.set_rank(conn, b, key, r)


class JiraRestClient:
    """find/set over Jira's REST API v2 (Server / Data Center), with ``requests``. Auth: a personal access token
    (Bearer), or a user and password / API token (Basic). ``verify``: a CA bundle path, or False; or pass your
    own ``session``."""

    def __init__(self, url, token=None, user=None, password=None, page_size=100, verify=True, session=None):
        if not (token or user):
            raise ValueError("JiraRestClient needs a token, or a user and password")
        self.url, self.page_size = url.rstrip("/"), page_size
        self.session = session or requests.Session()
        if session is None:
            self.session.verify = verify
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        else:
            self.session.auth = (user, password or "")

    def _request(self, method, path, payload):
        r = self.session.request(method, self.url + path, json=payload, timeout=30)
        if not r.ok:
            raise StoreError(f"{method} {path} -> HTTP {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else None

    def find(self, field, scope=None):
        jql = f"cf[{field.split('_')[1]}] is not EMPTY" + (f" AND ({scope})" if scope else "")
        out = []
        while True:
            page = self._request("POST", "/rest/api/2/search", {"jql": jql, "fields": [field], "startAt": len(out),
                                                                "maxResults": self.page_size})
            out += [(i["key"], i["fields"].get(field)) for i in page["issues"]]
            if not page["issues"] or len(out) >= page["total"]:
                return out

    def set(self, key, field, value):
        self._request("PUT", f"/rest/api/2/issue/{quote(key)}", {"fields": {field: value}})


class MemoryJira:
    """An in-memory Jira for tests and demos: {key: {field: value}}."""

    def __init__(self):
        self.issues = {}

    def find(self, field, scope=None):
        return [(k, f[field]) for k, f in sorted(self.issues.items()) if f.get(field)]

    def set(self, key, field, value):
        self.issues.setdefault(key, {})[field] = value


def jira_store_from_env():
    """CMTRACK_BACKLOG_STORES=jira=cmtrack.backlog.stores:jira_store_from_env, with CMTRACK_JIRA_URL and
    CMTRACK_JIRA_TOKEN (or CMTRACK_JIRA_USER + CMTRACK_JIRA_PASSWORD)."""
    env = os.environ.get
    return JiraRankStore(JiraRestClient(env("CMTRACK_JIRA_URL"), env("CMTRACK_JIRA_TOKEN"), env("CMTRACK_JIRA_USER"),
                                        env("CMTRACK_JIRA_PASSWORD")))


def memory_jira_store():
    """Try the Jira store without Jira: CMTRACK_BACKLOG_STORES=jira=cmtrack.backlog.stores:memory_jira_store"""
    return JiraRankStore(MemoryJira())

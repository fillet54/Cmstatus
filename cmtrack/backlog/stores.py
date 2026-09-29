"""Where a backlog's membership and order are kept: a rank store.

The backlog service never touches item storage directly; it asks the backlog's store. Two stores ship:

    SqliteStore    (name "sqlite", the default) the backlog_item table in cmtrack's own database:
                   one row per item, (backlog, ticket key, rank, added_at), UNIQUE(backlog, rank).

    JiraRankStore  the ranks live in Jira, in a plain text custom field per backlog (no Jira app needed).
                   An issue is on the backlog when that field holds a rank; removing it clears the field.
                   Backlog params: {"rank_field": "customfield_12345", "scope": "<optional JQL>"}.
                   Each backlog needs its own field, so an issue can sit on several backlogs.

A store works in (key, rank) pairs; ranks are rank.py strings compared as plain strings. Every method gets
the open connection (the SQLite store uses it, the Jira store ignores it) and the backlog as a dict with
``id``, ``name`` and ``store_params``.

    class RankStore:
        def items(conn, backlog) -> [Item]        every item, any order (the service sorts)
        def add(conn, backlog, pairs)             new items: [(key, rank), ...]
        def remove(conn, backlog, key)
        def set_rank(conn, backlog, key, rank)    a move
        def set_ranks(conn, backlog, ranks)       a rebalance: {key: rank} for every item
        def check_params(params) -> params        validate a backlog's store_params

Jira has no transactions or unique constraints, so two people moving items at the same moment can leave two
items with the same rank, and someone can type anything into the field. The service copes: items are sorted
by (rank, key), invalid values sort last and are reported, and rebalancing rewrites them all.
"""
import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple


class StoreError(RuntimeError):
    """A store couldn't read or write (e.g. Jira refused the update). Reported as a 502."""


@dataclass
class Item:
    key: str
    rank: Optional[str]            # None / anything invalid: sorts last until a rebalance fixes it
    added_at: Optional[str] = None


class RankStore:
    name = "base"

    def check_params(self, params: dict) -> dict:
        return params or {}

    def items(self, conn, backlog) -> List[Item]:
        raise NotImplementedError

    def add(self, conn, backlog, pairs: List[Tuple[str, str]]) -> None:
        raise NotImplementedError

    def remove(self, conn, backlog, key: str) -> None:
        raise NotImplementedError

    def set_rank(self, conn, backlog, key: str, rank: str) -> None:
        raise NotImplementedError

    def set_ranks(self, conn, backlog, ranks: dict) -> None:
        for key, r in ranks.items():
            self.set_rank(conn, backlog, key, r)

    def describe(self, backlog) -> str:
        return self.name


# ----------------------------------------------------------------------------- SQLite

class SqliteStore(RankStore):
    """Membership and ranks in the backlog_item table (schema.sql). Everything is transactional."""

    name = "sqlite"

    def check_params(self, params):
        if params:
            raise ValueError("the sqlite store takes no parameters")
        return {}

    def items(self, conn, backlog):
        return [Item(r["ticket_key"], r["rank"], r["added_at"]) for r in conn.execute(
            "SELECT ticket_key, rank, added_at FROM backlog_item WHERE backlog_id = ?", (backlog["id"],))]

    def add(self, conn, backlog, pairs):
        conn.executemany("INSERT INTO backlog_item (backlog_id, ticket_key, rank) VALUES (?, ?, ?)",
                         [(backlog["id"], k, r) for k, r in pairs])

    def remove(self, conn, backlog, key):
        conn.execute("DELETE FROM backlog_item WHERE backlog_id = ? AND ticket_key = ?", (backlog["id"], key))

    def set_rank(self, conn, backlog, key, rank):
        conn.execute("UPDATE backlog_item SET rank = ? WHERE backlog_id = ? AND ticket_key = ?",
                     (rank, backlog["id"], key))

    def set_ranks(self, conn, backlog, ranks):
        # two steps, because UNIQUE(backlog_id, rank) would trip over ranks that are still in use
        conn.execute("UPDATE backlog_item SET rank = '~' || ticket_key WHERE backlog_id = ?", (backlog["id"],))
        conn.executemany("UPDATE backlog_item SET rank = ? WHERE backlog_id = ? AND ticket_key = ?",
                         [(r, backlog["id"], k) for k, r in ranks.items()])


# ----------------------------------------------------------------------------- Jira

class JiraFieldClient:
    """The two Jira calls the Jira store needs. Implement these with your own Jira code, or use
    JiraRestClient below.

        find(field, scope)      -> [(issue key, field value)] for issues where ``field`` is set,
                                   limited by the ``scope`` JQL when given
        set(key, field, value)  -> write the field on one issue; value None clears it
    """

    def find(self, field: str, scope: Optional[str] = None) -> Iterable[Tuple[str, Optional[str]]]:
        raise NotImplementedError

    def set(self, key: str, field: str, value: Optional[str]) -> None:
        raise NotImplementedError


class JiraRankStore(RankStore):
    """Ranks in a plain text custom field on each issue (one field per backlog)."""

    name = "jira"
    FIELD = re.compile(r"customfield_\d+")

    def __init__(self, client: JiraFieldClient, name: str = "jira"):
        self.client = client
        self.name = name

    def check_params(self, params):
        params = dict(params or {})
        field = str(params.get("rank_field") or "").strip()
        if not self.FIELD.fullmatch(field):
            raise ValueError("the jira store needs rank_field, the id of a text custom field (customfield_12345)")
        unknown = set(params) - {"rank_field", "scope"}
        if unknown:
            raise ValueError(f"unknown jira store params {sorted(unknown)}; use rank_field and scope")
        out = {"rank_field": field}
        if str(params.get("scope") or "").strip():
            out["scope"] = str(params["scope"]).strip()
        return out

    def _params(self, backlog):
        p = backlog.get("store_params") or {}
        return json.loads(p) if isinstance(p, str) else p

    def _call(self, fn, *args):
        try:
            return fn(*args)
        except StoreError:
            raise
        except Exception as e:                       # Jira is outside our control: report, don't crash
            raise StoreError(f"Jira store {self.name!r}: {e}") from e

    def items(self, conn, backlog):
        p = self._params(backlog)
        return [Item(k, (v or "").strip() or None) for k, v in self._call(self.client.find, p["rank_field"],
                                                                        p.get("scope"))]

    def add(self, conn, backlog, pairs):
        field = self._params(backlog)["rank_field"]
        for key, r in pairs:
            self._call(self.client.set, key, field, r)

    def remove(self, conn, backlog, key):
        self._call(self.client.set, key, self._params(backlog)["rank_field"], None)

    def set_rank(self, conn, backlog, key, rank):
        self._call(self.client.set, key, self._params(backlog)["rank_field"], rank)

    def describe(self, backlog):
        return f"Jira field {self._params(backlog)['rank_field']}"


class JiraRestClient(JiraFieldClient):
    """JiraFieldClient over Jira's REST API v2 (Server / Data Center), standard library only.

    Auth: a personal access token (Bearer), or a user + password/API token (Basic).
    """

    def __init__(self, base_url: str, token: str = None, user: str = None, password: str = None,
                 timeout: float = 30, page_size: int = 100):
        self.base = base_url.rstrip("/")
        if token:
            self.auth = f"Bearer {token}"
        elif user:
            self.auth = "Basic " + base64.b64encode(f"{user}:{password or ''}".encode()).decode()
        else:
            raise ValueError("JiraRestClient needs a token, or a user and password")
        self.timeout, self.page_size = timeout, page_size

    def _request(self, method, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "Authorization": self.auth, "Accept": "application/json", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            raise StoreError(f"{method} {path} -> HTTP {e.code}: {detail}") from None
        return json.loads(body) if body else None

    def find(self, field, scope=None):
        jql = f"cf[{field.split('_')[1]}] is not EMPTY" + (f" AND ({scope})" if scope else "")
        start, out = 0, []
        while True:
            page = self._request("POST", "/rest/api/2/search", {
                "jql": jql, "fields": [field], "startAt": start, "maxResults": self.page_size})
            issues = page.get("issues", [])
            out += [(i["key"], i.get("fields", {}).get(field)) for i in issues]
            start += len(issues)
            if not issues or start >= page.get("total", 0):
                return out

    def set(self, key, field, value):
        self._request("PUT", f"/rest/api/2/issue/{urllib.parse.quote(key)}", {"fields": {field: value}})


class MemoryJira(JiraFieldClient):
    """In-memory stand-in for Jira (tests and demos): {key: {field: value}}."""

    def __init__(self):
        self.issues = {}
        self.writes = 0

    def find(self, field, scope=None):
        return [(k, f[field]) for k, f in sorted(self.issues.items()) if f.get(field)]

    def set(self, key, field, value):
        self.writes += 1
        self.issues.setdefault(key, {})[field] = value


def jira_store_from_env():
    """Factory for CMTRACK_BACKLOG_STORES=jira=cmtrack.backlog.stores:jira_store_from_env.
    Reads CMTRACK_JIRA_URL and CMTRACK_JIRA_TOKEN (or CMTRACK_JIRA_USER + CMTRACK_JIRA_PASSWORD)."""
    import os
    return JiraRankStore(JiraRestClient(os.environ["CMTRACK_JIRA_URL"], os.environ.get("CMTRACK_JIRA_TOKEN"),
                                        os.environ.get("CMTRACK_JIRA_USER"), os.environ.get("CMTRACK_JIRA_PASSWORD")))


def memory_jira_store():
    """Factory for trying the Jira store without Jira: CMTRACK_BACKLOG_STORES=jira=cmtrack.backlog.stores:memory_jira_store"""
    return JiraRankStore(MemoryJira())

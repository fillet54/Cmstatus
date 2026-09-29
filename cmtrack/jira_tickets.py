"""A starting point for a Jira ticket source: top-level projects of features and discrepancies, and CSCI
projects whose tickets point at their feature/discrepancy through a custom field.

    PRG (top level)        PRG-10  Feature      "GPS-denied navigation"      Affected CIs: NAV-SW, DISPLAY-SW
                           PRG-15  Discrepancy  "Heading drift after start"
    NAVL, NAVX, DSP (CSCI) NAVL-105 Story  Parent Ticket: PRG-10   Affected Product: core   Fix Version: 2027.Q1-b1

Three layers, each usable on its own:

    JiraClient      REST API v2 (Server / Data Center) over ``requests``: paged ``search(jql, fields)`` and
                    ``fields()`` (the field list, for turning names into ids).
    Fields          register the fields you use by a short name and their Jira *name* ("Parent Ticket") or id;
                    ids are looked up once, so code and config never carry customfield numbers. ``jql(name)`` gives
                    the JQL form (cf[12345]) and ``get(issue, name)`` the plain value, whatever the field type.
    JiraTicketSource  a cmtrack TicketSource over both kinds of project. ``record(issue)`` turns an issue into a
                    TicketRecord and ``state(issue, record)`` decides its state: override either for your rules.

Every query also filters its results in Python, so text, select and issue-picker custom fields all work even where
their JQL operators differ (a text field only supports ``~``, for instance).

Configure it in code:

    source = JiraTicketSource(
        JiraClient("https://jira.example.com", token="..."),
        top_projects=["PRG"],
        fields={"parent": "Parent Ticket", "affected_product": "Affected Product", "cis": "Affected CIs"},
    )
    create_app({"TICKET_SOURCES": {"jira": source}})

or from the environment (CMTRACK_TICKET_SOURCES=jira=cmtrack.jira_tickets:from_env) with CMTRACK_JIRA_URL,
CMTRACK_JIRA_TOKEN (or CMTRACK_JIRA_USER + CMTRACK_JIRA_PASSWORD) and CMTRACK_JIRA_CONFIG, a JSON file:

    {"top_projects": ["PRG"],
     "fields": {"parent": "Parent Ticket", "affected_product": "Affected Product", "cis": "Affected CIs"},
     "status_map": {"Awaiting CCB": "blocked"}}
"""
import json
import os
from typing import Iterable, List, Optional

import requests

from .tickets import TicketRecord, TicketSource, normalize_state

BATCH = 100            # keys per "key in (...)" query


class JiraError(RuntimeError):
    pass


# ----------------------------------------------------------------------------- REST client

class JiraClient:
    """The two calls a ticket source needs, over Jira's REST API v2, with ``requests``. Auth: a personal access
    token (Bearer), or a user and password / API token (Basic). ``verify`` is passed to requests (a CA bundle
    path, or False); give your own ``session`` for proxies, client certificates and the like."""

    def __init__(self, url, token=None, user=None, password=None, page_size=100, timeout=60, verify=True, session=None):
        if not (token or user):
            raise ValueError("JiraClient needs a token, or a user and password")
        self.url, self.page_size, self.timeout = url.rstrip("/"), page_size, timeout
        self.session = session or requests.Session()
        if session is None:
            self.session.verify = verify
        self.session.headers.update({"Accept": "application/json"})
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        else:
            self.session.auth = (user, password or "")

    def _request(self, method, path, payload=None):
        r = self.session.request(method, self.url + path, json=payload, timeout=self.timeout)
        if not r.ok:
            raise JiraError(f"{method} {path} -> HTTP {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else None

    def search(self, jql, fields):
        """Every issue matching ``jql`` (all pages), with just ``fields``."""
        out = []
        while True:
            page = self._request("POST", "/rest/api/2/search", {"jql": jql, "fields": fields, "startAt": len(out),
                                                                "maxResults": self.page_size})
            out += page["issues"]
            if not page["issues"] or len(out) >= page["total"]:
                return out

    def fields(self):
        """[{"id": "customfield_10500", "name": "Parent Ticket", "custom": true, ...}, ...]"""
        return self._request("GET", "/rest/api/2/field")


# ----------------------------------------------------------------------------- fields

def plain(value):
    """A Jira field value as plain data: select {"value": x} -> x, user {"displayName": x} -> x, version or
    status {"name": x} -> x, issue {"key": x} -> x, lists element by element, everything else unchanged."""
    if isinstance(value, list):
        return [plain(v) for v in value]
    if isinstance(value, dict):
        for k in ("value", "key", "displayName", "name"):
            if k in value:
                return value[k]
    return value


class Fields:
    """Short names for the fields you use, resolved to ids once (from the client's field list):

        f = Fields(client, parent="Parent Ticket", affected_product="Affected Product")
        f.id("parent") -> "customfield_10500"    f.jql("parent") -> "cf[10500]"    f.get(issue, "parent") -> "PRG-10"

    A registered value can also be an id ("customfield_10500") or a system field ("fixVersions"). ``clause`` builds
    the JQL test for a field in the form its type supports: ``~`` for text fields, ``in (...)`` for the rest."""

    SYSTEM = {"summary", "issuetype", "status", "project", "fixVersions", "assignee", "updated", "labels", "parent"}

    def __init__(self, client, **names):
        self.client, self.names, self._ids, self._types = client, dict(names), None, {}

    def register(self, short, jira_name):
        self.names[short] = jira_name
        return self

    def id(self, short):
        name = self.names.get(short, short)
        if name.startswith("customfield_") or (name in self.SYSTEM and short not in self.names):
            return name
        if self._ids is None:
            self._ids = {}
            for f in self.client.fields():
                self._ids.setdefault(f["name"].casefold(), f["id"])
                self._types[f["id"]] = (f.get("schema") or {}).get("type")
        try:
            return self._ids[name.casefold()]
        except KeyError:
            raise JiraError(f"Jira has no field called {name!r} (registered as {short!r})") from None

    def jql(self, short):
        fid = self.id(short)
        return f"cf[{fid.split('_')[1]}]" if fid.startswith("customfield_") else fid

    def clause(self, short, values):
        """JQL that matches any of ``values`` in the field: ``~`` for text fields (which can't do ``=`` or ``in``),
        ``in (...)`` otherwise. Text matching is fuzzy, so check results exactly afterwards."""
        fid, field = self.id(short), self.jql(short)
        if self._types.get(fid) == "string":
            return "(" + " OR ".join(f"{field} ~ {quote([v])}" for v in values) + ")"
        return f"{field} in ({quote(values)})"

    def get(self, issue, short):
        """The field's plain value on ``issue`` (None if the field isn't registered or isn't set)."""
        if short not in self.names and short not in self.SYSTEM:
            return None
        return plain(issue.get("fields", {}).get(self.id(short)))

    def ids(self, *shorts):
        return [self.id(s) for s in shorts if s in self.names or s in self.SYSTEM]


# ----------------------------------------------------------------------------- the ticket source

def quote(values):
    return ", ".join('"' + str(v).replace('"', '\\"') + '"' for v in sorted(values))


# Jira status (lower case) -> cmtrack state. Anything not here falls back on the status category.
STATUS_MAP = {
    "open": "analysis_required", "new": "analysis_required", "analysis required": "analysis_required",
    "in analysis": "in_analysis", "analysis": "in_analysis",
    "to do": "ready_for_work", "ready": "ready_for_work", "ready for work": "ready_for_work", "backlog": "ready_for_work",
    "in progress": "in_progress", "in development": "in_progress",
    "in review": "peer_review", "code review": "peer_review", "peer review": "peer_review",
    "in test": "verification", "verification": "verification", "testing": "verification",
    "done": "done", "closed": "done", "resolved": "done", "verified": "done",
    "blocked": "blocked", "on hold": "blocked",
    "cancelled": "cancelled", "canceled": "cancelled", "won't do": "cancelled", "rejected": "cancelled",
}
CATEGORY_MAP = {"new": "analysis_required", "indeterminate": "in_progress", "done": "done"}


class JiraTicketSource(TicketSource):
    """Features and discrepancies in ``top_projects``; CSC tickets in the CSCI projects (named on each CSC's
    Jira pair in cmtrack), pointing at their parent through the ``parent`` field.

    Registered field names used here (all optional except ``parent``):
        parent            CSC ticket -> its feature/discrepancy (text, issue picker, or select holding the key)
        affected_product  CSC ticket -> which CSC in its project (the second half of cmtrack's Jira pair)
        cis               top-level ticket -> CI names it affects (shown on backlogs; used by top_level_tickets)
    """

    name = "jira"
    FIELDS = ("summary", "issuetype", "status", "project", "fixVersions", "assignee", "updated")

    def __init__(self, client, top_projects, fields=None, status_map=None, top_types=None, browse_url=None):
        self.client = client
        self.top_projects = list(top_projects)
        self.fields = fields if isinstance(fields, Fields) else Fields(client, **(fields or {}))
        self.status_map = {**STATUS_MAP, **{k.casefold(): v for k, v in (status_map or {}).items()}}
        self.top_types = set(top_types or ())            # e.g. {"Feature", "Discrepancy"}; empty = any type
        self.browse = (browse_url or getattr(client, "url", "")).rstrip("/") + "/browse/"

    # -- queries -------------------------------------------------------------------------------------------

    def search(self, jql):
        """Issues matching ``jql``, with every field ``record`` needs."""
        return self.client.search(jql, list(self.FIELDS) + self.fields.ids("parent", "affected_product", "cis"))

    def query(self, projects, jql=None):
        """Every issue in ``projects``, optionally narrowed by more ``jql``: the building block for the rest."""
        where = f"project in ({quote(projects)})" + (f" AND ({jql})" if jql else "")
        return [self.record(i) for i in self.search(where + " ORDER BY key") if plain(i["fields"].get("project")) in projects]

    def by_keys(self, keys):
        keys = list(dict.fromkeys(keys))
        return [self.record(i) for n in range(0, len(keys), BATCH)
                for i in self.search(f"key in ({quote(keys[n:n + BATCH])})") if i["key"] in keys]

    # -- TicketSource ----------------------------------------------------------------------------------------

    def tickets_for_versions(self, ci, cscs, versions):
        pairs = {(c["jira_project"], c["affected_product"]) for c in cscs if c["jira_project"]}
        if not pairs or not versions:
            return []
        jql = f"fixVersion in ({quote(versions)})"
        if "affected_product" in self.fields.names:
            jql += " AND " + self.fields.clause("affected_product", {p for _, p in pairs})
        wanted = set(versions)
        return [r for r in self.query(sorted({p for p, _ in pairs}), jql)
                if (r.project, r.affected_product) in pairs and wanted & set(r.fix_versions or ())]

    def get_tickets(self, keys):
        return self.by_keys(keys)

    def get_children(self, key):
        """The CSC tickets whose parent field names ``key`` (in any project)."""
        return [r for r in (self.record(i) for i in self.search(self.fields.clause("parent", [key]) + " ORDER BY key"))
                if r.parent_key == key]

    def top_level_tickets(self, backlog, cis):
        """Open features and discrepancies for a backlog: those affecting any of its CIs (all, if it has none or
        the ``cis`` field isn't registered)."""
        names = {c["name"] for c in cis}
        found = self.query(self.top_projects, "statusCategory != Done")
        return [r for r in found if r.state not in ("done", "cancelled") and
                (not names or "cis" not in self.fields.names or names & set(r.cis or ()))]

    # -- extraction: override these for your rules ---------------------------------------------------------------

    def record(self, issue):
        """An API issue -> TicketRecord (state decided by ``state``)."""
        get = lambda short: self.fields.get(issue, short)
        project = get("project")
        top = project in self.top_projects
        cis = get("cis")
        rec = TicketRecord(
            key=issue["key"], summary=get("summary"), type=get("issuetype"), status=get("status"),
            parent_key=None if top else get("parent"),
            project=None if top else project, affected_product=None if top else get("affected_product"),
            fix_versions=get("fixVersions") or [], cis=[cis] if isinstance(cis, str) else cis,
            url=self.browse + issue["key"], assignee=get("assignee"), updated=get("updated"), state="done")
        rec.state, rec.state_reason = normalize_state(*self.state(issue, rec))
        return rec

    def state(self, issue, rec):
        """(state, reason) for one ticket. The default maps the Jira status (STATUS_MAP, then the status category)
        and flags data that doesn't add up as ``error``, so people see what to fix in Jira. Replace or extend it
        with your rules (linked issues, sub-tasks, a CCB field, ...)."""
        status = issue.get("fields", {}).get("status") or {}
        name = str(status.get("name") or "")
        state = self.status_map.get(name.casefold()) or CATEGORY_MAP.get((status.get("statusCategory") or {}).get("key"))
        if state is None:
            return "error", f"Jira status {name!r} isn't mapped to a cmtrack state"
        if rec.project:                                               # a CSC ticket
            if not rec.parent_key:
                return "error", f"no parent ticket in {self.fields.names.get('parent', 'parent')!r}"
            if "affected_product" in self.fields.names and not rec.affected_product:
                return "error", f"no {self.fields.names['affected_product']!r} set"
            if state == "done" and not rec.fix_versions:
                return "error", "closed without a fix version"
        elif self.top_types and rec.type not in self.top_types:
            return "error", f"{rec.type!r} isn't a top-level type ({', '.join(sorted(self.top_types))})"
        return state, None


def from_env():
    """CMTRACK_TICKET_SOURCES=jira=cmtrack.jira_tickets:from_env (see the module docstring for the settings)."""
    env = os.environ.get
    config = {}
    if env("CMTRACK_JIRA_CONFIG"):
        with open(env("CMTRACK_JIRA_CONFIG")) as f:
            config = json.load(f)
    client = JiraClient(env("CMTRACK_JIRA_URL"), env("CMTRACK_JIRA_TOKEN"), env("CMTRACK_JIRA_USER"),
                        env("CMTRACK_JIRA_PASSWORD"))
    return JiraTicketSource(client, config.get("top_projects", []), config.get("fields"), config.get("status_map"),
                            config.get("top_types"), config.get("browse_url"))

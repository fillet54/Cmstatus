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
                    TicketRecord; its state comes from a registered *state rule* and, for top-level tickets, a
                    *rollup* over their CSC tickets (see "states" below for both, and analysis_rule for an example).

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
     "status_map": {"Awaiting CCB": "blocked"},
     "state_rule": "cmtrack.jira_tickets:analysis_rule",          (and optionally "rollup": "module:function" or null)
     "ignore_summary": "VER-"}                                     (a regex matched at the start of the summary)
"""
import importlib
import json
import os
import re
from dataclasses import replace
from typing import Iterable, List, Optional

import requests

from .tickets import IDLE, TicketRecord, TicketSource, normalize_state, rollup

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

def in_pairs(cscs):
    """{(jira_project, affected_product)} for cmtrack CSC rows; None for no scope."""
    return None if cscs is None else {(c["jira_project"], c["affected_product"]) for c in cscs if c.get("jira_project")}


def as_list(value):
    """A field value as a list: None -> [], "x" -> ["x"], a list unchanged (empty values dropped)."""
    return [v for v in (value if isinstance(value, list) else [value]) if v not in (None, "")]


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
            return "(" + " OR ".join(f"{field} ~ {quote([v])}" for v in sorted(values)) + ")"
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


# ----------------------------------------------------------------------------- states
#
# A ticket's state comes from a *state rule*: a function you register that looks at one ticket and returns a cmtrack
# state (or (state, reason)). Top-level tickets then go through a *rollup*, which sees their own state and their CSC
# tickets and decides the final one. Both are plain functions:
#
#     def my_rule(t: JiraTicket) -> "in_progress" | ("blocked", "waiting on CCB") | None (= use status_rule)
#     def my_rollup(t: JiraTicket, own: (state, reason), children: [TicketRecord]) -> state | (state, reason)
#
# Register them with JiraTicketSource(..., state_rule=my_rule, rollup=my_rollup), by setting source.state_rule /
# source.rollup, or in the JSON config ("state_rule": "mypkg.rules:my_rule").
#
# Scope: when cmtrack asks about a set of CSCs (a CI's work report, top_level_tickets_for_versions), the rollup only
# gets the CSC tickets of those CSCs, and rules can see the scope as t.scope / t.cscs. Tickets asked for without a
# scope (a ticket page, a backlog) roll up over all their CSC tickets.
#
# Ignored: tickets that look like CSC tickets but aren't part of the work (verification tickets, say) can be given
# the state "ignored", by a rule or with ignore_summary (a regex matched at the start of the summary, checked before
# the rule). They still show in lists, but rollups, progress totals and the "open" filter leave them out, and the
# data checks (no fix version, no parent...) don't apply to them.

# Jira status (lower case) -> cmtrack state, for status_rule. Anything not here falls back on the status category.
STATUS_MAP = {
    "open": "analysis_required", "new": "analysis_required", "analysis required": "analysis_required",
    "in analysis": "in_analysis", "analysis": "in_analysis",
    "to do": "ready_for_work", "ready": "ready_for_work", "ready for work": "ready_for_work", "backlog": "ready_for_work",
    "in progress": "in_progress", "in development": "in_progress", "in work": "in_progress",
    "in review": "peer_review", "code review": "peer_review", "peer review": "peer_review",
    "in test": "verification", "verification": "verification", "testing": "verification",
    "done": "done", "closed": "done", "resolved": "done", "verified": "done",
    "blocked": "blocked", "on hold": "blocked",
    "cancelled": "cancelled", "canceled": "cancelled", "won't do": "cancelled", "rejected": "cancelled",
}
CATEGORY_MAP = {"new": "analysis_required", "indeterminate": "in_progress", "done": "done"}
WORK_STARTED = {"in_progress", "peer_review", "verification", "done", "blocked", "error"}


class JiraTicket:
    """What a state rule sees: one issue, with the lookups rules tend to need.

        t.key, t.type, t.status ("In Progress"), t.category ("new" / "indeterminate" / "done"), t.labels
        t.is_top            a feature/discrepancy (in top_projects), not a CSC ticket
        t.field("short")    a registered field's plain value, e.g. t.field("analysis_state") -> "Ready for Work"
        t.has_label("x")    case-insensitive
        t.status_state()    the Jira status through status_map / the category (None if neither knows it)
        t.scope             the CSCs being looked at (cmtrack CSC rows: name, jira_project, affected_product, team),
                            or None when there's no scope (a ticket page, a backlog)
        t.products          the ticket's Affected Product values (all of them; top-level tickets may set it too)
        t.cscs              the CSCs in scope that this ticket names (top level: by Affected Product alone)
        t.issue, t.record   the raw API issue and the TicketRecord being built
    """

    def __init__(self, source, issue, record, scope=None):
        f = issue.get("fields", {})
        status = f.get("status") or {}
        self.source, self.issue, self.record, self.scope = source, issue, record, scope
        self.products = record.attributes.get("affected_products", [])
        self.key, self.type, self.is_top = record.key, record.type, record.project is None
        self.cscs = [c for c in scope or [] if c["affected_product"] in self.products and   # top level: product only
                     (self.is_top or c["jira_project"] == record.project)]
        self.status = str(status.get("name") or "")
        self.category = (status.get("statusCategory") or {}).get("key")
        self.labels = [str(label) for label in f.get("labels") or []]

    def field(self, short):
        return self.source.fields.get(self.issue, short)

    def has_label(self, label):
        return label.casefold() in {x.casefold() for x in self.labels}

    def status_state(self):
        return self.source.status_map.get(self.status.casefold()) or CATEGORY_MAP.get(self.category)


def status_rule(t):
    """The default state rule: the Jira status, through status_map and then the status category."""
    return t.status_state() or ("error", f"Jira status {t.status!r} isn't mapped to a cmtrack state")


def work_rollup(t, own, children):
    """The default rollup for top-level tickets: their own state until work starts on any CSC ticket (in progress
    or later, or blocked / in error); from then on the CSC tickets' combined state (tickets.rollup: the least
    advanced, but in progress once anything is; blocked and error win). Cancelled and ignored CSC tickets don't
    count."""
    live = [c for c in children if c.state not in IDLE]
    if own[0] in ("error", "cancelled") or not any(c.state in WORK_STARTED for c in live):
        return own
    state = rollup([c.state for c in live])
    reason = next((f"{c.key}: {c.state_reason}" for c in live if c.state == state and c.state_reason), None)
    return state, reason


# An example of mixed rules (see the tests): register the field as fields={"analysis_state": "Analysis State"}
# and the rule as state_rule=analysis_rule; the default rollup then moves the parent to in progress once a CSC ticket
# is in work.
ANALYSIS_STATES = {"analysis required": "analysis_required", "in analysis": "in_analysis",
                   "ready for work": "ready_for_work"}
ANALYSIS_WORK = {"in_progress": "in_analysis", "peer_review": "in_analysis", "verification": "in_analysis",
                 "done": "ready_for_work"}


def analysis_rule(t):
    """Top-level tickets: the "Analysis State" field (unless Jira has closed or cancelled them). CSC tickets
    labelled "Analysis" are analysis work: in progress (or review/test) means in analysis, done means ready for
    work. Everything else: the Jira status."""
    status = t.status_state()
    if t.is_top and status not in ("done", "cancelled") and t.field("analysis_state"):
        value = str(t.field("analysis_state"))
        return ANALYSIS_STATES.get(value.casefold()) or ("error", f"Analysis State {value!r} isn't one cmtrack knows")
    if not t.is_top and t.has_label("Analysis") and status:
        return ANALYSIS_WORK.get(status, status)
    return status_rule(t)


def load_function(spec):
    """"package.module:function" -> the function (for rules named in the JSON config)."""
    module, _, name = spec.partition(":")
    return getattr(importlib.import_module(module), name)


class JiraTicketSource(TicketSource):
    """Features and discrepancies in ``top_projects``; CSC tickets in the CSCI projects (named on each CSC's
    Jira pair in cmtrack), pointing at their parent through the ``parent`` field.

    Registered field names used here (all optional except ``parent``); register any others your rules read:
        parent            CSC ticket -> its feature/discrepancy (text, issue picker, or select holding the key)
        affected_product  CSC ticket -> which CSC in its project (the second half of cmtrack's Jira pair). Single or
                          multi-select: every value is kept in attributes["affected_products"]; affected_product
                          is the first. tickets_for_versions returns one copy of the ticket per requested CSC
                          it names, each with that CSC's product
                          On a top-level ticket it names the CSCs it affects (top_level_tickets_for_versions)
        cis               top-level ticket -> CI names it affects (shown on backlogs; used by top_level_tickets)

    States: ``state_rule`` (default status_rule) decides each ticket's own state; top-level tickets then go through
    ``rollup`` (default work_rollup; None to skip it) with their CSC tickets, fetched in one query per batch.
    """

    name = "jira"
    FIELDS = ("summary", "issuetype", "status", "project", "fixVersions", "assignee", "updated", "labels")

    def __init__(self, client, top_projects, fields=None, status_map=None, top_types=None, browse_url=None,
                 state_rule=status_rule, rollup=work_rollup, ignore_summary=None):
        self.client = client
        self.top_projects = list(top_projects)
        self.fields = fields if isinstance(fields, Fields) else Fields(client, **(fields or {}))
        self.status_map = {**STATUS_MAP, **{k.casefold(): v for k, v in (status_map or {}).items()}}
        self.top_types = set(top_types or ())            # e.g. {"Feature", "Discrepancy"}; empty = any type
        self.browse = (browse_url or getattr(client, "url", "")).rstrip("/") + "/browse/"
        self.state_rule, self.rollup = state_rule or status_rule, rollup
        self.ignore_summary = re.compile(ignore_summary) if isinstance(ignore_summary, str) else ignore_summary

    # -- queries -------------------------------------------------------------------------------------------

    def search(self, jql):
        """Issues matching ``jql``, with the standard fields and every registered one (rules may read any)."""
        return self.client.search(jql, list(self.FIELDS) + self.fields.ids(*self.fields.names))

    def records(self, issues, cscs=None):
        """TicketRecords for API issues; top-level ones rolled up with their CSC tickets (one query per batch).
        With ``cscs`` (cmtrack CSC rows), the rollup only sees CSC tickets for those CSCs, and rules see the scope."""
        recs = [self.record(i, cscs) for i in issues]
        tops = [(i, r) for i, r in zip(issues, recs) if r.project is None]
        if self.rollup and tops and "parent" in self.fields.names:
            children = {}
            for c in self.children_of([r.key for _, r in tops], cscs):
                children.setdefault(c.parent_key, []).append(c)
            for issue, r in tops:
                out = self.rollup(JiraTicket(self, issue, r, cscs), (r.state, r.state_reason), children.get(r.key, []))
                r.state, r.state_reason = normalize_state(*(out if isinstance(out, tuple) else (out, None)))
        return recs

    def children_of(self, keys, cscs=None):
        """The CSC tickets whose parent field names any of ``keys`` (their own states only, no rollup); with
        ``cscs``, only those naming one of these CSCs."""
        pairs = in_pairs(cscs)
        out = []
        for n in range(0, len(keys), BATCH // 2):
            batch = set(keys[n:n + BATCH // 2])
            out += [r for r in (self.record(i, cscs) for i in self.search(self.fields.clause("parent", batch) + " ORDER BY key"))
                    if r.parent_key in batch and (pairs is None or any((r.project, p) in pairs for p in
                                                                       r.attributes["affected_products"]))]
        return out

    def query(self, projects, jql=None, cscs=None):
        """Every issue in ``projects``, optionally narrowed by more ``jql``: the building block for the rest."""
        where = f"project in ({quote(projects)})" + (f" AND ({jql})" if jql else "")
        return self.records([i for i in self.search(where + " ORDER BY key") if plain(i["fields"].get("project")) in projects],
                            cscs)

    def by_keys(self, keys, cscs=None):
        keys = list(dict.fromkeys(keys))
        return self.records([i for n in range(0, len(keys), BATCH)
                             for i in self.search(f"key in ({quote(keys[n:n + BATCH])})") if i["key"] in keys], cscs)

    # -- TicketSource ----------------------------------------------------------------------------------------

    def tickets_for_versions(self, ci, cscs, versions):
        pairs = {(c["jira_project"], c["affected_product"]) for c in cscs if c["jira_project"]}
        if not pairs or not versions:
            return []
        jql = f"fixVersion in ({quote(versions)})"
        if "affected_product" in self.fields.names:
            jql += " AND " + self.fields.clause("affected_product", {p for _, p in pairs})
        wanted, out = set(versions), []
        for r in self.query(sorted({p for p, _ in pairs}), jql, cscs):
            if wanted & set(r.fix_versions or ()):
                # one copy per requested CSC it names: a ticket for two CSCs shows under both
                out += [replace(r, affected_product=p, attributes=dict(r.attributes))
                        for p in r.attributes.get("affected_products", []) if (r.project, p) in pairs]
        return out

    def get_tickets(self, keys):
        return self.by_keys(keys)

    def get_parents(self, keys, cscs):
        """Top-level tickets by key, rolled up over just ``cscs``' tickets (the CI work report's parents)."""
        return self.by_keys(keys, cscs)

    def top_level_tickets_for_versions(self, ci, cscs, versions):
        """Features/discrepancies in the top-level projects fixed in ``versions`` whose Affected Product names any
        of ``cscs``, whether or not CSC tickets exist yet. Each is rolled up over those CSCs' tickets only (other
        CSCs following a different process, or out of scope, don't count)."""
        products = {c["affected_product"] for c in cscs if c.get("affected_product")}
        if not products or not versions:
            return []
        jql = f"fixVersion in ({quote(versions)})"
        if self.top_types:
            jql += f" AND issuetype in ({quote(sorted(self.top_types))})"
        if "affected_product" in self.fields.names:
            jql += " AND " + self.fields.clause("affected_product", products)
        return [r for r in self.query(self.top_projects, jql, cscs)
                if set(versions) & set(r.fix_versions or ()) and products & set(r.attributes["affected_products"])
                and (not self.top_types or r.type in self.top_types)]

    def get_children(self, key):
        """The CSC tickets whose parent field names ``key`` (in any project)."""
        return self.children_of([key])

    def top_level_tickets(self, backlog, cis):
        """Open features and discrepancies for a backlog: those affecting any of its CIs (all, if it has none or
        the ``cis`` field isn't registered)."""
        names = {c["name"] for c in cis}
        found = self.query(self.top_projects, "statusCategory != Done")
        return [r for r in found if r.state not in ("done", "cancelled") and
                (not names or "cis" not in self.fields.names or names & set(r.cis or ()))]

    # -- extraction ------------------------------------------------------------------------------------------

    def record(self, issue, cscs=None):
        """An API issue -> TicketRecord, with its own state (``state``); rollup happens in ``records``."""
        get = lambda short: self.fields.get(issue, short)
        project = get("project")
        top = project in self.top_projects
        cis = get("cis")
        products = as_list(get("affected_product"))                 # a single or multi-select field
        rec = TicketRecord(
            key=issue["key"], summary=get("summary"), type=get("issuetype"), status=get("status"),
            parent_key=None if top else get("parent"),
            project=None if top else project, affected_product=None if top or not products else products[0],
            attributes={"affected_products": products},
            fix_versions=get("fixVersions") or [], cis=[cis] if isinstance(cis, str) else cis,
            url=self.browse + issue["key"], assignee=get("assignee"), updated=get("updated"), state="done")
        rec.state, rec.state_reason = normalize_state(*self.state(issue, rec, cscs))
        return rec

    def state(self, issue, rec, cscs=None):
        """(state, reason): the registered state rule, then checks that flag data to fix in Jira as ``error``."""
        t = JiraTicket(self, issue, rec, cscs)
        if self.ignore_summary and self.ignore_summary.match(rec.summary or ""):
            return "ignored", f"summary matches {self.ignore_summary.pattern!r}"
        out = self.state_rule(t)
        state, reason = out if isinstance(out, tuple) else (out, None)
        if state is None:
            state, reason = (lambda o: o if isinstance(o, tuple) else (o, None))(status_rule(t))
        problem = None if state == "ignored" else self.check(t, state)
        return ("error", problem) if problem else (state, reason)

    def check(self, t, state):
        """Why this ticket's Jira data doesn't add up (shown to users as an error), or None."""
        rec = t.record
        if rec.project:                                               # a CSC ticket
            if not rec.parent_key:
                return f"no parent ticket in {self.fields.names.get('parent', 'parent')!r}"
            if "affected_product" in self.fields.names and not rec.affected_product:
                return f"no {self.fields.names['affected_product']!r} set"
            if state == "done" and not rec.fix_versions:
                return "closed without a fix version"
        elif self.top_types and rec.type not in self.top_types:
            return f"{rec.type!r} isn't a top-level type ({', '.join(sorted(self.top_types))})"
        return None


def from_env():
    """CMTRACK_TICKET_SOURCES=jira=cmtrack.jira_tickets:from_env (see the module docstring for the settings)."""
    env = os.environ.get
    config = {}
    if env("CMTRACK_JIRA_CONFIG"):
        with open(env("CMTRACK_JIRA_CONFIG")) as f:
            config = json.load(f)
    client = JiraClient(env("CMTRACK_JIRA_URL"), env("CMTRACK_JIRA_TOKEN"), env("CMTRACK_JIRA_USER"),
                        env("CMTRACK_JIRA_PASSWORD"))
    rules = {k: load_function(config[k]) if config.get(k) else None for k in ("state_rule", "rollup")}
    return JiraTicketSource(client, config.get("top_projects", []), config.get("fields"), config.get("status_map"),
                            config.get("top_types"), config.get("browse_url"), rules["state_rule"] or status_rule,
                            rules["rollup"] if "rollup" in config else work_rollup, config.get("ignore_summary"))

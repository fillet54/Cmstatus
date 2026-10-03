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
     "roles": {"analysis": {"label": "Analysis"}, "verification": {"summary": "VER:"}},   (or "module:function")
     "state_rule": "cmtrack.jira_tickets:analysis_rule"}          (and optionally "rollup": "module:function" or null)
"""
import importlib
import json
import os
import re
from collections import defaultdict
from dataclasses import replace
from urllib.parse import quote as quote_path

import requests

from .tickets import IDLE, ROLES, TicketRecord, TicketSource, normalize_state, rollup

BATCH = 100                  # keys per "key in (...)" query
PARENT_BATCH = BATCH // 2    # parent keys per children query (a text "parent" field needs one "~" test per key)


class JiraError(RuntimeError):
    pass


# ----------------------------------------------------------------------------- REST client

class JiraClient:
    """The calls a ticket or release source needs, over Jira's REST API v2, with ``requests``. Auth: a personal
    access token (Bearer), or a user and password / API token (Basic). ``verify`` is passed to requests (a CA
    bundle path, or False); give your own ``session`` for proxies, client certificates and the like."""

    def __init__(self, url, token=None, user=None, password=None, page_size=100, timeout=60, verify=True, session=None):
        if not (token or user):
            raise ValueError("JiraClient needs a token, or a user and password")
        self.url = url.rstrip("/")
        self.page_size = page_size
        self.timeout = timeout

        if session is None:
            session = requests.Session()
            session.verify = verify
        self.session = session
        self.session.headers.update({"Accept": "application/json"})
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        else:
            self.session.auth = (user, password or "")

    @classmethod
    def from_env(cls):
        """A client from CMTRACK_JIRA_URL and CMTRACK_JIRA_TOKEN (or CMTRACK_JIRA_USER + CMTRACK_JIRA_PASSWORD)."""
        env = os.environ.get
        return cls(env("CMTRACK_JIRA_URL"), token=env("CMTRACK_JIRA_TOKEN"), user=env("CMTRACK_JIRA_USER"),
                   password=env("CMTRACK_JIRA_PASSWORD"))

    def _request(self, method, path, payload=None):
        response = self.session.request(method, self.url + path, json=payload, timeout=self.timeout)
        if not response.ok:
            raise JiraError(f"{method} {path} -> HTTP {response.status_code}: {response.text[:300]}")
        return response.json() if response.content else None

    def search(self, jql, fields):
        """Every issue matching ``jql`` (all pages), with just ``fields``."""
        issues = []
        while True:
            page = self._request("POST", "/rest/api/2/search", {
                "jql": jql,
                "fields": fields,
                "startAt": len(issues),
                "maxResults": self.page_size,
            })
            issues.extend(page["issues"])
            if not page["issues"] or len(issues) >= page["total"]:
                return issues

    def fields(self):
        """[{"id": "customfield_10500", "name": "Parent Ticket", "custom": true, ...}, ...]"""
        return self._request("GET", "/rest/api/2/field")

    def project_versions(self, project):
        """A project's versions: [{"id", "name", "releaseDate"?, "released", "archived", "description"?}, ...]"""
        return self._request("GET", f"/rest/api/2/project/{quote_path(project, safe='')}/versions")


# ----------------------------------------------------------------------------- field values and JQL

def jql_list(values):
    """The inside of a JQL list: sorted, quoted and escaped. {"b", 'a"1'} -> '"a\\"1", "b"'."""
    return ", ".join('"' + str(v).replace('"', '\\"') + '"' for v in sorted(values))


def as_list(value):
    """A field value as a list: None -> [], "x" -> ["x"], a list unchanged (empty values dropped)."""
    values = value if isinstance(value, list) else [value]
    return [v for v in values if v not in (None, "")]


def plain(value):
    """A Jira field value as plain data: select {"value": x} -> x, user {"displayName": x} -> x, version or
    status {"name": x} -> x, issue {"key": x} -> x, lists element by element, everything else unchanged."""
    if isinstance(value, list):
        return [plain(v) for v in value]
    if isinstance(value, dict):
        for key in ("value", "key", "displayName", "name"):
            if key in value:
                return value[key]
    return value


def csc_pairs(cscs):
    """{(jira_project, affected_product)} for cmtrack CSC rows; None when there's no scope."""
    if cscs is None:
        return None
    return {(c["jira_project"], c["affected_product"]) for c in cscs if c.get("jira_project")}


def _batches(items, size):
    for start in range(0, len(items), size):
        yield items[start:start + size]


class Fields:
    """Short names for the fields you use, resolved to ids once (from the client's field list):

        f = Fields(client, parent="Parent Ticket", affected_product="Affected Product")
        f.id("parent") -> "customfield_10500"    f.jql("parent") -> "cf[10500]"    f.get(issue, "parent") -> "PRG-10"

    A registered value can also be an id ("customfield_10500") or a system field ("fixVersions"). ``clause`` builds
    the JQL test for a field in the form its type supports: ``~`` for text fields, ``in (...)`` for the rest."""

    SYSTEM = {"summary", "issuetype", "status", "project", "fixVersions", "assignee", "updated", "labels", "parent"}

    def __init__(self, client, **names):
        self.client = client
        self.names = dict(names)     # short name -> Jira field name (or id)
        self._ids = None             # casefolded Jira field name -> id, loaded on first use
        self._types = {}             # field id -> schema type ("string", "array", ...)

    def register(self, short, jira_name):
        self.names[short] = jira_name
        return self

    def is_known(self, short):
        """Registered, or a system field."""
        return short in self.names or short in self.SYSTEM

    def id(self, short):
        name = self.names.get(short, short)
        if name.startswith("customfield_"):
            return name                                     # already an id
        if name in self.SYSTEM and short not in self.names:
            return name                                     # a system field, by its own name
        try:
            return self._field_ids()[name.casefold()]
        except KeyError:
            raise JiraError(f"Jira has no field called {name!r} (registered as {short!r})") from None

    def _field_ids(self):
        if self._ids is None:
            ids = {}
            for f in self.client.fields():
                ids.setdefault(f["name"].casefold(), f["id"])
                self._types[f["id"]] = (f.get("schema") or {}).get("type")
            self._ids = ids
        return self._ids

    def jql(self, short):
        """How JQL refers to the field: "cf[10500]" for a custom field, the id for a system one."""
        field_id = self.id(short)
        if field_id.startswith("customfield_"):
            return f"cf[{field_id.removeprefix('customfield_')}]"
        return field_id

    def clause(self, short, values):
        """JQL that matches any of ``values`` in the field: ``~`` for text fields (which can't do ``=`` or ``in``),
        ``in (...)`` otherwise. Text matching is fuzzy, so check results exactly afterwards."""
        field_id, field = self.id(short), self.jql(short)
        if self._types.get(field_id) == "string":
            tests = [f"{field} ~ {jql_list([value])}" for value in sorted(values)]
            return "(" + " OR ".join(tests) + ")"
        return f"{field} in ({jql_list(values)})"

    def get(self, issue, short):
        """The field's plain value on ``issue`` (None if the field isn't registered or isn't set)."""
        if not self.is_known(short):
            return None
        return plain(issue.get("fields", {}).get(self.id(short)))

    def ids(self, *shorts):
        """Ids of the known fields among ``shorts`` (unknown ones are skipped)."""
        return [self.id(short) for short in shorts if self.is_known(short)]


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
#
# Roles: CSC tickets aren't all work. A *role rule* (``roles``) says what each one is for, before its state is decided:
#
#     work          the default: counts against its fix versions and drives the parent's rollup
#     analysis      drives the parent only before work starts (in analysis while one is active); its own state is
#                   its real one (done is done), and it needs no fix version
#     verification  never counted as work in a version or release, and doesn't move the parent's state: the parent
#                   carries it separately as attributes["verification"] = {"state", "done", "total"} (so "all work
#                   done" and "all work verified" are two questions). It needs no product or fix version, and a
#                   CSC scope doesn't filter it out (verification is often about the whole capability)
#     ignore        not part of the process at all: state "ignored", listed but left out of rollups and totals
#
# ``roles`` is a function (t -> role, or None for work) or a dict, first match wins:
#     {"analysis": {"label": "Analysis"}, "verification": {"summary": "VER:"}, "ignore": {"type": ["Test"]}}
# where label is case-insensitive, summary a regex matched at the start, and type the issue type name(s).

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
ACTIVE = {"in_analysis", "in_progress", "peer_review", "verification", "blocked", "error"}   # started, not done
DEFAULT_ROLES = {"analysis": {"label": "Analysis"}}


def _state_and_reason(result):
    """A rule's result as (state, reason): rules may return a bare state or a (state, reason) pair."""
    return result if isinstance(result, tuple) else (result, None)


def match_roles(roles):
    """A role rule from a dict, {role: {"label": ..., "summary": regex, "type": name(s)}}: the first role any of
    whose conditions matches, else None (work)."""
    specs = []
    for role, spec in roles.items():
        labels = as_list(spec.get("label"))
        summary = re.compile(spec["summary"]) if spec.get("summary") else None
        types = set(as_list(spec.get("type")))
        specs.append((role, labels, summary, types))

    def rule(t):
        for role, labels, summary, types in specs:
            if (any(t.has_label(label) for label in labels)
                    or (summary is not None and summary.match(t.summary))
                    or t.type in types):
                return role
        return None

    return rule


class JiraTicket:
    """What a state rule sees: one issue, with the lookups rules tend to need.

        t.key, t.type, t.summary, t.status ("In Progress"), t.category ("new" / "indeterminate" / "done"), t.labels
        t.role              a CSC ticket's role (work / analysis / verification / ignore); top-level: work
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
        fields = issue.get("fields", {})
        status = fields.get("status") or {}

        self.source = source
        self.issue = issue
        self.record = record
        self.scope = scope

        self.key = record.key
        self.type = record.type
        self.summary = record.summary or ""
        self.role = record.role
        self.is_top = record.project is None
        self.products = record.attributes.get("affected_products", [])
        self.cscs = [csc for csc in scope or [] if self._names(csc)]

        self.status = str(status.get("name") or "")
        self.category = (status.get("statusCategory") or {}).get("key")
        self.labels = [str(label) for label in fields.get("labels") or []]

    def _names(self, csc):
        """Whether this ticket names ``csc``: by Affected Product, and for a CSC ticket by Jira project too."""
        if csc["affected_product"] not in self.products:
            return False
        return self.is_top or csc["jira_project"] == self.record.project

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
    """The default rollup for top-level tickets, by phase:

    - once work starts on any work ticket (in progress or later, or blocked / in error): the work tickets' combined
      state (tickets.rollup: the least advanced, but in progress once anything is; blocked and error win). Analysis
      tickets don't count then, except that a reopened one keeps a parent whose work is all done in progress;
    - before that, while any analysis ticket is active: in analysis (or blocked / error, if one is);
    - otherwise the parent's own state.

    Verification tickets never count (see records: they're summarized apart), nor do cancelled or ignored ones."""
    own_state = own[0]
    if own_state in ("error", "cancelled"):
        return own

    counted = [c for c in children if c.state not in IDLE]
    work = [c for c in counted if c.role == "work"]
    active_analysis = [c for c in counted if c.role == "analysis" and c.state in ACTIVE]

    if any(c.state in WORK_STARTED for c in work):
        state = rollup(c.state for c in work)
        if state == "done" and active_analysis:
            return "in_progress", f"{active_analysis[0].key}: analysis reopened"
        return state, _first_reason(work, state)

    if active_analysis:
        states = {c.state for c in active_analysis}
        state = "error" if "error" in states else "blocked" if "blocked" in states else "in_analysis"
        return state, _first_reason(active_analysis, state)

    return own


def _first_reason(children, state):
    """"KEY: reason" for the first of ``children`` in ``state`` that gives a reason, or None."""
    for child in children:
        if child.state == state and child.state_reason:
            return f"{child.key}: {child.state_reason}"
    return None


def _verification_summary(children):
    """{"state", "total", "done"} over a parent's verification tickets, or None if it has none."""
    verification = [c for c in children if c.role == "verification" and c.state not in IDLE]
    if not verification:
        return None
    return {
        "state": rollup(c.state for c in verification),
        "total": len(verification),
        "done": sum(c.state == "done" for c in verification),
    }


# An example of mixed rules (see the tests): register the field as fields={"analysis_state": "Analysis State"}
# and the rule as state_rule=analysis_rule; the default rollup then moves the parent to in analysis while an
# analysis ticket is active, and to in progress once a work ticket is in work.
ANALYSIS_STATES = {"analysis required": "analysis_required", "in analysis": "in_analysis",
                   "ready for work": "ready_for_work"}
ANALYSIS_WORK = {"in_progress": "in_analysis", "peer_review": "in_analysis", "verification": "in_analysis"}


def analysis_rule(t):
    """Top-level tickets: the "Analysis State" field (unless Jira has closed or cancelled them). Analysis tickets
    (role "analysis") in progress (or review/test) read as in analysis; done is done. Everything else: the Jira
    status."""
    status = t.status_state()
    analysis_state = t.field("analysis_state")

    if t.is_top and status not in ("done", "cancelled") and analysis_state:
        value = str(analysis_state)
        return ANALYSIS_STATES.get(value.casefold()) or ("error", f"Analysis State {value!r} isn't one cmtrack knows")
    if t.role == "analysis" and status:
        return ANALYSIS_WORK.get(status, status)
    return status_rule(t)


def load_function(spec):
    """"package.module:function" -> the function (for rules named in the JSON config)."""
    module_name, _, function_name = spec.partition(":")
    return getattr(importlib.import_module(module_name), function_name)


# ----------------------------------------------------------------------------- the ticket source

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

    States: ``roles`` (a dict or function; default: the "Analysis" label means analysis) gives each CSC ticket its
    role, ``state_rule`` (default status_rule) its own state; top-level tickets then go through ``rollup`` (default
    work_rollup; None to skip it) with their CSC tickets, fetched in one query per batch, and carry their
    verification tickets' summary in attributes["verification"].
    """

    name = "jira"
    FIELDS = ("summary", "issuetype", "status", "project", "fixVersions", "assignee", "updated", "labels")

    def __init__(self, client, top_projects, fields=None, status_map=None, top_types=None, browse_url=None,
                 state_rule=status_rule, rollup=work_rollup, roles=DEFAULT_ROLES):
        self.client = client
        self.top_projects = list(top_projects)
        self.fields = fields if isinstance(fields, Fields) else Fields(client, **(fields or {}))
        self.status_map = {**STATUS_MAP, **{status.casefold(): state for status, state in (status_map or {}).items()}}
        self.top_types = set(top_types or ())            # e.g. {"Feature", "Discrepancy"}; empty = any type
        self.browse = (browse_url or getattr(client, "url", "")).rstrip("/") + "/browse/"
        self.state_rule = state_rule or status_rule
        self.rollup = rollup
        self.roles = roles

    # -- queries -------------------------------------------------------------------------------------------
    #
    # Every query re-checks its results in Python (see the module docstring), so fuzzy or loose JQL is fine.

    def search(self, jql):
        """Issues matching ``jql``, with the standard fields and every registered one (rules may read any)."""
        return self.client.search(jql, list(self.FIELDS) + self.fields.ids(*self.fields.names))

    def records(self, issues, cscs=None):
        """TicketRecords for API issues; top-level ones rolled up with their CSC tickets (one query per batch), and
        given their verification summary. With ``cscs`` (cmtrack CSC rows), the rollup only sees CSC tickets for
        those CSCs (verification tickets aren't filtered), and rules see the scope."""
        records = [self.record(issue, cscs) for issue in issues]
        top_level = [(issue, rec) for issue, rec in zip(issues, records) if rec.project is None]
        if not top_level or "parent" not in self.fields.names:
            return records

        children = defaultdict(list)
        for child in self.children_of([rec.key for _, rec in top_level], cscs):
            children[child.parent_key].append(child)

        for issue, rec in top_level:
            kids = children.get(rec.key, [])
            verification = _verification_summary(kids)
            if verification:
                rec.attributes["verification"] = verification
            if self.rollup:
                ticket = JiraTicket(self, issue, rec, cscs)
                result = self.rollup(ticket, (rec.state, rec.state_reason), kids)
                rec.state, rec.state_reason = normalize_state(*_state_and_reason(result))
        return records

    def children_of(self, keys, cscs=None):
        """The CSC tickets whose parent field names any of ``keys`` (their own states only, no rollup); with
        ``cscs``, only those naming one of these CSCs (and every verification ticket)."""
        pairs = csc_pairs(cscs)

        def in_scope(rec):
            if pairs is None or rec.role == "verification":
                return True
            return any((rec.project, product) in pairs for product in rec.attributes["affected_products"])

        children = []
        for batch in _batches(keys, PARENT_BATCH):
            parents = set(batch)
            for issue in self.search(self.fields.clause("parent", parents) + " ORDER BY key"):
                rec = self.record(issue, cscs)
                if rec.parent_key in parents and in_scope(rec):
                    children.append(rec)
        return children

    def query(self, projects, jql=None, cscs=None):
        """Every issue in ``projects``, optionally narrowed by more ``jql``: the building block for the rest."""
        where = f"project in ({jql_list(projects)})"
        if jql:
            where += f" AND ({jql})"
        issues = [issue for issue in self.search(where + " ORDER BY key")
                  if plain(issue["fields"].get("project")) in projects]
        return self.records(issues, cscs)

    def by_keys(self, keys, cscs=None):
        keys = list(dict.fromkeys(keys))                # de-duplicated, order kept
        wanted = set(keys)
        issues = []
        for batch in _batches(keys, BATCH):
            issues.extend(issue for issue in self.search(f"key in ({jql_list(batch)})") if issue["key"] in wanted)
        return self.records(issues, cscs)

    # -- TicketSource ----------------------------------------------------------------------------------------

    def tickets_for_versions(self, ci, cscs, versions):
        pairs = {(c["jira_project"], c["affected_product"]) for c in cscs if c["jira_project"]}
        if not pairs or not versions:
            return []
        projects = sorted({project for project, _ in pairs})
        products = {product for _, product in pairs}

        jql = f"fixVersion in ({jql_list(versions)})"
        if "affected_product" in self.fields.names:
            jql += " AND " + self.fields.clause("affected_product", products)

        wanted = set(versions)
        found = []
        for rec in self.query(projects, jql, cscs):
            if rec.role == "verification":              # not work against a version
                continue
            if not wanted.intersection(rec.fix_versions or ()):
                continue
            # One copy per requested CSC it names: a ticket for two CSCs shows under both.
            for product in rec.attributes.get("affected_products", []):
                if (rec.project, product) in pairs:
                    found.append(replace(rec, affected_product=product, attributes=dict(rec.attributes)))
        return found

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

        jql = f"fixVersion in ({jql_list(versions)})"
        if self.top_types:
            jql += f" AND issuetype in ({jql_list(self.top_types)})"
        if "affected_product" in self.fields.names:
            jql += " AND " + self.fields.clause("affected_product", products)

        wanted = set(versions)
        return [rec for rec in self.query(self.top_projects, jql, cscs)
                if wanted.intersection(rec.fix_versions or ())
                and products.intersection(rec.attributes["affected_products"])
                and (not self.top_types or rec.type in self.top_types)]

    def get_children(self, key):
        """The CSC tickets whose parent field names ``key`` (in any project)."""
        return self.children_of([key])

    def top_level_tickets(self, backlog, cis):
        """Open features and discrepancies for a backlog: those affecting any of its CIs (all, if it has none or
        the ``cis`` field isn't registered)."""
        open_tickets = [rec for rec in self.query(self.top_projects, "statusCategory != Done")
                        if rec.state not in ("done", "cancelled")]

        ci_names = {c["name"] for c in cis}
        if not ci_names or "cis" not in self.fields.names:
            return open_tickets
        return [rec for rec in open_tickets if ci_names.intersection(rec.cis or ())]

    # -- extraction ------------------------------------------------------------------------------------------

    def record(self, issue, cscs=None):
        """An API issue -> TicketRecord, with its own state (``state``); rollup happens in ``records``."""
        def get(short):
            return self.fields.get(issue, short)

        project = get("project")
        is_top = project in self.top_projects
        products = as_list(get("affected_product"))     # a single or multi-select field

        rec = TicketRecord(
            key=issue["key"],
            summary=get("summary"),
            type=get("issuetype"),
            status=get("status"),
            parent_key=None if is_top else get("parent"),
            project=None if is_top else project,
            affected_product=products[0] if products and not is_top else None,
            fix_versions=get("fixVersions") or [],
            cis=get("cis"),
            url=self.browse + issue["key"],
            assignee=get("assignee"),
            updated=get("updated"),
            attributes={"affected_products": products},
            state="done",                               # a placeholder; the real state is worked out just below
        )
        rec.state, rec.state_reason = normalize_state(*self.state(issue, rec, cscs))
        return rec

    def role(self, t):
        """A CSC ticket's role from the role rule (``roles``): work, analysis, verification or ignore."""
        if t.is_top:
            return "work"
        rule = self.roles if callable(self.roles) else match_roles(self.roles or {})
        role = rule(t)
        return role if role in ROLES or role == "ignore" else "work"

    def state(self, issue, rec, cscs=None):
        """(state, reason): the role, the registered state rule, then checks that flag data to fix in Jira as
        ``error``. Sets ``rec.role`` as it goes."""
        t = JiraTicket(self, issue, rec, cscs)
        role = self.role(t)
        if role == "ignore":
            rec.role = t.role = "work"                  # records have no "ignore" role; the "ignored" state says it
            return "ignored", "left out by the role rules"
        rec.role = t.role = role

        state, reason = _state_and_reason(self.state_rule(t))
        if state is None:                               # the rule passed: fall back on the Jira status
            state, reason = _state_and_reason(status_rule(t))

        if state != "ignored":
            problem = self.check(t, state)
            if problem:
                return "error", problem
        return state, reason

    def check(self, t, state):
        """Why this ticket's Jira data doesn't add up (shown to users as an error), or None."""
        rec = t.record
        names = self.fields.names

        if not rec.project:                             # a top-level ticket
            if self.top_types and rec.type not in self.top_types:
                return f"{rec.type!r} isn't a top-level type ({', '.join(sorted(self.top_types))})"
            return None

        if not rec.parent_key:
            return f"no parent ticket in {names.get('parent', 'parent')!r}"
        if "affected_product" in names and not rec.affected_product and rec.role != "verification":
            return f"no {names['affected_product']!r} set"
        if state == "done" and not rec.fix_versions and rec.role == "work":
            return "closed without a fix version"
        return None


def from_env():
    """CMTRACK_TICKET_SOURCES=jira=cmtrack.jira_tickets:from_env (see the module docstring for the settings)."""
    config = {}
    config_path = os.environ.get("CMTRACK_JIRA_CONFIG")
    if config_path:
        with open(config_path) as f:
            config = json.load(f)

    state_rule = load_function(config["state_rule"]) if config.get("state_rule") else status_rule
    if "rollup" not in config:
        rollup_rule = work_rollup
    else:
        rollup_rule = load_function(config["rollup"]) if config["rollup"] else None   # null: no rollup

    roles = config.get("roles", DEFAULT_ROLES)
    if isinstance(roles, str):
        roles = load_function(roles)

    return JiraTicketSource(
        JiraClient.from_env(),
        top_projects=config.get("top_projects", []),
        fields=config.get("fields"),
        status_map=config.get("status_map"),
        top_types=config.get("top_types"),
        browse_url=config.get("browse_url"),
        state_rule=state_rule,
        rollup=rollup_rule,
        roles=roles,
    )

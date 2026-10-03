"""Ticket sources: where CSC work items come from.

The ticket source (Jira, typically) is the system of record. cmtrack never stores tickets; every page
and API call that shows tickets asks the source, so what you see is what the source says right now.
Whether to cache, and for how long, is the source's decision.

cmtrack's part is everything the source doesn't know: which versions a range like 2026.Q4-b4..2027.Q1-b2
covers (the lineage DAG), which CSC/CSCI a (Jira project, affected product) pair belongs to, and the
grouping into parent ticket -> CSC -> CSC tickets.

    class JiraSource(TicketSource):
        name = "jira"

        def tickets_for_versions(self, ci, cscs, versions):
            for csc in cscs:
                jql = (f'project = {csc["jira_project"]} AND "Affected Product" = "{csc["affected_product"]}" '
                       f'AND fixVersion in ({", ".join(map(repr, versions))})')
                for issue in my_jira.search(jql):
                    yield self.record(issue)

        def get_tickets(self, keys):
            return [self.record(i) for i in my_jira.search(f"key in ({', '.join(keys)})")]

        def get_children(self, key):
            return [self.record(i) for i in my_jira.search(f"parent = {key}")]

        def record(self, issue):
            state, reason = my_state_logic(issue, my_jira.linked(issue))   # your domain rules
            return TicketRecord(key=issue.key, summary=issue.summary, state=state, state_reason=reason,
                                status=issue.status, parent_key=issue.parent, project=issue.project,
                                affected_product=issue.affected_product, fix_versions=issue.fix_versions,
                                url=issue.url)

Register it with ``create_app({"TICKET_SOURCES": {"jira": JiraSource()}})`` or, without touching the
app factory, ``CMTRACK_TICKET_SOURCES="jira=mypackage.jira:JiraSource"`` (the factory is called with
no arguments, so read credentials from the environment there). Exceptions raised by a source are
reported to the user (HTTP 502 / an alert in the page) rather than crashing the request.
"""
import importlib
from collections import defaultdict
from dataclasses import asdict, dataclass, field, fields
from typing import Iterable, List, Optional

# ----------------------------------------------------------------------------- states and roles

# Workflow states, in order. The source decides a ticket's state, typically with domain logic over the
# ticket and every issue linked to it, not a 1:1 map of one Jira status. cmtrack only stores and reports it.
STATES = {
    "analysis_required":    "Analysis required",
    "in_analysis":          "In analysis",
    "ready_for_work":       "Ready for work",
    "in_progress":          "In progress",
    "peer_review":          "Peer review",
    "verification":         "Verification",
    "done":                 "Done",
    "blocked":              "Blocked",           # waiting on something; state_reason says what
    "cancelled":            "Cancelled",
    "ignored":              "Ignored",           # not part of the work (e.g. a verification ticket filed like a CSC
                                                 # ticket): shown, but left out of rollups and progress totals
    "error":                "Error",             # something is off in the source data; state_reason says what
}
ERROR = "error"

# Other spellings a source may send for a state.
ALIASES = {"analysis_in_progress": "in_analysis", "merge_blocked": "blocked", "canceled": "cancelled"}

# The forward path a ticket moves along (STATES minus the side states blocked, cancelled, ignored, error).
WORKFLOW = ["analysis_required", "in_analysis", "ready_for_work", "in_progress", "peer_review", "verification", "done"]

# CSC ticket states a parent's rollup doesn't count.
IDLE = ("cancelled", "ignored")

# What a CSC ticket is for. Only "work" (and "analysis") tickets count as work against a version; a "verification"
# ticket is tracked separately, per parent, and never shows in a version's or release's work.
ROLES = ("work", "analysis", "verification")


def _has_started_work(state: str) -> bool:
    """In progress or further along the workflow."""
    return WORKFLOW.index(state) >= WORKFLOW.index("in_progress")


def rollup(states: Iterable[str]) -> Optional[str]:
    """A parent ticket's state from its CSC tickets' states.

    - Cancelled and ignored tickets don't count. If nothing else is left: cancelled (if any were), else None.
    - Any error, then any blocked, wins.
    - Otherwise the least advanced state, except that once any CSC has started work (in progress or later),
      a parent still partly in analysis counts as in progress.
    """
    states = list(states)
    counted = [s for s in states if s not in IDLE]
    if not counted:
        return "cancelled" if "cancelled" in states else None

    for overriding in ("error", "blocked"):
        if overriding in counted:
            return overriding

    least_advanced = min(counted, key=WORKFLOW.index)
    if any(_has_started_work(s) for s in counted) and not _has_started_work(least_advanced):
        return "in_progress"
    return least_advanced


def normalize_state(state, reason=None):
    """(state, reason) with the state turned into a ``STATES`` key ("In Progress" -> "in_progress").

    Anything missing or unrecognized becomes ``error``, with the reason saying why."""
    key = str(state or "").strip().lower().replace(" ", "_").replace("-", "_")
    key = ALIASES.get(key, key)
    if key in STATES:
        return key, reason

    problem = f"source sent unknown state {state!r}" if state else "source did not supply a state"
    return ERROR, f"{problem}; {reason}" if reason else problem


# ----------------------------------------------------------------------------- records

@dataclass
class TicketRecord:
    """One ticket as a source reports it.

    CSC tickets carry ``project`` + ``affected_product`` (resolved to a CSC through the CSC's Jira pair)
    and ``fix_versions`` (names of versions of that CSC's CSCI, as cmtrack names them). Parent tickets
    usually have neither.

    ``state`` is one of ``STATES`` and is the source's call; ``state_reason`` explains it where useful
    (why it's ``error``, what a ``blocked`` ticket waits on). ``status`` is the raw source status,
    kept for display. ``role`` (``ROLES``) says what a CSC ticket is for: work (the default), analysis or
    verification.
    """
    key: str
    summary: Optional[str] = None
    type: Optional[str] = None
    state: Optional[str] = None
    state_reason: Optional[str] = None
    status: Optional[str] = None
    parent_key: Optional[str] = None
    project: Optional[str] = None
    affected_product: Optional[str] = None
    fix_versions: Optional[List[str]] = None
    cis: Optional[List[str]] = None          # CI names a top-level ticket affects, if the source knows
    url: Optional[str] = None
    assignee: Optional[str] = None
    updated: Optional[str] = None
    attributes: dict = field(default_factory=dict)
    role: str = "work"

    @classmethod
    def from_dict(cls, data: dict) -> "TicketRecord":
        """Build a record from a dict. Keys that aren't fields are kept in ``attributes``."""
        if not isinstance(data, dict) or not str(data.get("key") or "").strip():
            raise ValueError("each ticket record needs a 'key'")

        field_names = {f.name for f in fields(cls)}
        known = {name: value for name, value in data.items() if name in field_names}
        extra = {name: value for name, value in data.items() if name not in field_names}

        record = cls(**known)
        record.key = str(record.key).strip()
        record.attributes = {**(record.attributes or {}), **extra}
        return record

    def __post_init__(self):
        self.state, self.state_reason = normalize_state(self.state, self.state_reason)
        if isinstance(self.fix_versions, str):
            self.fix_versions = [self.fix_versions]
        if isinstance(self.cis, str):
            self.cis = [self.cis]
        if self.role not in ROLES:
            self.role = "work"

    def to_dict(self) -> dict:
        return asdict(self)


# ----------------------------------------------------------------------------- sources

class TicketSource:
    """Implement this for a ticket system. Called on every request; methods may return generators."""

    name = "jira"

    def tickets_for_versions(self, ci: dict, cscs: List[dict], versions: List[str]) -> Iterable[TicketRecord]:
        """CSC tickets of ``ci`` whose fix versions include any of ``versions`` (cmtrack version names).

        ``cscs`` are the CI's CSC rows (name, jira_project, affected_product, team), i.e. what to query.
        Map the source's version naming to cmtrack's here if they differ, and work out each ticket's
        ``state`` (from its status, sub-tasks, links, ...); use ``error`` + ``state_reason`` when the
        source data doesn't add up, so users can see what to fix.
        """
        raise NotImplementedError

    def get_tickets(self, keys: List[str]) -> Iterable[TicketRecord]:
        """Tickets by key (a ticket page, and the parents of CSC tickets in a report). Omit unknown keys."""
        raise NotImplementedError

    def get_parents(self, keys: List[str], cscs: List[dict]) -> Iterable[TicketRecord]:
        """Optional: parent tickets for a report about ``cscs`` (a CI's CSC rows), their state judged over just
        those CSCs' tickets. Defaults to ``get_tickets(keys)``."""
        return self.get_tickets(keys)

    def get_children(self, key: str) -> Iterable[TicketRecord]:
        """The CSC tickets under a parent ticket, across all CIs."""
        raise NotImplementedError

    def top_level_tickets(self, backlog: dict, cis: List[dict]) -> Iterable[TicketRecord]:
        """Optional: candidate top-level tickets for a shared backlog ("Pull from Jira").

        ``backlog`` has name/description/teams; ``cis`` are the CIs it is related to (may be empty).
        Set ``cis`` on the records if you know which CIs each ticket affects. New keys are appended
        to the bottom of the backlog; nothing is removed.
        """
        raise NotImplementedError(f"ticket source {self.name!r} can't list top-level tickets")


def _get(record, name):
    """A field of a TicketRecord or of a plain dict."""
    return getattr(record, name) if isinstance(record, TicketRecord) else record.get(name)


class StaticSource(TicketSource):
    """In-memory source (tests, demos, a JSON export): answers from a fixed list of records."""

    def __init__(self, records: Iterable, name: str = "static"):
        """``records``: TicketRecords or dicts. A parent given as a dict without a ``state`` gets its children's
        (``rollup``)."""
        self.name = name
        records = list(records)

        child_states = defaultdict(list)          # parent key -> its children's states
        for record in records:
            parent = _get(record, "parent_key")
            if parent:
                state, _ = normalize_state(_get(record, "state"))
                child_states[parent].append(state)

        self.records = [self._to_record(record, child_states) for record in records]

    @staticmethod
    def _to_record(record, child_states) -> TicketRecord:
        if isinstance(record, TicketRecord):
            return record
        if not record.get("state") and record.get("key") in child_states:
            record = {**record, "state": rollup(child_states[record["key"]])}
        return TicketRecord.from_dict(record)

    def tickets_for_versions(self, ci, cscs, versions):
        pairs = {(c["jira_project"], c["affected_product"]) for c in cscs}
        wanted = set(versions)
        return [r for r in self.records
                if (r.project, r.affected_product) in pairs and wanted.intersection(r.fix_versions or ())]

    def get_tickets(self, keys):
        keys = set(keys)
        return [r for r in self.records if r.key in keys]

    def get_children(self, key):
        return [r for r in self.records if r.parent_key == key]

    def top_level_tickets(self, backlog, cis):
        ci_names = {c["name"] for c in cis}
        top_level = [r for r in self.records if not r.parent_key and not r.project]
        if not ci_names:
            return top_level
        return [r for r in top_level if ci_names.intersection(r.cis or ())]


# ----------------------------------------------------------------------------- configuration

def pick_source(sources: Optional[dict], name: Optional[str] = None) -> Optional[TicketSource]:
    """The source called ``name``; with no name, the only (or first) configured source; None if none."""
    sources = sources or {}
    if not name:
        return next(iter(sources.values()), None)
    if name not in sources:
        raise KeyError(f"unknown ticket source {name!r}; configured: {sorted(sources)}")
    return sources[name]


def load_sources(spec: Optional[str], what: str = "ticket source") -> dict:
    """Parse CMTRACK_TICKET_SOURCES (or CMTRACK_RELEASE_SOURCES): 'name=module:factory[,name=module:factory]'.

    Each factory is called with no arguments and the result is named ``name``."""
    sources = {}
    for entry in (spec or "").split(","):
        entry = entry.strip()
        if not entry:
            continue

        name, _, target = entry.partition("=")
        module_name, _, factory_name = target.partition(":")
        if not (name and module_name and factory_name):
            raise ValueError(f"bad {what} {entry!r}; expected name=module:factory")

        factory = getattr(importlib.import_module(module_name), factory_name)
        source = factory()
        source.name = name
        sources[name] = source
    return sources

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
from dataclasses import asdict, dataclass, field
from typing import Iterable, List, Optional

# Workflow states, in order. The source decides a ticket's state, typically with domain logic over the
# ticket and every issue linked to it, not a 1:1 map of one Jira status. cmtrack only stores and reports it.
STATES = {
    "analysis_required":    "Analysis required",
    "analysis_in_progress": "Analysis in progress",
    "ready_for_work":       "Ready for work",
    "in_progress":          "In progress",
    "peer_review":          "Peer review",
    "merge_blocked":        "Merge blocked",     # code is ready, but something is holding up the merge
    "verification":         "Verification",
    "done":                 "Done",
    "error":                "Error",             # something is off in the source data; state_reason says what
}
DONE, ERROR = "done", "error"


def normalize_state(state, reason=None):
    """(state, reason) with anything missing or unrecognized turned into 'error' plus an explanation."""
    key = str(state or "").strip().lower().replace(" ", "_").replace("-", "_")
    if key in STATES:
        return key, reason
    why = f"source sent unknown state {state!r}" if state else "source did not supply a state"
    return ERROR, f"{why}; {reason}" if reason else why


@dataclass
class TicketRecord:
    """One ticket as a source reports it.

    CSC tickets carry ``project`` + ``affected_product`` (resolved to a CSC through the CSC's Jira pair)
    and ``fix_versions`` (names of versions of that CSC's CSCI, as cmtrack names them). Parent tickets
    usually have neither.

    ``state`` is one of ``STATES`` and is the source's call; ``state_reason`` explains it where useful
    (why it's ``error``, what a ``merge_blocked`` ticket waits on). ``status`` is the raw source status,
    kept for display.
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
    url: Optional[str] = None
    assignee: Optional[str] = None
    updated: Optional[str] = None
    attributes: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "TicketRecord":
        if not isinstance(d, dict) or not str(d.get("key") or "").strip():
            raise ValueError("each ticket record needs a 'key'")
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        extra = {k: v for k, v in d.items() if k not in cls.__dataclass_fields__}
        rec = cls(**known)
        rec.key = str(rec.key).strip()
        rec.attributes = {**(rec.attributes or {}), **extra}
        return rec

    def __post_init__(self):
        self.state, self.state_reason = normalize_state(self.state, self.state_reason)
        if isinstance(self.fix_versions, str):
            self.fix_versions = [self.fix_versions]

    def to_dict(self) -> dict:
        return asdict(self)


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

    def get_children(self, key: str) -> Iterable[TicketRecord]:
        """The CSC tickets under a parent ticket, across all CIs."""
        raise NotImplementedError


class StaticSource(TicketSource):
    """In-memory source (tests, demos, a JSON export): answers from a fixed list of records."""

    def __init__(self, records: Iterable, name: str = "static"):
        self.name = name
        self.records = [r if isinstance(r, TicketRecord) else TicketRecord.from_dict(r) for r in records]

    def tickets_for_versions(self, ci, cscs, versions):
        pairs = {(c["jira_project"], c["affected_product"]) for c in cscs}
        wanted = set(versions)
        return [r for r in self.records
                if (r.project, r.affected_product) in pairs and wanted & set(r.fix_versions or ())]

    def get_tickets(self, keys):
        keys = set(keys)
        return [r for r in self.records if r.key in keys]

    def get_children(self, key):
        return [r for r in self.records if r.parent_key == key]


def pick_source(sources: Optional[dict], name: Optional[str] = None) -> Optional[TicketSource]:
    """The source called ``name``; with no name, the only (or first) configured source; None if none."""
    sources = sources or {}
    if name:
        if name not in sources:
            raise KeyError(f"unknown ticket source {name!r}; configured: {sorted(sources)}")
        return sources[name]
    return next(iter(sources.values()), None)


def load_sources(spec: Optional[str]) -> dict:
    """Parse CMTRACK_TICKET_SOURCES: 'name=module:factory[,name=module:factory]'."""
    sources = {}
    for part in filter(None, (p.strip() for p in (spec or "").split(","))):
        name, _, target = part.partition("=")
        module, _, attr = target.partition(":")
        if not (name and module and attr):
            raise ValueError(f"bad ticket source {part!r}; expected name=module:factory")
        source = getattr(importlib.import_module(module), attr)()
        source.name = name
        sources[name] = source
    return sources

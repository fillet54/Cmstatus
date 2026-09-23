"""Ticket sources: where CSC work items come from.

cmtrack stores tickets in a source-neutral shape (``TicketRecord``). A source is anything that can
produce records; Jira is the expected one. Two ways to get tickets in:

* pull: implement ``TicketSource`` and register it; ``POST /api/cis/<ci>/tickets/sync`` calls it
  for a version range and upserts what it returns (then fetches any parent tickets it referenced);
* push: another system posts records to ``POST /api/tickets`` in the ``TicketRecord`` JSON shape.

Both paths go through ``service.upsert_tickets``, so they behave the same.

    class JiraSource(TicketSource):
        name = "jira"
        def fetch_for_versions(self, ci, cscs, versions):
            for csc in cscs:
                jql = (f'project = {csc["jira_project"]} AND "Affected Product" = "{csc["affected_product"]}" '
                       f'AND fixVersion in ({", ".join(map(repr, versions))})')
                for issue in my_jira.search(jql):
                    yield TicketRecord(key=issue.key, summary=issue.summary, ...)
        def fetch_by_keys(self, keys):
            ...

Register it with ``create_app({"TICKET_SOURCES": {"jira": JiraSource()}})`` or, without touching the
app factory, ``CMTRACK_TICKET_SOURCES="jira=mypackage.jira:JiraSource"`` (the factory is called with
no arguments, so read credentials from the environment there).
"""
import importlib
from dataclasses import asdict, dataclass, field
from typing import Iterable, List, Optional

STATUS_CATEGORIES = ("todo", "in_progress", "done")
_CATEGORY_ALIASES = {
    "new": "todo", "to do": "todo", "todo": "todo", "open": "todo",              # Jira key 'new'
    "indeterminate": "in_progress", "in progress": "in_progress", "in_progress": "in_progress",
    "done": "done", "closed": "done", "resolved": "done",
}


def status_category(value) -> str:
    """Normalize a source's status category (Jira: new / indeterminate / done) to todo / in_progress / done."""
    return _CATEGORY_ALIASES.get(str(value or "").strip().lower(), "todo")


@dataclass
class TicketRecord:
    """One ticket as a source reports it.

    CSC tickets carry ``project`` + ``affected_product`` (resolved to a CSC through the CSC's Jira pair)
    and ``fix_versions`` (names of versions of that CSC's CSCI). Parent tickets usually have neither.
    ``fix_versions=None`` leaves existing version links alone; ``[]`` clears them.
    """
    key: str
    summary: Optional[str] = None
    type: Optional[str] = None
    status: Optional[str] = None
    status_category: str = "todo"
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
        self.status_category = status_category(self.status_category)
        if isinstance(self.fix_versions, str):
            self.fix_versions = [self.fix_versions]

    def to_dict(self) -> dict:
        return asdict(self)


class TicketSource:
    """Implement this for a ticket system. Methods may be generators."""

    name = "jira"

    def fetch_for_versions(self, ci: dict, cscs: List[dict], versions: List[str]) -> Iterable[TicketRecord]:
        """CSC tickets of ``ci`` whose fix version is one of ``versions`` (cmtrack version names).

        ``cscs`` are the CI's CSC rows (name, jira_project, affected_product, team), i.e. what to query.
        Map the source's version naming to cmtrack's here if they differ.
        """
        raise NotImplementedError

    def fetch_by_keys(self, keys: List[str]) -> Iterable[TicketRecord]:
        """Tickets by key; used to fill in parent tickets referenced by CSC tickets."""
        return []


class StaticSource(TicketSource):
    """In-memory source (tests, demos, or a JSON export): filters a fixed list of records."""

    def __init__(self, records: Iterable, name: str = "static"):
        self.name = name
        self.records = [r if isinstance(r, TicketRecord) else TicketRecord.from_dict(r) for r in records]

    def fetch_for_versions(self, ci, cscs, versions):
        pairs = {(c["jira_project"], c["affected_product"]) for c in cscs}
        wanted = set(versions)
        return [r for r in self.records
                if (r.project, r.affected_product) in pairs and wanted & set(r.fix_versions or ())]

    def fetch_by_keys(self, keys):
        keys = set(keys)
        return [r for r in self.records if r.key in keys]


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

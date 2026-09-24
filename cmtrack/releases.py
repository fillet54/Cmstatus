"""Release sources: where a CI's releases and builds come from.

A CI either syncs from a release source (Jira, typically) or is managed by hand. A sync asks the source
for *everything* it knows about the CI and reconciles: new items are created, known ones updated, items the
source no longer lists are flagged "missing" (never deleted). What doesn't add up is reported, and fixed by
hand in cmtrack: remap a missing item onto the one the source now calls it, detach it, cancel it, or pin a
field. cmtrack keeps what the source doesn't know: build status, build/release dates, manifests, lineage
merges, baselines.

Identity is the source's key (a Jira version id), so a rename in the source is a rename here. Unlike
tickets, releases and builds are stored, because cmtrack hangs its own state off them.

The simplest source to write is a ``PatternSource``: return the flat list of versions in a Jira project and
let name patterns (the CI's ``source_params``) sort them into releases, builds, patches and emergencies:

    class JiraReleases(PatternSource):
        def versions(self, ci, params):
            for v in my_jira.project_versions(params["project"]):
                yield SourceVersion(key=v.id, name=v.name, date=v.releaseDate, description=v.description,
                                    released=v.released, archived=v.archived)

Register it like a ticket source: ``create_app({"RELEASE_SOURCES": {"jira": JiraReleases()}})`` or
``CMTRACK_RELEASE_SOURCES="jira=mypackage.jira:JiraReleases"``. Anything else (a different system, rules
the patterns can't express) implements ``ReleaseSource.releases`` and returns ``ReleaseRecord``s directly.
"""
import re
from dataclasses import asdict, dataclass, field
from typing import Iterable, List, Optional, Union

KINDS = ("planned", "patch", "emergency")


class SourceConfigError(ValueError):
    """The CI's source_params don't make sense to the source (bad pattern, missing project, ...)."""


@dataclass
class BuildRecord:
    key: str
    name: str
    planned_date: Optional[str] = None


@dataclass
class ReleaseRecord:
    """One release as the source reports it, with its builds in order.

    ``parent_key`` ties a patch/emergency to the planned release it patches. ``reason`` is the change
    request (e.g. the Jira version description). ``released`` is what the source believes; cmtrack only
    reports a mismatch, releasing still goes through cmtrack's gate.
    """
    key: str
    name: str
    kind: str = "planned"
    target_date: Optional[str] = None
    parent_key: Optional[str] = None
    reason: Optional[str] = None
    released: Optional[bool] = None
    builds: List[BuildRecord] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "ReleaseRecord":
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        known["builds"] = [b if isinstance(b, BuildRecord) else BuildRecord(**b) for b in d.get("builds") or ()]
        return cls(**known)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Unplaced:
    """Something the source has but couldn't place (e.g. a build with no matching release). Shown to users."""
    key: str
    name: str
    why: str


class ReleaseSource:
    """Implement this for a release system. Called on every sync."""

    name = "jira"

    def releases(self, ci: dict, params: dict) -> Iterable[Union[ReleaseRecord, Unplaced]]:
        """Every release (with its builds) the source has for ``ci``. ``params`` is the CI's source_params."""
        raise NotImplementedError


@dataclass
class SourceVersion:
    """One entry of a flat version list, e.g. a Jira project version."""
    key: str
    name: str
    date: Optional[str] = None
    description: Optional[str] = None
    released: Optional[bool] = None
    archived: bool = False


class PatternSource(ReleaseSource):
    """Sorts a flat version list into releases by name patterns. The CI's ``source_params``:

        {"project": "NAV",
         "patterns": {"planned":   "(?P<line>\\d{4}\\.Q\\d)",
                      "build":     "(?P<line>\\d{4}\\.Q\\d)-b(?P<n>\\d+)",
                      "patch":     "(?P<line>\\d{4}\\.Q\\d)\\.P(?P<n>\\d+)",
                      "emergency": "(?P<line>\\d{4}\\.Q\\d)\\.ER(?P<n>\\d+)"},
         "self_build": ["patch", "emergency"],
         "include_archived": true}

    Patterns must match the whole name. Named groups tie things together: a build, patch or emergency
    belongs to the planned release whose groups (all except ``n``) have the same values; ``n`` orders
    builds. Only ``planned`` is required. Kinds in ``self_build`` get their own version as a build (a
    patch is usually one Jira version that is both the release and its build; add "planned" when a
    release's final build carries the release's name). Names matching no pattern are ignored.
    """

    PATTERN_KINDS = ("planned", "build", "patch", "emergency")

    def versions(self, ci: dict, params: dict) -> Iterable[SourceVersion]:
        raise NotImplementedError

    @classmethod
    def check_params(cls, params: dict) -> dict:
        patterns = (params or {}).get("patterns")
        if not isinstance(patterns, dict) or "planned" not in patterns:
            raise SourceConfigError("source_params.patterns must map at least 'planned' to a regular expression")
        unknown = set(patterns) - set(cls.PATTERN_KINDS)
        if unknown:
            raise SourceConfigError(f"unknown pattern kinds {sorted(unknown)}; use {list(cls.PATTERN_KINDS)}")
        compiled = {}
        for kind, rx in patterns.items():
            try:
                compiled[kind] = re.compile(rx)
            except (re.error, TypeError) as e:
                raise SourceConfigError(f"bad {kind} pattern {rx!r}: {e}") from None
        self_build = params.get("self_build", ["patch", "emergency"])
        if not isinstance(self_build, list) or set(self_build) - set(KINDS):
            raise SourceConfigError(f"self_build must be a list drawn from {list(KINDS)}")
        return compiled

    def releases(self, ci, params):
        pats = self.check_params(params)
        self_build = set(params.get("self_build", ["patch", "emergency"]))
        include_archived = params.get("include_archived", True)
        planned, children, builds, out = {}, [], {}, []

        for v in self.versions(ci, params):
            v = v if isinstance(v, SourceVersion) else SourceVersion(**v)
            if v.archived and not include_archived:
                continue
            hits = [(k, m) for k, rx in pats.items() for m in [rx.fullmatch(v.name)] if m]
            if not hits:
                continue
            if len(hits) > 1:
                out.append(Unplaced(v.key, v.name, f"matches more than one pattern ({', '.join(k for k, _ in hits)})"))
                continue
            kind, m = hits[0]
            groups = m.groupdict()
            line = tuple(sorted((g, val) for g, val in groups.items() if g != "n"))
            n = groups.get("n")
            order = (int(n) if n and n.isdigit() else float("inf"), v.date or "", v.name)
            if kind == "planned":
                if line in planned:
                    out.append(Unplaced(v.key, v.name, f"same release line as {planned[line].name}"))
                    continue
                planned[line] = ReleaseRecord(key=v.key, name=v.name, target_date=v.date, reason=v.description,
                                              released=v.released)
            elif kind == "build":
                builds.setdefault(line, []).append((order, v))
            else:
                children.append((line, kind, v))

        def line_name(line):
            return ", ".join(f"{g}={val}" for g, val in line) or "no groups"

        for line, rec in planned.items():
            found = sorted(builds.pop(line, []), key=lambda b: b[0])
            rec.builds = [BuildRecord(v.key, v.name, v.date) for _, v in found]
            if "planned" in self_build:
                rec.builds.append(BuildRecord(rec.key, rec.name, rec.target_date))
            out.append(rec)
        for line, kind, v in children:
            parent = planned.get(line)
            if parent is None:
                out.append(Unplaced(v.key, v.name, f"{kind} with no planned release for {line_name(line)}"))
                continue
            rec = ReleaseRecord(key=v.key, name=v.name, kind=kind, target_date=v.date, parent_key=parent.key,
                                reason=v.description, released=v.released)
            if kind in self_build:
                rec.builds = [BuildRecord(v.key, v.name, v.date)]
            out.append(rec)
        for line, found in builds.items():
            for _, v in found:
                out.append(Unplaced(v.key, v.name, f"build with no planned release for {line_name(line)}"))
        return out


class StaticVersionSource(PatternSource):
    """In-memory PatternSource (tests, demos, an export): ``{project: [SourceVersion or dict, ...]}``."""

    def __init__(self, projects: dict, name: str = "static"):
        self.name = name
        self.projects = {p: [v if isinstance(v, SourceVersion) else SourceVersion(**v) for v in vs]
                         for p, vs in projects.items()}

    def versions(self, ci, params):
        project = params.get("project")
        if project not in self.projects:
            raise SourceConfigError(f"unknown project {project!r}")
        return list(self.projects[project])

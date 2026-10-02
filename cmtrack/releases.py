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

KINDS = ("planned", "patch", "emergency", "snapshot")   # snapshot: one-off builds, in no release


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
                      "emergency": "(?P<line>\\d{4}\\.Q\\d)\\.ER(?P<n>\\d+)",
                      "snapshot":  "(?P<line>\\d{4}\\.Q\\d)-s(?P<n>\\d+)"},
         "self_build": ["patch", "emergency"],
         "child_builds": {"kind": "emergency", "group": "n1", "after": 3},
         "include_archived": true}

    Patterns must match the whole name. Named groups tie things together: a build, patch, emergency or snapshot
    belongs to the planned release whose groups have the same values, leaving out the *order groups*: ``n``,
    ``n1``, ``n2``, ... (numbers; builds are ordered by them, in that order, so 2026.01.01.00 < 2026.01.01.01 <
    2026.01.02.00 with ``(?P<n1>\\d\\d)\\.(?P<n2>\\d\\d)``). Only ``planned`` is required. Names matching no
    pattern are ignored.

    ``self_build``: kinds that get their own version as a build (a patch is usually one Jira version that is both
    the release and its build; add "planned" when a release's final build carries the release's name).

    ``child_builds``: builds numbered past a planned release's own belong to its patches or emergencies. With
    {"kind": "emergency", "group": "n1", "after": 3}, a build whose n1 is 3 or less is the planned release's, and
    one whose n1 is 3 + k is the build of the emergency numbered k (its ``n``) on the same line: 2026.01.04.00
    belongs to 2026.01.ER01, 2026.01.12.00 to 2026.01.ER09.

    ``snapshot``: one-off builds that belong to no release. They're kept per line, in a "<release> snapshots"
    record of kind snapshot under the planned release, which never counts as a release (it isn't open, released
    or fielded); each snapshot hangs off the line's latest build at its date.
    """

    PATTERN_KINDS = ("planned", "build", "patch", "emergency", "snapshot")
    SELF_BUILD = ("planned", "patch", "emergency")

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
        if not isinstance(self_build, list) or set(self_build) - set(cls.SELF_BUILD):
            raise SourceConfigError(f"self_build must be a list drawn from {list(cls.SELF_BUILD)}")
        cb = params.get("child_builds")
        if cb is not None:
            if not (isinstance(cb, dict) and cb.get("kind") in ("patch", "emergency") and isinstance(cb.get("after"), int)
                    and is_order_group(cb.get("group") or "")):
                raise SourceConfigError('child_builds must be {"kind": "patch" or "emergency", "group": an order group '
                                        'such as "n1", "after": a number}')
            if "build" not in compiled or cb["group"] not in compiled["build"].groupindex:
                raise SourceConfigError(f"child_builds.group {cb['group']!r} isn't a group of the build pattern")
            if cb["kind"] not in compiled:
                raise SourceConfigError(f"child_builds.kind {cb['kind']!r} has no pattern")
        return compiled

    def releases(self, ci, params):
        return self.place(self.versions(ci, params), params)

    def place(self, versions, params):
        """Sort a flat version list into ReleaseRecords (and Unplaced) by the patterns in ``params``."""
        pats = self.check_params(params)
        self_build = set(params.get("self_build", ["patch", "emergency"]))
        include_archived = params.get("include_archived", True)
        cb = params.get("child_builds")
        planned, children, builds, child_builds, snapshots, out = {}, [], {}, {}, {}, []

        for v in versions:
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
            line = tuple(sorted((g, val) for g, val in groups.items() if not is_order_group(g)))
            numbers = [_number(groups[g]) for g in sorted(filter(is_order_group, groups), key=_order_index)]
            order = (numbers or [float("inf")], v.date or "", v.name)
            if kind == "planned":
                if line in planned:
                    out.append(Unplaced(v.key, v.name, f"same release line as {planned[line].name}"))
                    continue
                planned[line] = ReleaseRecord(key=v.key, name=v.name, target_date=v.date, reason=v.description,
                                              released=v.released)
            elif kind == "build":
                child = cb and _number(groups.get(cb["group"])) - cb["after"]
                if child and child > 0:                       # numbered past the release's own: a child's build
                    child_builds.setdefault((line, cb["kind"], child), []).append((order, v))
                else:
                    builds.setdefault(line, []).append((order, v))
            elif kind == "snapshot":
                snapshots.setdefault(line, []).append((order, v))
            else:
                children.append((line, kind, v, _number(groups.get("n"))))

        def line_name(line):
            return ", ".join(f"{g}={val}" for g, val in line) or "no groups"

        def ordered(found):
            return [BuildRecord(v.key, v.name, v.date) for _, v in sorted(found, key=lambda b: b[0])]

        for line, rec in planned.items():
            rec.builds = ordered(builds.pop(line, []))
            if "planned" in self_build:
                rec.builds.append(BuildRecord(rec.key, rec.name, rec.target_date))
            out.append(rec)
        for line, kind, v, n in children:
            parent = planned.get(line)
            if parent is None:
                out.append(Unplaced(v.key, v.name, f"{kind} with no planned release for {line_name(line)}"))
                continue
            rec = ReleaseRecord(key=v.key, name=v.name, kind=kind, target_date=v.date, parent_key=parent.key,
                                reason=v.description, released=v.released)
            rec.builds = ([BuildRecord(v.key, v.name, v.date)] if kind in self_build else []) + \
                ordered(child_builds.pop((line, kind, n), []))
            out.append(rec)
        for line, found in snapshots.items():
            parent = planned.get(line)
            if parent is None:
                out += [Unplaced(v.key, v.name, f"snapshot with no planned release for {line_name(line)}") for _, v in found]
                continue
            out.append(ReleaseRecord(key=f"snapshots:{parent.key}", name=f"{parent.name} snapshots", kind="snapshot",
                                     parent_key=parent.key, builds=ordered(found)))
        for line, found in builds.items():
            for _, v in found:
                out.append(Unplaced(v.key, v.name, f"build with no planned release for {line_name(line)}"))
        for (line, kind, n), found in child_builds.items():
            for _, v in found:
                out.append(Unplaced(v.key, v.name, f"build for {kind} {n} of {line_name(line)}, which isn't listed"))
        return out


def is_order_group(name):
    """``n``, ``n1``, ``n2``, ...: the groups that order builds rather than tie them to a release."""
    return re.fullmatch(r"n\d*", name) is not None


def _order_index(name):
    return int(name[1:] or 0)


def _number(value):
    return int(value) if value and value.isdigit() else float("inf")


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

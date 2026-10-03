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
from collections import defaultdict
from dataclasses import asdict, dataclass, field, fields
from typing import Iterable, List, Optional, Tuple, Union

KINDS = ("planned", "patch", "emergency")
DEFAULT_SELF_BUILD = ["patch", "emergency"]


class SourceConfigError(ValueError):
    """The CI's source_params don't make sense to the source (bad pattern, missing project, ...)."""


# ----------------------------------------------------------------------------- records a source returns

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
    def from_dict(cls, data: dict) -> "ReleaseRecord":
        """Build a record from a dict, ignoring keys that aren't fields. Builds may be dicts too."""
        field_names = {f.name for f in fields(cls)}
        known = {name: value for name, value in data.items() if name in field_names}
        known["builds"] = [_as_build(b) for b in data.get("builds") or []]
        return cls(**known)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Unplaced:
    """Something the source has but couldn't place (e.g. a build with no matching release). Shown to users."""
    key: str
    name: str
    why: str


def _as_build(build) -> BuildRecord:
    return build if isinstance(build, BuildRecord) else BuildRecord(**build)


class ReleaseSource:
    """Implement this for a release system. Called on every sync."""

    name = "jira"

    def releases(self, ci: dict, params: dict) -> Iterable[Union[ReleaseRecord, Unplaced]]:
        """Every release (with its builds) the source has for ``ci``. ``params`` is the CI's source_params."""
        raise NotImplementedError


# ----------------------------------------------------------------------------- pattern-based sources

@dataclass
class SourceVersion:
    """One entry of a flat version list, e.g. a Jira project version."""
    key: str
    name: str
    date: Optional[str] = None
    description: Optional[str] = None
    released: Optional[bool] = None
    archived: bool = False


def _as_version(version) -> SourceVersion:
    return version if isinstance(version, SourceVersion) else SourceVersion(**version)


# A release line is the values of a name's named groups, minus ``n``: e.g. (("line", "2027.Q1"),).
# Two names with the same line belong to the same planned release.
ReleaseLine = Tuple[Tuple[str, str], ...]


def _release_line(match: re.Match) -> ReleaseLine:
    return tuple(sorted((group, value) for group, value in match.groupdict().items() if group != "n"))


def _describe_line(line: ReleaseLine) -> str:
    """For messages: "line=2027.Q1"."""
    return ", ".join(f"{group}={value}" for group, value in line) or "no groups"


def _build_order(match: re.Match, version: SourceVersion):
    """Sort key for a build: by its ``n`` group as a number (builds without one go last), then date, then name."""
    n = match.groupdict().get("n")
    number = int(n) if n and n.isdigit() else float("inf")
    return number, version.date or "", version.name


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
        """Validate ``params`` and return the compiled patterns, ``{kind: regex}``."""
        params = params or {}
        patterns = params.get("patterns")
        if not isinstance(patterns, dict) or "planned" not in patterns:
            raise SourceConfigError("source_params.patterns must map at least 'planned' to a regular expression")

        unknown = set(patterns) - set(cls.PATTERN_KINDS)
        if unknown:
            raise SourceConfigError(f"unknown pattern kinds {sorted(unknown)}; use {list(cls.PATTERN_KINDS)}")

        compiled = {}
        for kind, pattern in patterns.items():
            try:
                compiled[kind] = re.compile(pattern)
            except (re.error, TypeError) as e:
                raise SourceConfigError(f"bad {kind} pattern {pattern!r}: {e}") from None

        self_build = params.get("self_build", DEFAULT_SELF_BUILD)
        if not isinstance(self_build, list) or not set(self_build) <= set(KINDS):
            raise SourceConfigError(f"self_build must be a list drawn from {list(KINDS)}")

        return compiled

    def releases(self, ci, params):
        return self.place(self.versions(ci, params), params)

    def place(self, versions, params) -> List[Union[ReleaseRecord, Unplaced]]:
        """Sort a flat version list into ReleaseRecords (and Unplaced) by the patterns in ``params``.

        Two passes: first classify every version by the one pattern it matches, then attach builds and
        patches/emergencies to the planned release on the same line.
        """
        patterns = self.check_params(params)
        self_build = set(params.get("self_build", DEFAULT_SELF_BUILD))
        include_archived = params.get("include_archived", True)

        planned = {}                      # line -> ReleaseRecord
        builds = defaultdict(list)        # line -> [(sort key, SourceVersion)]
        children = []                     # [(line, kind, SourceVersion)] for patches and emergencies
        out = []

        # Pass 1: classify.
        for version in map(_as_version, versions):
            if version.archived and not include_archived:
                continue

            matches = [(kind, m) for kind, regex in patterns.items() if (m := regex.fullmatch(version.name))]
            if not matches:
                continue                  # not a release name: ignore it
            if len(matches) > 1:
                kinds = ", ".join(kind for kind, _ in matches)
                out.append(Unplaced(version.key, version.name, f"matches more than one pattern ({kinds})"))
                continue

            kind, match = matches[0]
            line = _release_line(match)
            if kind == "planned":
                if line in planned:
                    out.append(Unplaced(version.key, version.name, f"same release line as {planned[line].name}"))
                    continue
                planned[line] = ReleaseRecord(key=version.key, name=version.name, target_date=version.date,
                                              reason=version.description, released=version.released)
            elif kind == "build":
                builds[line].append((_build_order(match, version), version))
            else:
                children.append((line, kind, version))

        # Pass 2a: planned releases take the builds on their line, in order.
        for line, release in planned.items():
            ordered = sorted(builds.pop(line, []), key=lambda pair: pair[0])
            release.builds = [BuildRecord(v.key, v.name, v.date) for _, v in ordered]
            if "planned" in self_build:
                release.builds.append(BuildRecord(release.key, release.name, release.target_date))
            out.append(release)

        # Pass 2b: patches and emergencies hang off the planned release on their line.
        for line, kind, version in children:
            parent = planned.get(line)
            if parent is None:
                why = f"{kind} with no planned release for {_describe_line(line)}"
                out.append(Unplaced(version.key, version.name, why))
                continue
            release = ReleaseRecord(key=version.key, name=version.name, kind=kind, target_date=version.date,
                                    parent_key=parent.key, reason=version.description, released=version.released)
            if kind in self_build:
                release.builds = [BuildRecord(version.key, version.name, version.date)]
            out.append(release)

        # Whatever builds are left had no planned release to go to.
        for line, orphans in builds.items():
            why = f"build with no planned release for {_describe_line(line)}"
            out.extend(Unplaced(version.key, version.name, why) for _, version in orphans)

        return out


class StaticVersionSource(PatternSource):
    """In-memory PatternSource (tests, demos, an export): ``{project: [SourceVersion or dict, ...]}``."""

    def __init__(self, projects: dict, name: str = "static"):
        self.name = name
        self.projects = {project: [_as_version(v) for v in versions] for project, versions in projects.items()}

    def versions(self, ci, params):
        project = params.get("project")
        if project not in self.projects:
            raise SourceConfigError(f"unknown project {project!r}")
        return list(self.projects[project])

"""A Jira release source for a CI.

It reads the versions of every Jira project the CI's CSCs map to, merges them by name,
and sorts them into releases and builds by the CI's name patterns
(releases.PatternSource):

    NAVL  2027.Q1  2027.Q1-b1  2027.Q1-b2  2027.Q1.P1  core-tools-3   (a CSC project)
    NAVX  2027.Q1  2027.Q1-b1  2027.Q1-b2               maps-data-9   (another one)
       -> 2027.Q1 (planned) with builds b1, b2; patch 2027.Q1.P1
          (names matching no pattern are left out)

A CSCI's CSC projects each carry a copy of its versions (one Jira project per CSC
team), so one cmtrack version is "every project's version of that name". Merging:

    key           the name: one version across projects has no single Jira id, so a
                  rename in Jira shows up as one missing and one new version at the
                  next sync (remap it in cmtrack to keep its history)
    date          the latest releaseDate among the projects (the CSCI is ready when
                  its last CSC is)
    released      every project has released it
    archived      every project has archived it
    description   the first one set, in project order

Register it with create_app({"RELEASE_SOURCES": {"jira": source}}) or
CMTRACK_RELEASE_SOURCES=jira=cmtrack.jira_releases:from_env (with the CMTRACK_JIRA_*
settings the ticket source uses). Then set the CI's release_source to "jira" and its
source_params:

    {"patterns": {"planned":   "(?P<line>\\d{4}\\.Q\\d)",
                  "build":     "(?P<line>\\d{4}\\.Q\\d)-b(?P<n>\\d+)",
                  "patch":     "(?P<line>\\d{4}\\.Q\\d)\\.P(?P<n>\\d+)"},
     "match": "2\\d{3}\\..*",
     "projects": ["NAVL", "NAVX"],
     "require_all": false}

    match         optional: only names matching this regex (whole name) are considered
    projects      optional: read these instead of the CSCs' Jira projects
    require_all   true: a name missing from any project is reported, not used

The patterns, self_build and include_archived work as in releases.PatternSource.
"""

import re

from .jira_tickets import JiraClient
from .releases import PatternSource, SourceConfigError, SourceVersion, Unplaced


class JiraReleaseSource(PatternSource):
    def __init__(self, client, name="jira"):
        self.client = client
        self.name = name

    def projects(self, ci, params):
        """The Jira projects to read.

        source_params.projects if set, else the Jira projects of the CI's CSCs.
        """
        projects = params.get("projects")
        if not projects:
            csc_projects = {
                csc["jira_project"]
                for csc in ci.get("cscs") or []
                if csc.get("jira_project")
            }
            projects = sorted(csc_projects)
        if not projects:
            raise SourceConfigError(
                "no Jira projects: map the CI's CSCs to Jira projects, "
                "or set source_params.projects"
            )
        return list(projects)

    def merged(self, ci, params):
        """(merged SourceVersions, Unplaced).

        The Unplaced are names some projects lack, when require_all is set.
        """
        name_filter = _compile_match(params.get("match"))
        projects = self.projects(ci, params)
        by_name = self._versions_by_name(projects, name_filter)

        merged, unplaced = [], []
        for name, by_project in sorted(by_name.items()):
            missing_from = [p for p in projects if p not in by_project]
            if params.get("require_all") and missing_from:
                why = f"not in {', '.join(missing_from)}"
                unplaced.append(Unplaced(name, name, why))
                continue
            in_project_order = [by_project[p] for p in projects if p in by_project]
            merged.append(_merge(name, in_project_order))
        return merged, unplaced

    def _versions_by_name(self, projects, name_filter):
        """{version name: {project: Jira version}} across ``projects``.

        Only names passing ``name_filter`` (if given) are kept.
        """
        by_name = {}
        for project in projects:
            for version in self.client.project_versions(project) or []:
                if name_filter is None or name_filter.fullmatch(version["name"]):
                    by_name.setdefault(version["name"], {})[project] = version
        return by_name

    def versions(self, ci, params):
        versions, _ = self.merged(ci, params)
        return versions

    def releases(self, ci, params):
        versions, unplaced = self.merged(ci, params)
        return self.place(versions, params) + unplaced


def _compile_match(pattern):
    if not pattern:
        return None
    try:
        return re.compile(pattern)
    except re.error as e:
        raise SourceConfigError(f"bad match pattern {pattern!r}: {e}") from None


def _merge(name, versions):
    """One SourceVersion from the same-named Jira versions of several projects.

    See the module docstring for how each field is merged.
    """
    dates = [v["releaseDate"] for v in versions if v.get("releaseDate")]
    descriptions = [v["description"] for v in versions if v.get("description")]
    return SourceVersion(
        key=name,  # no single Jira id across projects
        name=name,
        date=max(dates, default=None),  # ready when the last CSC is
        description=descriptions[0] if descriptions else None,
        released=all(v.get("released") for v in versions),
        archived=all(v.get("archived") for v in versions),
    )


def from_env():
    """For CMTRACK_RELEASE_SOURCES=jira=cmtrack.jira_releases:from_env.

    Reads CMTRACK_JIRA_URL and CMTRACK_JIRA_TOKEN (or CMTRACK_JIRA_USER +
    CMTRACK_JIRA_PASSWORD).
    """
    return JiraReleaseSource(JiraClient.from_env())

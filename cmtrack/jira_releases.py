"""A Jira release source for a CI: the versions of every Jira project its CSCs map to, merged by name and sorted
into releases and builds by the CI's name patterns (releases.PatternSource).

    NAVL  2027.Q1  2027.Q1-b1  2027.Q1-b2  2027.Q1.P1  core-tools-3      (a CSC project)
    NAVX  2027.Q1  2027.Q1-b1  2027.Q1-b2               maps-data-9      (another one)
       -> 2027.Q1 (planned) with builds b1, b2; patch 2027.Q1.P1         (names matching no pattern are left out)

A CSCI's CSC projects each carry a copy of its versions (one Jira project per CSC team), so one cmtrack version
is "every project's version of that name". Merging:

    key           the name: one version across projects has no single Jira id, so a rename in Jira shows up as
                  one missing and one new version at the next sync (remap it in cmtrack to keep its history)
    date          the latest releaseDate among the projects (the CSCI is ready when its last CSC is)
    released      every project has released it;  archived: every project has archived it
    description   the first one set, in project order

Register it (create_app({"RELEASE_SOURCES": {"jira": source}}) or
CMTRACK_RELEASE_SOURCES=jira=cmtrack.jira_releases:from_env, with the CMTRACK_JIRA_* settings the ticket source
uses), then set the CI's release_source to "jira" and its source_params:

    {"patterns": {"planned":   "(?P<line>\\d{4}\\.Q\\d)",
                  "build":     "(?P<line>\\d{4}\\.Q\\d)-b(?P<n>\\d+)",
                  "patch":     "(?P<line>\\d{4}\\.Q\\d)\\.P(?P<n>\\d+)"},
     "match": "2\\d{3}\\..*",          optional: only names matching this regex (whole name) are considered
     "projects": ["NAVL", "NAVX"],     optional: instead of the CSCs' Jira projects
     "require_all": false}             true: a name missing from any project is reported, not used

The patterns, self_build and include_archived work as in releases.PatternSource.
"""
import os
import re

from .jira_tickets import JiraClient
from .releases import PatternSource, SourceConfigError, SourceVersion, Unplaced


class JiraReleaseSource(PatternSource):
    def __init__(self, client, name="jira"):
        self.client, self.name = client, name

    def projects(self, ci, params):
        """The Jira projects to read: source_params.projects, else those of the CI's CSCs."""
        projects = params.get("projects") or sorted({c["jira_project"] for c in ci.get("cscs") or [] if c.get("jira_project")})
        if not projects:
            raise SourceConfigError("no Jira projects: map the CI's CSCs to Jira projects, or set source_params.projects")
        return list(projects)

    def merged(self, ci, params):
        """(merged SourceVersions, Unplaced for names some projects lack when require_all is set)."""
        try:
            match = re.compile(params["match"]) if params.get("match") else None
        except re.error as e:
            raise SourceConfigError(f"bad match pattern {params['match']!r}: {e}") from None
        projects = self.projects(ci, params)
        by_name = {}
        for p in projects:
            for v in self.client.project_versions(p) or []:
                if not match or match.fullmatch(v["name"]):
                    by_name.setdefault(v["name"], {})[p] = v
        out, unplaced = [], []
        for name, vs in sorted(by_name.items()):
            if params.get("require_all") and len(vs) < len(projects):
                unplaced.append(Unplaced(name, name, f"not in {', '.join(p for p in projects if p not in vs)}"))
                continue
            ordered = [vs[p] for p in projects if p in vs]
            dates = [v["releaseDate"] for v in ordered if v.get("releaseDate")]
            out.append(SourceVersion(key=name, name=name, date=max(dates) if dates else None,
                                     description=next((v["description"] for v in ordered if v.get("description")), None),
                                     released=all(v.get("released") for v in ordered),
                                     archived=all(v.get("archived") for v in ordered)))
        return out, unplaced

    def versions(self, ci, params):
        return self.merged(ci, params)[0]

    def releases(self, ci, params):
        versions, unplaced = self.merged(ci, params)
        return self.place(versions, params) + unplaced


def from_env():
    """CMTRACK_RELEASE_SOURCES=jira=cmtrack.jira_releases:from_env, with CMTRACK_JIRA_URL and CMTRACK_JIRA_TOKEN
    (or CMTRACK_JIRA_USER + CMTRACK_JIRA_PASSWORD)."""
    env = os.environ.get
    return JiraReleaseSource(JiraClient(env("CMTRACK_JIRA_URL"), env("CMTRACK_JIRA_TOKEN"), env("CMTRACK_JIRA_USER"),
                                        env("CMTRACK_JIRA_PASSWORD")))

"""The "manual" release source: a CI's versions kept in cmtrack instead of Jira.

It works like the Jira release source, with cmtrack as the version list: add versions by name (releases,
builds, patches and emergencies alike, e.g. 2027.Q1, 2027.Q1-b1, 2027.Q1.P1), and the CI's name patterns
(releases.PatternSource) sort them into releases and builds on sync. Set a CI's release_source to "manual" and
its source_params to the patterns:

    {"patterns": {"planned":   "(?P<line>\\d{4}\\.Q\\d)",
                  "build":     "(?P<line>\\d{4}\\.Q\\d)-b(?P<n>\\d+)",
                  "patch":     "(?P<line>\\d{4}\\.Q\\d)\\.P(?P<n>\\d+)",
                  "emergency": "(?P<line>\\d{4}\\.Q\\d)\\.ER(?P<n>\\d+)"}}

The API (/api/cis/<ci>/manual-versions, /api/manual-versions/<id>) and the CI page add, edit and remove versions,
and sync the CI after each change. A version's key is its row id, so a rename here is a rename in cmtrack; removing
one leaves its release or build flagged missing, as a version deleted in Jira would. Because the patterns are
the same, a CI can move between "manual" and "jira" and keep its releases (same-named ones are adopted).

create_app registers this source as "manual" unless RELEASE_SOURCES already names one.
"""
import json

from . import service as svc
from .releases import PatternSource, SourceConfigError, SourceVersion

NAME = "manual"
FIELDS = ("name", "date", "description", "released", "archived")


class ManualReleaseSource(PatternSource):
    def __init__(self, connect=None, name=NAME):
        """``connect``: () -> the sqlite connection to read (default: the request's, cmtrack.db.get_db)."""
        self.name, self.connect = name, connect

    def _conn(self):
        if self.connect:
            return self.connect()
        from .db import get_db
        return get_db()

    def versions(self, ci, params):
        return [SourceVersion(key=str(r["id"]), name=r["name"], date=r["date"], description=r["description"],
                              released=bool(r["released"]), archived=bool(r["archived"]))
                for r in list_versions(self._conn(), ci["id"])]


# ----------------------------------------------------------------------------- the version list

def list_versions(conn, ci_ref):
    ci = svc.get_ci(conn, ci_ref)
    return conn.execute("SELECT * FROM manual_version WHERE ci_id = ? ORDER BY COALESCE(date, '9999'), name",
                        (ci["id"],)).fetchall()


def get_version(conn, mid):
    return svc._one(conn, "SELECT * FROM manual_version WHERE id = ?", (mid,), f"manual version {mid}")


def _params(ci):
    """A CI's source_params, from a row (JSON text) or a detail dict (already parsed)."""
    p = ci["source_params"]
    return p if isinstance(p, dict) else json.loads(p or "{}")


def kind_of(ci, name):
    """Which pattern of the CI's source_params a name matches ('planned', 'build', ...), or None."""
    try:
        pats = PatternSource.check_params(_params(ci))
    except SourceConfigError:
        return None
    hits = [k for k, rx in pats.items() if rx.fullmatch(name)]
    return hits[0] if len(hits) == 1 else None


def _check(conn, ci, name, mid=None):
    name = (name or "").strip()
    if not name:
        raise svc.CMError("a version needs a name")
    if ci["release_source"] != NAME:
        raise svc.CMError(f"{ci['name']} doesn't use the manual release source (its release_source is "
                          f"{ci['release_source'] or 'not set'})")
    try:
        PatternSource.check_params(_params(ci))
    except SourceConfigError as e:
        raise svc.CMError(f"{ci['name']}: {e}") from None
    if kind_of(ci, name) is None:
        raise svc.CMError(f"{name!r} matches none of {ci['name']}'s name patterns (or more than one)")
    clash = conn.execute("SELECT id FROM manual_version WHERE ci_id = ? AND name = ? AND id IS NOT ?",
                         (ci["id"], name, mid)).fetchone()
    if clash:
        raise svc.Conflict(f"{ci['name']} already has a version {name!r}")
    return name


def add_version(conn, ci_ref, name, date=None, description=None, released=False, archived=False):
    ci = svc.get_ci(conn, ci_ref)
    name = _check(conn, ci, name)
    cur = conn.execute("INSERT INTO manual_version (ci_id, name, date, description, released, archived) "
                       "VALUES (?, ?, ?, ?, ?, ?)", (ci["id"], name, svc._date(date), (description or "").strip() or None,
                                                     1 if released else 0, 1 if archived else 0))
    svc.log(conn, "ci", ci["id"], "manual_version_added", version=name)
    return get_version(conn, cur.lastrowid)


def update_version(conn, mid, **fields):
    row = get_version(conn, mid)
    unknown = set(fields) - set(FIELDS)
    if unknown:
        raise svc.CMError(f"cannot update {sorted(unknown)}; use {list(FIELDS)}")
    ci = svc.get_ci(conn, row["ci_id"])
    sets = {}
    if "name" in fields:
        sets["name"] = _check(conn, ci, fields["name"], row["id"])
    if "date" in fields:
        sets["date"] = svc._date(fields["date"])
    if "description" in fields:
        sets["description"] = (fields["description"] or "").strip() or None
    for flag in ("released", "archived"):
        if flag in fields:
            sets[flag] = 1 if fields[flag] not in (False, 0, "0", "false", "", None) else 0
    changed = {k: v for k, v in sets.items() if row[k] != v}
    if changed:
        conn.execute(f"UPDATE manual_version SET {', '.join(k + ' = ?' for k in changed)} WHERE id = ?",
                     (*changed.values(), row["id"]))
        svc.log(conn, "ci", ci["id"], "manual_version_updated", version=row["name"], **changed)
    return get_version(conn, row["id"])


def delete_version(conn, mid):
    row = get_version(conn, mid)
    conn.execute("DELETE FROM manual_version WHERE id = ?", (row["id"],))
    svc.log(conn, "ci", row["ci_id"], "manual_version_removed", version=row["name"])
    return row


def sync(conn, ci_ref, sources):
    """Sync a manual CI after a change to its version list (``sources``: the app's RELEASE_SOURCES)."""
    ci = svc.get_ci(conn, ci_ref)
    source = (sources or {}).get(ci["release_source"])
    return svc.sync_ci(conn, source, ci["id"]) if isinstance(source, ManualReleaseSource) else None


def init_app(app):
    sources = app.config["RELEASE_SOURCES"] or {}
    if NAME not in sources:
        app.config["RELEASE_SOURCES"] = {**sources, NAME: ManualReleaseSource()}

"""Domain logic.

Every function takes an open sqlite3 connection and does NOT commit: the caller
owns the transaction (the API wraps each request in ``with conn:``; scripts
should do the same). References to CIs and IFCs accept an id or a name.
"""
import datetime as dt
import json
import sqlite3

from . import policies
from .policies import PolicyError, add_months, parse_date

RELEASED = ("released", "external")          # version states a baseline may reference
VERSION_TRANSITIONS = {
    "planned": {"built", "rejected"},
    "built": {"tested", "rejected"},
    "tested": {"rejected"},
}                                            # 'released' only via release_version()


class CMError(Exception):
    status = 400

    def __init__(self, message, problems=None):
        super().__init__(message)
        self.message = message
        self.problems = problems or []


class NotFound(CMError):
    status = 404


class Conflict(CMError):
    status = 409


# ----------------------------------------------------------------------------- helpers

_JSON_COLS = {"attributes", "params", "detail"}


def to_dict(row):
    if row is None:
        return None
    d = dict(row)
    for k in _JSON_COLS & d.keys():
        if isinstance(d[k], str):
            d[k] = json.loads(d[k])
    return d


def to_dicts(rows):
    return [to_dict(r) for r in rows]


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def log(conn, entity, entity_id, action, **detail):
    conn.execute("INSERT INTO event (entity, entity_id, action, detail) VALUES (?, ?, ?, ?)",
                 (entity, entity_id, action, json.dumps(detail, default=str)))


def _one(conn, sql, args, what):
    row = conn.execute(sql, args).fetchone()
    if row is None:
        raise NotFound(f"{what} not found")
    return row


def _by_ref(conn, table, ref, what):
    ref = str(ref)
    col = "id" if ref.isdigit() else "name"
    return _one(conn, f"SELECT * FROM {table} WHERE {col} = ?", (ref,), f"{what} {ref!r}")


def _date(value, what="date"):
    try:
        return parse_date(value, what)
    except PolicyError as e:
        raise CMError(str(e)) from None


# ----------------------------------------------------------------------------- policies

def create_policy(conn, name, type, params=None, base_dir=None):
    if not name:
        raise CMError("name is required")
    try:
        policies.build(type, params, base_dir)          # validate
        cur = conn.execute("INSERT INTO policy (name, type, params) VALUES (?, ?, ?)",
                           (name, type, json.dumps(params or {})))
    except PolicyError as e:
        raise CMError(str(e)) from None
    except sqlite3.IntegrityError:
        raise Conflict(f"policy {name!r} already exists") from None
    log(conn, "policy", cur.lastrowid, "created", type=type, params=params or {})
    return get_policy(conn, cur.lastrowid)


def get_policy(conn, ref):
    return _by_ref(conn, "policy", ref, "policy")


def list_policies(conn):
    return conn.execute("SELECT * FROM policy ORDER BY name").fetchall()


def policy_for(conn, ci, base_dir=None):
    if ci["policy_id"] is None:
        return policies.Policy()
    row = get_policy(conn, ci["policy_id"])
    try:
        return policies.build(row["type"], json.loads(row["params"]), base_dir)
    except PolicyError as e:
        raise CMError(f"policy {row['name']!r}: {e}") from None


# ----------------------------------------------------------------------------- configuration items

def get_ci(conn, ref):
    return _by_ref(conn, "ci", ref, "CI")


def list_cis(conn, type=None, managed=None):
    sql, args = "SELECT * FROM ci WHERE 1=1", []
    if type:
        sql += " AND type = ?"
        args.append(type)
    if managed is not None:
        sql += " AND managed = ?"
        args.append(1 if managed else 0)
    return conn.execute(sql + " ORDER BY name", args).fetchall()


def create_ci(conn, name, type="CSCI", kind="simple", managed=True, policy=None,
              description=None, attributes=None):
    if not name:
        raise CMError("name is required")
    if type not in ("CSCI", "HWCI"):
        raise CMError("type must be CSCI or HWCI")
    if kind not in ("simple", "composite"):
        raise CMError("kind must be simple or composite")
    policy_id = get_policy(conn, policy)["id"] if policy else None
    try:
        cur = conn.execute(
            "INSERT INTO ci (name, type, kind, managed, policy_id, description, attributes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, type, kind, 1 if managed else 0, policy_id, description, json.dumps(attributes or {})))
    except sqlite3.IntegrityError:
        raise Conflict(f"CI {name!r} already exists") from None
    log(conn, "ci", cur.lastrowid, "created", name=name, type=type, kind=kind, managed=bool(managed))
    return get_ci(conn, cur.lastrowid)


def update_ci(conn, ref, **fields):
    """Update managed/kind/policy/description/attributes (e.g. adopt a placeholder)."""
    ci = get_ci(conn, ref)
    allowed = {"managed", "kind", "policy", "description", "attributes"}
    unknown = set(fields) - allowed
    if unknown:
        raise CMError(f"cannot update {sorted(unknown)}")
    sets = {}
    if "managed" in fields:
        sets["managed"] = 1 if fields["managed"] else 0
    if "kind" in fields:
        if fields["kind"] not in ("simple", "composite"):
            raise CMError("kind must be simple or composite")
        sets["kind"] = fields["kind"]
    if "policy" in fields:
        sets["policy_id"] = get_policy(conn, fields["policy"])["id"] if fields["policy"] else None
    if "description" in fields:
        sets["description"] = fields["description"]
    if "attributes" in fields:
        sets["attributes"] = json.dumps(fields["attributes"] or {})
    if sets:
        conn.execute(f"UPDATE ci SET {', '.join(k + ' = ?' for k in sets)} WHERE id = ?",
                     (*sets.values(), ci["id"]))
        log(conn, "ci", ci["id"], "updated", **fields)
    return get_ci(conn, ci["id"])


def ci_detail(conn, ref):
    ci = get_ci(conn, ref)
    out = to_dict(ci)
    out["policy"] = to_dict(get_policy(conn, ci["policy_id"])) if ci["policy_id"] else None
    out["cscs"] = to_dicts(conn.execute("SELECT * FROM csc WHERE ci_id = ? ORDER BY name", (ci["id"],)))
    out["releases"] = list_releases(conn, ci["id"])
    return out


# ----------------------------------------------------------------------------- CSCs (Jira mapping)

def add_csc(conn, ci_ref, name, jira_project=None, affected_product=None, team=None):
    ci = get_ci(conn, ci_ref)
    if not name:
        raise CMError("name is required")
    if bool(jira_project) != bool(affected_product):
        raise CMError("jira_project and affected_product must be given together")
    try:
        cur = conn.execute(
            "INSERT INTO csc (ci_id, name, jira_project, affected_product, team) VALUES (?, ?, ?, ?, ?)",
            (ci["id"], name, jira_project, affected_product, team))
    except sqlite3.IntegrityError:
        raise Conflict(f"CSC {name!r} or Jira pair ({jira_project}, {affected_product}) already mapped") from None
    log(conn, "csc", cur.lastrowid, "created", ci=ci["name"], name=name,
        jira_project=jira_project, affected_product=affected_product)
    return conn.execute("SELECT * FROM csc WHERE id = ?", (cur.lastrowid,)).fetchone()


def find_csc(conn, jira_project, affected_product):
    """Resolve a Jira (project, affected product) pair to its CSC and CSCI."""
    return _one(conn,
                "SELECT csc.*, ci.name AS ci_name FROM csc JOIN ci ON ci.id = csc.ci_id "
                "WHERE jira_project = ? AND affected_product = ?",
                (jira_project, affected_product),
                f"CSC for ({jira_project}, {affected_product})")


# ----------------------------------------------------------------------------- planning

def plan_ci(conn, ci_ref, start=None, end=None, base_dir=None):
    """Apply the CI's policy: create/update planned releases and version slots. Idempotent."""
    ci = get_ci(conn, ci_ref)
    pol = policy_for(conn, ci, base_dir)
    start = _date(start, "start") or dt.date.today()
    end = _date(end, "end") or add_months(start, 12, start.day if start.day <= 28 else 28)
    try:
        planned = pol.plan(start, end)
    except PolicyError as e:
        raise CMError(str(e)) from None

    summary = {"ci": ci["name"], "policy": pol.type, "created_releases": [], "updated_releases": [],
               "created_versions": [], "updated_versions": []}
    for pr in planned:
        rel = conn.execute("SELECT * FROM release WHERE ci_id = ? AND name = ?", (ci["id"], pr.name)).fetchone()
        if rel is None:
            cur = conn.execute(
                "INSERT INTO release (ci_id, name, kind, target_date, source) VALUES (?, ?, 'planned', ?, 'policy')",
                (ci["id"], pr.name, pr.target_date))
            rel = get_release(conn, cur.lastrowid)
            summary["created_releases"].append(pr.name)
        elif rel["status"] == "planned" and pr.target_date and rel["target_date"] != pr.target_date:
            conn.execute("UPDATE release SET target_date = ? WHERE id = ?", (pr.target_date, rel["id"]))
            summary["updated_releases"].append(pr.name)

        for pv in pr.versions:
            ver = conn.execute("SELECT * FROM version WHERE ci_id = ? AND name = ?", (ci["id"], pv.name)).fetchone()
            if ver is None:
                _insert_version(conn, rel, pv.name, pv.planned_date, planned=True)
                summary["created_versions"].append(pv.name)
            elif ver["release_id"] != rel["id"]:
                raise Conflict(f"version {pv.name} already belongs to another release of {ci['name']}")
            elif ver["status"] == "planned" and pv.planned_date and ver["planned_date"] != pv.planned_date:
                conn.execute("UPDATE version SET planned_date = ? WHERE id = ?", (pv.planned_date, ver["id"]))
                summary["updated_versions"].append(pv.name)

    log(conn, "ci", ci["id"], "planned", start=start, end=end,
        **{k: v for k, v in summary.items() if k.startswith(("created", "updated"))})
    return summary


# ----------------------------------------------------------------------------- releases & versions

def get_release(conn, release_id):
    return _one(conn, "SELECT * FROM release WHERE id = ?", (release_id,), f"release {release_id}")


def get_version(conn, version_id):
    return _one(conn, "SELECT * FROM version WHERE id = ?", (version_id,), f"version {version_id}")


def find_version(conn, ci, ref):
    """Version of ``ci`` by id or name."""
    ref = str(ref)
    col = "id" if ref.isdigit() else "name"
    return _one(conn, f"SELECT * FROM version WHERE ci_id = ? AND {col} = ?", (ci["id"], ref),
                f"version {ref!r} of {ci['name']}")


def list_releases(conn, ci_ref):
    ci = get_ci(conn, ci_ref)
    rows = conn.execute(
        """SELECT r.*, rv.name AS released_version,
                  (SELECT COUNT(*) FROM version v WHERE v.release_id = r.id AND v.planned = 1) AS planned_versions,
                  (SELECT COUNT(*) FROM version v WHERE v.release_id = r.id AND v.planned = 0) AS unplanned_versions
           FROM release r LEFT JOIN version rv ON rv.id = r.released_version_id
           WHERE r.ci_id = ? ORDER BY COALESCE(r.target_date, r.created_at), r.id""", (ci["id"],))
    return to_dicts(rows)


def release_detail(conn, release_id):
    rel = get_release(conn, release_id)
    out = to_dict(rel)
    out["ci"] = get_ci(conn, rel["ci_id"])["name"]
    out["versions"] = to_dicts(conn.execute(
        "SELECT * FROM version WHERE release_id = ? ORDER BY seq", (rel["id"],)))
    out["parent"] = get_release(conn, rel["parent_id"])["name"] if rel["parent_id"] else None
    out["base_version"] = get_version(conn, rel["base_version_id"])["name"] if rel["base_version_id"] else None
    out["children"] = to_dicts(conn.execute(
        "SELECT id, name, kind, status, reason, released_at FROM release WHERE parent_id = ? ORDER BY id",
        (rel["id"],)))
    out["released_version"] = get_version(conn, rel["released_version_id"])["name"] \
        if rel["released_version_id"] else None
    eff = effective_version(conn, rel["id"])
    out["effective_version"] = eff["name"] if eff else None
    out["baselines_behind"] = behind_effective(conn, rel["id"])
    return out


def _insert_version(conn, rel, name, planned_date=None, planned=False, status="planned"):
    seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM version WHERE release_id = ?",
                       (rel["id"],)).fetchone()[0]
    try:
        cur = conn.execute(
            "INSERT INTO version (release_id, ci_id, seq, name, status, planned, planned_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (rel["id"], rel["ci_id"], seq, name, status, 1 if planned else 0, planned_date))
    except sqlite3.IntegrityError:
        raise Conflict(f"version {name!r} already exists for this CI") from None
    log(conn, "version", cur.lastrowid, "created", release=rel["name"], name=name, planned=planned)
    return get_version(conn, cur.lastrowid)


def add_version(conn, release_id, name=None, planned_date=None, base_dir=None):
    """Add an ad hoc version (e.g. a 4th build after a failed candidate). Counted as variance."""
    rel = get_release(conn, release_id)
    if rel["status"] in ("released", "cancelled"):
        raise CMError(f"release {rel['name']} is {rel['status']}")
    if not name:
        seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM version WHERE release_id = ?",
                           (rel["id"],)).fetchone()[0]
        name = policy_for(conn, get_ci(conn, rel["ci_id"]), base_dir).version_name(rel, seq)
    planned_date = _date(planned_date, "planned_date")
    return _insert_version(conn, rel, name, planned_date.isoformat() if planned_date else None)


def update_version(conn, version_id, status=None, artifact_ref=None, built_at=None):
    ver = get_version(conn, version_id)
    sets = {}
    if status and status != ver["status"]:
        if status not in VERSION_TRANSITIONS.get(ver["status"], ()):
            raise CMError(f"cannot move version {ver['name']} from {ver['status']} to {status}")
        sets["status"] = status
        if status == "built":
            sets["built_at"] = built_at or now()
    if artifact_ref is not None:
        sets["artifact_ref"] = artifact_ref
    if sets:
        conn.execute(f"UPDATE version SET {', '.join(k + ' = ?' for k in sets)} WHERE id = ?",
                     (*sets.values(), ver["id"]))
        if sets.get("status") == "built":
            conn.execute("UPDATE release SET status = 'active' WHERE id = ? AND status = 'planned'",
                         (ver["release_id"],))
        log(conn, "version", ver["id"], "updated", **sets)
    return get_version(conn, ver["id"])


def release_version(conn, version_id, base_dir=None):
    """Promote a version to be its release. Enforces the policy gate and composite rules."""
    ver = get_version(conn, version_id)
    rel = get_release(conn, ver["release_id"])
    ci = get_ci(conn, ver["ci_id"])
    if rel["status"] in ("released", "cancelled"):
        raise CMError(f"release {rel['name']} is already {rel['status']}")

    problems = policy_for(conn, ci, base_dir).release_gate(dict(ver))
    if ci["kind"] == "composite":
        children = conn.execute(
            "SELECT v.name, v.status, c.name AS ci FROM manifest_entry m "
            "JOIN version v ON v.id = m.child_version_id JOIN ci c ON c.id = v.ci_id "
            "WHERE m.parent_version_id = ?", (ver["id"],)).fetchall()
        if not children:
            problems.append("composite version has an empty manifest")
        problems += [f"child {c['ci']} {c['name']} is {c['status']}, not released"
                     for c in children if c["status"] not in RELEASED]
    if problems:
        raise CMError(f"cannot release {ver['name']}", problems)

    conn.execute("UPDATE version SET status = 'released' WHERE id = ?", (ver["id"],))
    conn.execute("UPDATE release SET status = 'released', released_version_id = ?, released_at = ? WHERE id = ?",
                 (ver["id"], now(), rel["id"]))
    log(conn, "release", rel["id"], "released", version=ver["name"])
    return release_detail(conn, rel["id"])


def root_release(conn, release_id):
    """The planned (or external) release a patch/emergency family hangs off."""
    rel = get_release(conn, release_id)
    while rel["parent_id"] is not None:
        rel = get_release(conn, rel["parent_id"])
    return rel


def effective_version(conn, release_id):
    """Latest released version in the release's family (root + its patches/emergencies).

    The root's released_version_id never changes; this is what should be fielded now.
    """
    root = root_release(conn, release_id)
    return conn.execute(
        "SELECT v.* FROM release r JOIN version v ON v.id = r.released_version_id "
        "WHERE (r.id = ? OR r.parent_id = ?) AND r.status = 'released' "
        "ORDER BY r.released_at DESC, r.id DESC LIMIT 1", (root["id"], root["id"])).fetchone()


def behind_effective(conn, release_id):
    """Current approved baselines that still field an older version of this release family."""
    root = root_release(conn, release_id)
    eff = effective_version(conn, root["id"])
    if eff is None:
        return []
    return to_dicts(conn.execute(
        "SELECT b.id, b.name, i.name AS ifc, v.name AS fielded_version FROM baseline b "
        "JOIN ifc i ON i.id = b.ifc_id JOIN baseline_entry e ON e.baseline_id = b.id "
        "JOIN version v ON v.id = e.version_id JOIN release r ON r.id = v.release_id "
        "WHERE b.status = 'approved' AND (r.id = ? OR r.parent_id = ?) AND v.id != ? "
        "ORDER BY i.name", (root["id"], root["id"], eff["id"])))


def spawn_release(conn, release_id, kind, reason=None, base_version=None, target_date=None, base_dir=None):
    """Spawn a patch/emergency release on an already-promoted release.

    Children always attach to the root release, so names count up per release line
    (2026.Q4.ER1, 2026.Q4.ER2) even when spawned from a child. base_version defaults to
    the family's effective version, so ER2 builds on ER1. Creates one version slot
    named after the new release.
    """
    # Take the write lock before reading, so two concurrent spawns can't both pass the checks below.
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    root = root_release(conn, release_id)
    ci = get_ci(conn, root["ci_id"])
    pol = policy_for(conn, ci, base_dir)
    reason = (reason or "").strip() or None
    if kind not in pol["spawn_kinds"]:
        raise CMError(f"{kind!r} releases not allowed by policy (allowed: {pol['spawn_kinds']})")
    if kind == "emergency" and not reason:
        raise CMError("emergency releases require a reason / change request")
    if root["status"] != "released":
        raise CMError(f"release {root['name']} has not been promoted yet; "
                      f"spawn the {kind} from the latest released release instead")

    # Guard 1: same change request on the same release line -> return the existing one (idempotent).
    if reason:
        existing = conn.execute(
            "SELECT id FROM release WHERE parent_id = ? AND kind = ? AND reason = ? AND status != 'cancelled'",
            (root["id"], kind, reason)).fetchone()
        if existing:
            out = release_detail(conn, existing["id"])
            out["spawned"] = False
            return out

    if base_version is not None:
        base = find_version(conn, ci, base_version)
        family = root_release(conn, base["release_id"])["id"]
        if family != root["id"]:
            raise CMError(f"base version {base['name']} is not part of release {root['name']}")
        if base["status"] not in RELEASED:
            raise CMError(f"base version {base['name']} is {base['status']}, not released")
    else:
        base = effective_version(conn, root["id"])

    # Guard 2: limit concurrently open children of this kind (default 1); cancel an abandoned one first.
    limit = (pol["max_open"] or {}).get(kind)
    if limit is not None:
        open_ = conn.execute(
            "SELECT name, reason FROM release WHERE parent_id = ? AND kind = ? AND status IN ('planned', 'active')",
            (root["id"], kind)).fetchall()
        if len(open_) >= limit:
            raise Conflict(f"{root['name']} already has {len(open_)} open {kind} release(s); "
                           f"finish or cancel before spawning another",
                           [f"{r['name']} ({r['reason'] or 'no reason'})" for r in open_])

    parent = root
    n = conn.execute("SELECT COUNT(*) FROM release WHERE parent_id = ? AND kind = ?",
                     (root["id"], kind)).fetchone()[0] + 1
    name = pol.child_release_name(kind, root["name"], base["name"], n)
    target = _date(target_date, "target_date")
    try:
        cur = conn.execute(
            "INSERT INTO release (ci_id, name, kind, parent_id, base_version_id, target_date, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ci["id"], name, kind, parent["id"], base["id"], target.isoformat() if target else None, reason))
    except sqlite3.IntegrityError:
        raise Conflict(f"release {name!r} or an open {kind} for {reason!r} already exists") from None
    rel = get_release(conn, cur.lastrowid)
    log(conn, "release", rel["id"], "spawned", kind=kind, parent=parent["name"], base=base["name"], reason=reason)
    _insert_version(conn, rel, name, rel["target_date"])
    out = release_detail(conn, rel["id"])
    out["spawned"] = True
    return out


def cancel_release(conn, release_id, note=None):
    """Cancel an unreleased release (an abandoned emergency, a skipped quarter).

    Its unfinished versions are rejected. The name stays used, so the next
    emergency on the line still gets the next number (no ER number reuse).
    """
    rel = get_release(conn, release_id)
    if rel["status"] in ("released", "cancelled"):
        raise CMError(f"release {rel['name']} is already {rel['status']}")
    conn.execute("UPDATE release SET status = 'cancelled' WHERE id = ?", (rel["id"],))
    conn.execute("UPDATE version SET status = 'rejected' WHERE release_id = ? AND status IN ('planned', 'built', 'tested')",
                 (rel["id"],))
    log(conn, "release", rel["id"], "cancelled", note=note)
    return release_detail(conn, rel["id"])


# ----------------------------------------------------------------------------- composites

def set_manifest(conn, version_id, children):
    """Pin child versions into a composite CI's version. ``children``: [{ci, version}]."""
    ver = get_version(conn, version_id)
    ci = get_ci(conn, ver["ci_id"])
    if ci["kind"] != "composite":
        raise CMError(f"{ci['name']} is not a composite CI")
    if ver["status"] in ("released", "rejected"):
        raise CMError(f"version {ver['name']} is {ver['status']}; manifest is frozen")
    resolved, seen = [], set()
    for child in children:
        child_ci = get_ci(conn, child.get("ci"))
        if child_ci["id"] == ci["id"]:
            raise CMError("a composite cannot contain itself")
        if child_ci["id"] in seen:
            raise CMError(f"{child_ci['name']} listed twice")
        seen.add(child_ci["id"])
        resolved.append(find_version(conn, child_ci, child.get("version")))
    conn.execute("DELETE FROM manifest_entry WHERE parent_version_id = ?", (ver["id"],))
    conn.executemany("INSERT INTO manifest_entry VALUES (?, ?)", [(ver["id"], c["id"]) for c in resolved])
    log(conn, "version", ver["id"], "manifest_set", children=[c["name"] for c in resolved])
    return manifest(conn, ver["id"])


def manifest(conn, version_id):
    return to_dicts(conn.execute(
        "SELECT c.name AS ci, v.id AS version_id, v.name AS version, v.status FROM manifest_entry m "
        "JOIN version v ON v.id = m.child_version_id JOIN ci c ON c.id = v.ci_id "
        "WHERE m.parent_version_id = ? ORDER BY c.name", (version_id,)))


def where_used(conn, version_id):
    """Composites that pin this version (transitively) and baselines that contain any of them."""
    ver = get_version(conn, version_id)
    up = """WITH RECURSIVE up(vid) AS (
                SELECT ? UNION SELECT m.parent_version_id FROM manifest_entry m JOIN up ON m.child_version_id = up.vid)"""
    composites = conn.execute(
        up + " SELECT c.name AS ci, v.id, v.name AS version FROM up JOIN version v ON v.id = up.vid "
             "JOIN ci c ON c.id = v.ci_id WHERE up.vid != ?", (ver["id"], ver["id"]))
    baselines = conn.execute(
        up + " SELECT DISTINCT b.id, b.name, b.status, i.name AS ifc, v.name AS via_version FROM up "
             "JOIN baseline_entry e ON e.version_id = up.vid JOIN baseline b ON b.id = e.baseline_id "
             "JOIN ifc i ON i.id = b.ifc_id JOIN version v ON v.id = up.vid ORDER BY i.name, b.id", (ver["id"],))
    return {"version": ver["name"], "composites": to_dicts(composites), "baselines": to_dicts(baselines)}


# ----------------------------------------------------------------------------- IFCs

def get_ifc(conn, ref):
    return _by_ref(conn, "ifc", ref, "IFC")


def create_ifc(conn, name, parent=None, description=None):
    if not name:
        raise CMError("name is required")
    parent_id = get_ifc(conn, parent)["id"] if parent else None
    try:
        cur = conn.execute("INSERT INTO ifc (name, parent_id, description) VALUES (?, ?, ?)",
                           (name, parent_id, description))
    except sqlite3.IntegrityError:
        raise Conflict(f"IFC {name!r} already exists") from None
    log(conn, "ifc", cur.lastrowid, "created", name=name, parent=parent)
    return get_ifc(conn, cur.lastrowid)


def set_ifc_parent(conn, ref, parent):
    ifc = get_ifc(conn, ref)
    parent_id = get_ifc(conn, parent)["id"] if parent else None
    if parent_id is not None and (parent_id == ifc["id"] or
                                  ifc["id"] in [a["id"] for a in _ancestors(conn, parent_id)]):
        raise CMError("that parent would create a cycle")
    conn.execute("UPDATE ifc SET parent_id = ? WHERE id = ?", (parent_id, ifc["id"]))
    log(conn, "ifc", ifc["id"], "reparented", parent=parent)
    return get_ifc(conn, ifc["id"])


def _ancestors(conn, ifc_id):
    return conn.execute(
        """WITH RECURSIVE a(id, parent_id, name, depth) AS (
               SELECT id, parent_id, name, 0 FROM ifc WHERE id = (SELECT parent_id FROM ifc WHERE id = ?)
               UNION ALL SELECT i.id, i.parent_id, i.name, a.depth + 1 FROM ifc i JOIN a ON i.id = a.parent_id
               WHERE a.depth < 50)
           SELECT id, name FROM a ORDER BY depth""", (ifc_id,)).fetchall()


def list_ifcs(conn):
    return conn.execute(
        "SELECT i.*, p.name AS parent FROM ifc i LEFT JOIN ifc p ON p.id = i.parent_id ORDER BY i.name").fetchall()


def ifc_detail(conn, ref):
    ifc = get_ifc(conn, ref)
    out = to_dict(ifc)
    out["ancestors"] = [a["name"] for a in _ancestors(conn, ifc["id"])]
    out["children"] = [r["name"] for r in conn.execute(
        "SELECT name FROM ifc WHERE parent_id = ? ORDER BY name", (ifc["id"],))]
    out["baselines"] = to_dicts(conn.execute(
        "SELECT id, name, status, source, source_ref, created_at, approved_at FROM baseline "
        "WHERE ifc_id = ? ORDER BY id", (ifc["id"],)))
    current = current_baseline(conn, ifc["id"])
    out["current_hscm"] = baseline_detail(conn, current["id"]) if current else None
    return out


# ----------------------------------------------------------------------------- baselines (HSCM)

def get_baseline(conn, baseline_id):
    return _one(conn, "SELECT * FROM baseline WHERE id = ?", (baseline_id,), f"baseline {baseline_id}")


def current_baseline(conn, ifc_id):
    return conn.execute("SELECT * FROM baseline WHERE ifc_id = ? AND status = 'approved' "
                        "ORDER BY approved_at DESC, id DESC LIMIT 1", (ifc_id,)).fetchone()


def baseline_entries(conn, baseline_id):
    return to_dicts(conn.execute(
        "SELECT c.name AS ci, c.type, c.managed, v.id AS version_id, v.name AS version, v.status "
        "FROM baseline_entry e JOIN ci c ON c.id = e.ci_id JOIN version v ON v.id = e.version_id "
        "WHERE e.baseline_id = ? ORDER BY c.type, c.name", (baseline_id,)))


def baseline_detail(conn, baseline_id):
    b = get_baseline(conn, baseline_id)
    out = to_dict(b)
    out["ifc"] = get_ifc(conn, b["ifc_id"])["name"]
    out["supersedes"] = get_baseline(conn, b["supersedes_id"])["name"] if b["supersedes_id"] else None
    out["entries"] = baseline_entries(conn, b["id"])
    return out


def _resolve_entries(conn, entries):
    resolved = {}
    for e in entries:
        ci = get_ci(conn, e.get("ci"))
        resolved[ci["id"]] = find_version(conn, ci, e.get("version"))
    return resolved


def _write_entries(conn, baseline_id, resolved):
    conn.execute("DELETE FROM baseline_entry WHERE baseline_id = ?", (baseline_id,))
    conn.executemany("INSERT INTO baseline_entry (baseline_id, ci_id, version_id) VALUES (?, ?, ?)",
                     [(baseline_id, ci_id, v["id"]) for ci_id, v in resolved.items()])


def create_baseline(conn, ifc_ref, name, entries=(), source="manual", source_ref=None):
    ifc = get_ifc(conn, ifc_ref)
    if not name:
        raise CMError("name is required")
    resolved = _resolve_entries(conn, entries)
    prev = current_baseline(conn, ifc["id"])
    try:
        cur = conn.execute(
            "INSERT INTO baseline (ifc_id, name, supersedes_id, source, source_ref) VALUES (?, ?, ?, ?, ?)",
            (ifc["id"], name, prev["id"] if prev else None, source, source_ref))
    except sqlite3.IntegrityError:
        raise Conflict(f"baseline {name!r} already exists for {ifc['name']}") from None
    _write_entries(conn, cur.lastrowid, resolved)
    log(conn, "baseline", cur.lastrowid, "created", ifc=ifc["name"], name=name, source=source)
    return baseline_detail(conn, cur.lastrowid)


def set_baseline_entries(conn, baseline_id, entries):
    b = get_baseline(conn, baseline_id)
    if b["status"] != "draft":
        raise CMError(f"baseline {b['name']} is {b['status']}; clone it to change it")
    _write_entries(conn, b["id"], _resolve_entries(conn, entries))
    log(conn, "baseline", b["id"], "entries_set", count=len(entries))
    return baseline_detail(conn, b["id"])


def clone_baseline(conn, baseline_id, name):
    b = get_baseline(conn, baseline_id)
    entries = [{"ci": e["ci"], "version": e["version_id"]} for e in baseline_entries(conn, b["id"])]
    return create_baseline(conn, b["ifc_id"], name, entries)


def _approve(conn, b):
    conn.execute("UPDATE baseline SET status = 'superseded' WHERE ifc_id = ? AND status = 'approved' AND id != ?",
                 (b["ifc_id"], b["id"]))
    conn.execute("UPDATE baseline SET status = 'approved', approved_at = ? WHERE id = ?", (now(), b["id"]))
    log(conn, "baseline", b["id"], "approved")


def approve_baseline(conn, baseline_id):
    b = get_baseline(conn, baseline_id)
    if b["status"] != "draft":
        raise CMError(f"baseline {b['name']} is {b['status']}")
    entries = baseline_entries(conn, b["id"])
    problems = [] if entries else ["baseline has no entries"]
    problems += [f"{e['ci']} {e['version']} is {e['status']}, not released"
                 for e in entries if e["status"] not in RELEASED]
    if problems:
        raise CMError(f"cannot approve {b['name']}", problems)
    _approve(conn, b)
    return baseline_detail(conn, b["id"])


def diff_baselines(conn, a_id, b_id):
    a = {e["ci"]: e["version"] for e in baseline_entries(conn, get_baseline(conn, a_id)["id"])}
    b = {e["ci"]: e["version"] for e in baseline_entries(conn, get_baseline(conn, b_id)["id"])}
    return {
        "added": {k: b[k] for k in sorted(b.keys() - a.keys())},
        "removed": {k: a[k] for k in sorted(a.keys() - b.keys())},
        "changed": {k: {"from": a[k], "to": b[k]} for k in sorted(a.keys() & b.keys()) if a[k] != b[k]},
        "unchanged": sorted(k for k in a.keys() & b.keys() if a[k] == b[k]),
    }


def _external_version(conn, ci, name):
    """Record a version known only from an HSCM (placeholder CIs, or untracked versions)."""
    try:
        cur = conn.execute(
            "INSERT INTO release (ci_id, name, kind, status, source) VALUES (?, ?, 'external', 'released', 'scraped')",
            (ci["id"], f"ext:{name}"))
    except sqlite3.IntegrityError:
        raise Conflict(f"external release for {ci['name']} {name} already exists") from None
    rel = get_release(conn, cur.lastrowid)
    ver = _insert_version(conn, rel, name, status="external")
    conn.execute("UPDATE release SET released_version_id = ?, released_at = ? WHERE id = ?",
                 (ver["id"], now(), rel["id"]))
    return ver


def import_hscm(conn, ifc_ref, name, rows, source_ref=None, approve=True):
    """Record a scraped HSCM as a baseline.

    rows: [{ci, version, type?}]. Unknown CIs become placeholder CIs (managed=0);
    unknown versions become 'external' versions. The HSCM document is the authority,
    so it is approved as-is by default; anything odd comes back as warnings.
    """
    ifc = get_ifc(conn, ifc_ref)
    warnings, resolved, created_cis = [], {}, []
    for i, row in enumerate(rows, 1):
        ci_name = str(row.get("ci") or "").strip()
        ver_name = str(row.get("version") or "").strip()
        if not ci_name or not ver_name:
            warnings.append(f"row {i}: missing ci or version; skipped")
            continue
        ci = conn.execute("SELECT * FROM ci WHERE name = ?", (ci_name,)).fetchone()
        if ci is None:
            ci = create_ci(conn, ci_name, type=(row.get("type") or "CSCI").strip().upper(), managed=False,
                           description=f"placeholder from HSCM {name}")
            created_cis.append(ci_name)
        ver = conn.execute("SELECT * FROM version WHERE ci_id = ? AND name = ?", (ci["id"], ver_name)).fetchone()
        if ver is None:
            ver = _external_version(conn, ci, ver_name)
            if ci["managed"]:
                warnings.append(f"{ci_name} {ver_name}: not in tracker; recorded as external")
        elif ver["status"] not in RELEASED:
            warnings.append(f"{ci_name} {ver_name}: tracker says {ver['status']}, HSCM lists it")
        if ci["id"] in resolved:
            warnings.append(f"row {i}: {ci_name} listed more than once; last row wins")
        resolved[ci["id"]] = ver

    prev = current_baseline(conn, ifc["id"])
    try:
        cur = conn.execute(
            "INSERT INTO baseline (ifc_id, name, supersedes_id, source, source_ref) VALUES (?, ?, ?, 'scraped', ?)",
            (ifc["id"], name, prev["id"] if prev else None, source_ref))
    except sqlite3.IntegrityError:
        raise Conflict(f"baseline {name!r} already exists for {ifc['name']}") from None
    _write_entries(conn, cur.lastrowid, resolved)
    log(conn, "baseline", cur.lastrowid, "imported", ifc=ifc["name"], name=name,
        source_ref=source_ref, placeholders=created_cis, warnings=warnings)
    if approve:
        _approve(conn, get_baseline(conn, cur.lastrowid))
    return {"baseline": baseline_detail(conn, cur.lastrowid), "placeholders_created": created_cis,
            "warnings": warnings}


# ----------------------------------------------------------------------------- events

def list_events(conn, entity=None, entity_id=None, limit=200):
    sql, args = "SELECT * FROM event WHERE 1=1", []
    if entity:
        sql += " AND entity = ?"
        args.append(entity)
    if entity_id is not None:
        sql += " AND entity_id = ?"
        args.append(entity_id)
    return conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, int(limit))).fetchall()

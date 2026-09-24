"""Domain logic.

Every function takes an open sqlite3 connection and does NOT commit: the caller
owns the transaction (the API wraps each request in ``with conn:``; scripts
should do the same). References to CIs and IFCs accept an id or a name.
"""
import datetime as dt
import json
import sqlite3

from . import rank, releases as rsrc, tickets

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

_JSON_COLS = {"attributes", "params", "detail", "teams", "source_params", "last_sync", "pinned"}


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
    """ISO date string (YYYY-MM-DD) or None."""
    if value in (None, ""):
        return None
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()[:10]
    try:
        return dt.date.fromisoformat(str(value).strip()[:10]).isoformat()
    except ValueError:
        raise CMError(f"invalid {what} {value!r}; expected YYYY-MM-DD") from None


def _when(value, what):
    """A past-or-present UTC timestamp (a date means midnight UTC), for correcting when something happened."""
    raw = str(value or "").strip()
    try:
        d = dt.datetime.fromisoformat(raw.replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        raise CMError(f"invalid {what} {value!r}; expected YYYY-MM-DD or an ISO timestamp") from None
    d = d.replace(tzinfo=dt.timezone.utc) if d.tzinfo is None else d.astimezone(dt.timezone.utc)
    if d > dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5):
        raise CMError(f"{what} {raw} is in the future")
    return d.isoformat(timespec="seconds")


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


def _source_settings(release_source, source_params):
    if release_source is not None and not isinstance(release_source, str):
        raise CMError("release_source must be a source name, or null to manage releases by hand")
    if source_params is not None and not isinstance(source_params, dict):
        raise CMError("source_params must be a JSON object")
    return (release_source or "").strip() or None, json.dumps(source_params or {})


def create_ci(conn, name, type="CSCI", kind="simple", managed=True, release_source=None, source_params=None,
              require_tested=True, description=None, attributes=None):
    if not name:
        raise CMError("name is required")
    if type not in ("CSCI", "HWCI"):
        raise CMError("type must be CSCI or HWCI")
    if kind not in ("simple", "composite"):
        raise CMError("kind must be simple or composite")
    release_source, source_params = _source_settings(release_source, source_params)
    try:
        cur = conn.execute(
            "INSERT INTO ci (name, type, kind, managed, release_source, source_params, require_tested, description, "
            "attributes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, type, kind, 1 if managed else 0, release_source, source_params, 1 if require_tested else 0,
             description, json.dumps(attributes or {})))
    except sqlite3.IntegrityError:
        raise Conflict(f"CI {name!r} already exists") from None
    log(conn, "ci", cur.lastrowid, "created", name=name, type=type, kind=kind, managed=bool(managed),
        release_source=release_source)
    return get_ci(conn, cur.lastrowid)


def update_ci(conn, ref, **fields):
    """Update managed/kind/release source/gate/description/attributes (e.g. adopt a placeholder)."""
    ci = get_ci(conn, ref)
    allowed = {"managed", "kind", "release_source", "source_params", "require_tested", "description", "attributes"}
    unknown = set(fields) - allowed
    if unknown:
        raise CMError(f"cannot update {sorted(unknown)}")
    sets = {}
    for flag in ("managed", "require_tested"):
        if flag in fields:
            sets[flag] = 1 if fields[flag] else 0
    if "kind" in fields:
        if fields["kind"] not in ("simple", "composite"):
            raise CMError("kind must be simple or composite")
        sets["kind"] = fields["kind"]
    if "release_source" in fields:
        sets["release_source"] = _source_settings(fields["release_source"], None)[0]
    if "source_params" in fields:
        sets["source_params"] = _source_settings(None, fields["source_params"])[1]
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
    out["cscs"] = to_dicts(conn.execute("SELECT * FROM csc WHERE ci_id = ? ORDER BY name", (ci["id"],)))
    out["releases"] = list_releases(conn, ci["id"])
    out["attention"] = ci_attention(conn, ci["id"])
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


# ----------------------------------------------------------------------------- releases & versions
#
# A CI's releases and builds come from its release source (see "release sources" below) or are entered by
# hand. Either way cmtrack owns what happens to them: build status, build and release dates, the release
# gate, manifests, lineage.

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


def find_release(conn, ci, ref):
    """Release of ``ci`` by id or name."""
    ref = str(ref)
    col = "id" if ref.isdigit() else "name"
    return _one(conn, f"SELECT * FROM release WHERE ci_id = ? AND {col} = ?", (ci["id"], ref),
                f"release {ref!r} of {ci['name']}")


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
    out["unabsorbed"] = unabsorbed_fixes(conn, rel["id"])
    return out


def _next_seq(conn, release_id):
    return conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM version WHERE release_id = ?",
                        (release_id,)).fetchone()[0]


def _insert_version(conn, rel, name, planned_date=None, planned=False, status="planned", source_key=None):
    try:
        cur = conn.execute(
            "INSERT INTO version (release_id, ci_id, seq, name, status, planned, planned_date, source_key, "
            "source_state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rel["id"], rel["ci_id"], _next_seq(conn, rel["id"]), name, status, 1 if planned else 0, planned_date,
             source_key, "synced" if source_key else None))
    except sqlite3.IntegrityError:
        raise Conflict(f"version {name!r} already exists for this CI") from None
    log(conn, "version", cur.lastrowid, "created", release=rel["name"], name=name, planned=planned)
    return get_version(conn, cur.lastrowid)


def add_version(conn, release_id, name=None, planned_date=None):
    """Add a build by hand (e.g. a 4th build after a failed candidate). Counted as variance on a synced CI."""
    rel = get_release(conn, release_id)
    if rel["status"] in ("released", "cancelled"):
        raise CMError(f"release {rel['name']} is {rel['status']}")
    name = (name or "").strip() or \
        f"{rel['name']}-{'b' if rel['kind'] == 'planned' else 'r'}{_next_seq(conn, rel['id'])}"
    ver = _insert_version(conn, rel, name, _date(planned_date, "planned_date"))
    rebuild_lineage(conn, rel["ci_id"])
    return ver


def _base_version(conn, ci, root, ref=None):
    """The version a patch/emergency on ``root``'s line builds on: ``ref`` (checked), else the line's effective
    version (None while nothing on the line is released)."""
    if ref in (None, ""):
        return effective_version(conn, root["id"])
    base = find_version(conn, ci, ref)
    if root_release(conn, base["release_id"])["id"] != root["id"]:
        raise CMError(f"base version {base['name']} is not part of release {root['name']}")
    if base["status"] not in RELEASED:
        raise CMError(f"base version {base['name']} is {base['status']}, not released")
    return base


def create_release(conn, ci_ref, name=None, kind="planned", target_date=None, parent=None, base_version=None,
                   reason=None, builds=None):
    """Add a release by hand (a CI without a release source, or something the source doesn't track).

    ``builds``: names (or {name, planned_date}) of its builds, in order. A patch/emergency needs the planned
    release it patches (``parent``), gets a build named after itself unless ``builds`` says otherwise, and
    builds on ``base_version`` (default: the line's effective version). Emergencies need a ``reason``.
    """
    ci = get_ci(conn, ci_ref)
    if kind not in rsrc.KINDS:
        raise CMError(f"kind must be one of {', '.join(rsrc.KINDS)}")
    name, reason = (name or "").strip() or None, (reason or "").strip() or None
    root = base = None
    if kind == "planned":
        if parent or base_version:
            raise CMError("only patch and emergency releases have a parent and a base version")
        if not name:
            raise CMError("name is required")
    else:
        if not parent:
            raise CMError(f"a {kind} release needs the planned release it patches ('parent')")
        root = root_release(conn, find_release(conn, ci, parent)["id"])
        if root["kind"] != "planned":
            raise CMError(f"{root['name']} is {root['kind']}; patches go on planned releases")
        if kind == "emergency" and not reason:
            raise CMError("emergency releases need a reason / change request")
        base = _base_version(conn, ci, root, base_version)
        if not name:
            n = conn.execute("SELECT COUNT(*) FROM release WHERE parent_id = ? AND kind = ?",
                             (root["id"], kind)).fetchone()[0] + 1
            name = f"{root['name']}.{'P' if kind == 'patch' else 'ER'}{n}"
    try:
        cur = conn.execute(
            "INSERT INTO release (ci_id, name, kind, parent_id, base_version_id, target_date, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ci["id"], name, kind, root and root["id"], base and base["id"], _date(target_date, "target_date"),
             reason))
    except sqlite3.IntegrityError:
        raise Conflict(f"release {name!r} already exists for {ci['name']}") from None
    rel = get_release(conn, cur.lastrowid)
    log(conn, "release", rel["id"], "created", name=name, kind=kind, parent=root and root["name"],
        base=base and base["name"], reason=reason)
    if builds is None:
        builds = [] if kind == "planned" else [name]
    for b in builds:
        b = b if isinstance(b, dict) else {"name": b}
        if not str(b.get("name") or "").strip():
            raise CMError("every build needs a name")
        _insert_version(conn, rel, str(b["name"]).strip(), _date(b.get("planned_date"), "planned_date"))
    rebuild_lineage(conn, ci["id"])
    return release_detail(conn, rel["id"])


# Fields a sync owns. Changing one by hand on a synced row pins it, so later syncs leave it alone.
SYNCED_RELEASE_FIELDS = ("name", "kind", "target_date", "reason", "parent_id")
SYNCED_VERSION_FIELDS = ("name", "planned_date", "release_id")
PIN_ALIASES = {"parent": "parent_id", "base_version": "base_version_id", "release": "release_id"}


def _label(conn, field, value):
    """A field value as people read it (names instead of row ids)."""
    if value is None:
        return None
    if field in ("parent_id", "release_id"):
        return get_release(conn, value)["name"]
    if field in ("base_version_id", "released_version_id"):
        return get_version(conn, value)["name"]
    return value


def _pins(row, changed, pinnable, unpin):
    pins = set(json.loads(row["pinned"] or "[]"))
    if row["source_key"]:
        pins |= set(changed) & set(pinnable)
    wanted = {PIN_ALIASES.get(f, f) for f in (unpin or ())}
    bad = wanted - set(pinnable)
    if bad:
        raise CMError(f"can't unpin {sorted(bad)}; pinnable fields: {list(pinnable)}")
    return pins - wanted


def _note(note, what):
    note = (note or "").strip()
    if not note:
        raise CMError(f"correcting {what} needs a note saying why")
    return note


def update_release(conn, release_id, note=None, unpin=None, **fields):
    """Correct a release by hand: name, target_date, reason, parent, base_version, released_at.

    On a synced release the source-owned fields changed here are pinned (later syncs keep your value and
    report the difference); ``unpin`` hands fields back to the source. ``released_at`` corrects when it was
    released and needs a ``note``.
    """
    rel = get_release(conn, release_id)
    ci = get_ci(conn, rel["ci_id"])
    unknown = set(fields) - {"name", "target_date", "reason", "parent", "base_version", "released_at"}
    if unknown:
        raise CMError(f"cannot update {sorted(unknown)}")
    sets = {}
    if "name" in fields:
        sets["name"] = str(fields["name"] or "").strip()
        if not sets["name"]:
            raise CMError("name can't be empty")
    if "target_date" in fields:
        sets["target_date"] = _date(fields["target_date"], "target_date")
    if "reason" in fields:
        sets["reason"] = str(fields["reason"] or "").strip() or None
    if ("parent" in fields or "base_version" in fields) and rel["kind"] == "planned":
        raise CMError("only patch and emergency releases have a parent and a base version")
    root = get_release(conn, rel["parent_id"]) if rel["parent_id"] else None
    if "parent" in fields:
        root = root_release(conn, find_release(conn, ci, fields["parent"])["id"])
        if root["kind"] != "planned" or root["id"] == rel["id"]:
            raise CMError(f"{root['name']} can't be the parent of {rel['name']}")
        sets["parent_id"] = root["id"]
    if "base_version" in fields:
        base = _base_version(conn, ci, root, fields["base_version"]) if fields["base_version"] else None
        if base and base["release_id"] == rel["id"]:
            raise CMError(f"{rel['name']} can't build on its own version")
        sets["base_version_id"] = base and base["id"]
    if "released_at" in fields:
        if rel["status"] != "released":
            raise CMError(f"{rel['name']} hasn't been released")
        note = _note(note, "the release date")
        sets["released_at"] = _when(fields["released_at"], "released_at")
        built = get_version(conn, rel["released_version_id"])["built_at"] if rel["released_version_id"] else None
        if built and sets["released_at"] < built:
            raise CMError(f"released_at {sets['released_at']} is before the release build was built ({built})")

    changes = {k: v for k, v in sets.items() if rel[k] != v}
    pins = _pins(rel, changes, SYNCED_RELEASE_FIELDS + ("base_version_id",), unpin)
    if changes or pins != set(json.loads(rel["pinned"])):
        cols = {**changes, "pinned": json.dumps(sorted(pins))}
        try:
            conn.execute(f"UPDATE release SET {', '.join(k + ' = ?' for k in cols)} WHERE id = ?",
                         (*cols.values(), rel["id"]))
        except sqlite3.IntegrityError:
            raise Conflict(f"release {sets.get('name')!r} already exists for {ci['name']}") from None
        log(conn, "release", rel["id"], "corrected" if "released_at" in changes else "edited",
            **{k: [_label(conn, k, rel[k]), _label(conn, k, v)] for k, v in changes.items()},
            **({"note": note} if note else {}), **({"unpinned": list(unpin)} if unpin else {}))
        if {"target_date", "parent_id", "base_version_id"} & set(changes):
            rebuild_lineage(conn, ci["id"])
    return release_detail(conn, rel["id"])


def update_version(conn, version_id, status=None, artifact_ref=None, built_at=None, planned_date=None, name=None,
                   note=None, unpin=None):
    """Move a version through its states, or correct it.

    ``built_at`` with ``status='built'`` records when it was built (default now); on its own it corrects the
    build date of an already-built version and needs a ``note``. ``name`` / ``planned_date`` on a synced
    version pin those fields (``unpin`` hands them back to the source).
    """
    ver = get_version(conn, version_id)
    sets = {}
    if status and status != ver["status"]:
        if status not in VERSION_TRANSITIONS.get(ver["status"], ()):
            raise CMError(f"cannot move version {ver['name']} from {ver['status']} to {status}")
        sets["status"] = status
        if status == "built":
            sets["built_at"] = _when(built_at, "built_at") if built_at else now()
    elif built_at not in (None, ""):
        if not ver["built_at"]:
            raise CMError(f"{ver['name']} hasn't been built; set status 'built' with built_at instead")
        note = _note(note, "the build date")
        sets["built_at"] = _when(built_at, "built_at")
        rel = get_release(conn, ver["release_id"])
        if rel["released_version_id"] == ver["id"] and rel["released_at"] and sets["built_at"] > rel["released_at"]:
            raise CMError(f"built_at {sets['built_at']} is after {rel['name']} was released ({rel['released_at']})")
    if artifact_ref is not None:
        sets["artifact_ref"] = artifact_ref
    if planned_date is not None:
        sets["planned_date"] = _date(planned_date, "planned_date")
    if name is not None:
        sets["name"] = str(name).strip()
        if not sets["name"]:
            raise CMError("name can't be empty")

    changes = {k: v for k, v in sets.items() if ver[k] != v}
    pins = _pins(ver, changes, SYNCED_VERSION_FIELDS, unpin)
    if changes or pins != set(json.loads(ver["pinned"])):
        cols = {**changes, "pinned": json.dumps(sorted(pins))}
        try:
            conn.execute(f"UPDATE version SET {', '.join(k + ' = ?' for k in cols)} WHERE id = ?",
                         (*cols.values(), ver["id"]))
        except sqlite3.IntegrityError:
            raise Conflict(f"version {sets.get('name')!r} already exists for this CI") from None
        if changes.get("status") == "built":
            conn.execute("UPDATE release SET status = 'active' WHERE id = ? AND status = 'planned'",
                         (ver["release_id"],))
        corrected = "built_at" in changes and "status" not in changes
        log(conn, "version", ver["id"], "corrected" if corrected else "updated",
            **({k: [ver[k], v] for k, v in changes.items()} if corrected else changes),
            **({"note": note} if note else {}), **({"unpinned": list(unpin)} if unpin else {}))
    return get_version(conn, ver["id"])


def release_version(conn, version_id, released_at=None):
    """Promote a version to be its release. Enforces the CI's gate and the composite rules.
    ``released_at`` backdates it (default now)."""
    ver = get_version(conn, version_id)
    rel = get_release(conn, ver["release_id"])
    ci = get_ci(conn, ver["ci_id"])
    if rel["status"] in ("released", "cancelled"):
        raise CMError(f"release {rel['name']} is already {rel['status']}")

    allowed = ("tested",) if ci["require_tested"] else ("built", "tested")
    problems = [] if ver["status"] in allowed else \
        [f"version {ver['name']} is {ver['status']}; must be {' or '.join(allowed)}"]
    when = _when(released_at, "released_at") if released_at else now()
    if ver["built_at"] and when < ver["built_at"]:
        problems.append(f"released_at {when} is before it was built ({ver['built_at']})")
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
                 (ver["id"], when, rel["id"]))
    log(conn, "release", rel["id"], "released", version=ver["name"], **({"at": when} if released_at else {}))
    rebuild_lineage(conn, ci["id"])
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
    rebuild_lineage(conn, rel["ci_id"])
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


# ----------------------------------------------------------------------------- lineage (version DAG)

def release_head(conn, rel):
    """The version a successor release builds on: the released one, else the latest non-rejected build."""
    if rel["released_version_id"]:
        return get_version(conn, rel["released_version_id"])
    return conn.execute("SELECT * FROM version WHERE release_id = ? ORDER BY status = 'rejected', seq DESC LIMIT 1",
                        (rel["id"],)).fetchone()


def rebuild_lineage(conn, ci_id):
    """Recompute the automatic parent edges of a CI's versions. Versions with manual lineage are untouched.

    - a build's parent is the previous build of its release;
    - the first build of a patch/emergency builds on its base version;
    - the first build of a planned release builds on the previous planned release's head
      (cancelled releases are skipped, and so are ones gone from the release source that never got a real
      build). External (scraped) versions have no lineage.
    """
    auto = {r[0] for r in conn.execute("SELECT id FROM version WHERE ci_id = ? AND lineage = 'auto'", (ci_id,))}
    by_release = {}
    for v in conn.execute("SELECT id, release_id FROM version WHERE ci_id = ? ORDER BY release_id, seq", (ci_id,)):
        by_release.setdefault(v["release_id"], []).append(v["id"])
    edges = []

    def chain(rel, first_parent):
        prev = first_parent
        for vid in by_release.get(rel["id"], []):
            if prev is not None and vid in auto:
                edges.append((vid, prev))
            prev = vid

    prev_head = None
    for rel in conn.execute("SELECT * FROM release WHERE ci_id = ? AND kind = 'planned' "
                            "ORDER BY target_date IS NULL, target_date, id", (ci_id,)).fetchall():
        chain(rel, prev_head)
        phantom = rel["source_state"] == "missing" and not conn.execute(
            "SELECT 1 FROM version WHERE release_id = ? AND status NOT IN ('planned', 'rejected')", (rel["id"],)).fetchone()
        if rel["status"] != "cancelled" and not phantom:
            head = release_head(conn, rel)
            prev_head = head["id"] if head else prev_head
    for rel in conn.execute("SELECT * FROM release WHERE ci_id = ? AND parent_id IS NOT NULL", (ci_id,)).fetchall():
        chain(rel, rel["base_version_id"])

    conn.execute("DELETE FROM version_parent WHERE version_id IN "
                 "(SELECT id FROM version WHERE ci_id = ? AND lineage = 'auto')", (ci_id,))
    conn.executemany("INSERT OR IGNORE INTO version_parent (version_id, parent_id) VALUES (?, ?)", edges)


def backfill_lineage(conn):
    """Build lineage for databases created before the version DAG existed."""
    if conn.execute("SELECT 1 FROM version_parent LIMIT 1").fetchone() is None:
        for (ci_id,) in conn.execute("SELECT DISTINCT ci_id FROM version").fetchall():
            rebuild_lineage(conn, ci_id)


def _closure(conn, version_id, direction):
    """Ids of ``version_id`` and all its ancestors (direction='up') or descendants ('down')."""
    near, far = ("version_id", "parent_id") if direction == "up" else ("parent_id", "version_id")
    rows = conn.execute(
        f"WITH RECURSIVE c(id) AS (SELECT ? UNION SELECT p.{far} FROM version_parent p JOIN c ON p.{near} = c.id) "
        "SELECT id FROM c", (version_id,))
    return {r[0] for r in rows}


def ancestor_ids(conn, version_id):
    return _closure(conn, version_id, "up")


def descendant_ids(conn, version_id):
    return _closure(conn, version_id, "down")


def version_parents(conn, version_id, direction="up"):
    near, far = ("version_id", "parent_id") if direction == "up" else ("parent_id", "version_id")
    return to_dicts(conn.execute(
        f"SELECT v.id, v.name, v.status, r.id AS release_id, r.name AS release, r.kind AS release_kind "
        f"FROM version_parent p JOIN version v ON v.id = p.{far} JOIN release r ON r.id = v.release_id "
        f"WHERE p.{near} = ? ORDER BY v.id", (version_id,)))


def lineage(conn, version_id):
    ver = get_version(conn, version_id)
    return {"version": ver["name"], "lineage": ver["lineage"],
            "parents": version_parents(conn, ver["id"], "up"),
            "children": version_parents(conn, ver["id"], "down")}


def set_version_parents(conn, version_id, parents):
    """Set a version's parents by hand (e.g. a merge: [2026.Q4-b4, 2026.Q4.ER1]); ``None`` reverts to automatic."""
    ver = get_version(conn, version_id)
    ci = get_ci(conn, ver["ci_id"])
    if parents is None:
        conn.execute("UPDATE version SET lineage = 'auto' WHERE id = ?", (ver["id"],))
        rebuild_lineage(conn, ci["id"])
        log(conn, "version", ver["id"], "lineage_reset")
        return lineage(conn, ver["id"])
    if not isinstance(parents, list):
        raise CMError("parents must be a list of version names or ids")
    resolved = {p["id"]: p for p in (find_version(conn, ci, ref) for ref in parents)}
    below = descendant_ids(conn, ver["id"])
    loops = [p["name"] for p in resolved.values() if p["id"] in below]
    if loops:
        raise CMError(f"{', '.join(loops)} would make {ver['name']} its own ancestor")
    conn.execute("DELETE FROM version_parent WHERE version_id = ?", (ver["id"],))
    conn.executemany("INSERT INTO version_parent (version_id, parent_id) VALUES (?, ?)",
                     [(ver["id"], pid) for pid in resolved])
    conn.execute("UPDATE version SET lineage = 'manual' WHERE id = ?", (ver["id"],))
    log(conn, "version", ver["id"], "lineage_set", parents=[p["name"] for p in resolved.values()])
    return lineage(conn, ver["id"])


def version_range(conn, ci_ref, to, frm=None):
    """Versions in ``frm..to``: ``to`` and its ancestors, minus ``frm`` and its ancestors (git semantics).

    Without ``frm``, everything ``to`` was built from. Ordered oldest first.
    """
    ci = get_ci(conn, ci_ref)
    head = find_version(conn, ci, to)
    ids = ancestor_ids(conn, head["id"])
    if frm not in (None, ""):
        ids -= ancestor_ids(conn, find_version(conn, ci, frm)["id"])
    ids_json = json.dumps(sorted(ids))
    rows = {r["id"]: r for r in conn.execute(
        "SELECT v.*, r.name AS release FROM version v JOIN release r ON r.id = v.release_id "
        "WHERE v.id IN (SELECT value FROM json_each(?))", (ids_json,))}
    edges = conn.execute("SELECT version_id, parent_id FROM version_parent WHERE version_id IN "
                         "(SELECT value FROM json_each(?))", (ids_json,)).fetchall()
    return [rows[i] for i in _topo_order(rows, edges)]


def _topo_order(ids, edges):
    """Parents before children; ties broken by id (creation order)."""
    waiting = {i: 0 for i in ids}
    children = {}
    for child, parent in edges:
        if parent in waiting and child in waiting:
            waiting[child] += 1
            children.setdefault(parent, []).append(child)
    ready = sorted(i for i, n in waiting.items() if n == 0)
    order = []
    while ready:
        i = ready.pop(0)
        order.append(i)
        for c in children.get(i, []):
            waiting[c] -= 1
            if waiting[c] == 0:
                ready.append(c)
        ready.sort()
    return order + sorted(set(ids) - set(order))   # anything left is on a cycle


def ci_versions(conn, ci_ref):
    """All of a CI's (non-external) versions, parents before children."""
    ci = get_ci(conn, ci_ref)
    rows = {r["id"]: r for r in conn.execute(
        "SELECT v.*, r.name AS release FROM version v JOIN release r ON r.id = v.release_id "
        "WHERE v.ci_id = ? AND r.kind != 'external'", (ci["id"],))}
    edges = conn.execute("SELECT p.version_id, p.parent_id FROM version_parent p JOIN version v ON v.id = p.version_id "
                         "WHERE v.ci_id = ?", (ci["id"],)).fetchall()
    return [rows[i] for i in _topo_order(rows, edges)]


def release_range(conn, release_id):
    """{from, to} for "what's new in this release": its head, minus what its first build was built from
    outside the release (the previous release's head, or a patch's base version)."""
    rel = get_release(conn, release_id)
    head = release_head(conn, rel)
    if head is None:
        return None
    first = conn.execute("SELECT id FROM version WHERE release_id = ? ORDER BY seq LIMIT 1", (rel["id"],)).fetchone()
    outside = [p for p in version_parents(conn, first["id"]) if p["release_id"] != rel["id"]]
    return {"from": outside[0]["name"] if outside else None, "to": head["name"]}


def unabsorbed_fixes(conn, release_id):
    """Released patch/emergency fixes of earlier release lines that this planned release does not build on."""
    rel = get_release(conn, release_id)
    if rel["kind"] != "planned" or not rel["target_date"]:
        return []
    head = release_head(conn, rel)
    if head is None:
        return []
    have = ancestor_ids(conn, head["id"])
    rows = conn.execute(
        "SELECT v.id, v.name, r.id AS release_id, r.kind, r.reason, root.name AS root FROM release r "
        "JOIN version v ON v.id = r.released_version_id JOIN release root ON root.id = r.parent_id "
        "WHERE r.ci_id = ? AND r.status = 'released' AND root.target_date < ? ORDER BY r.released_at",
        (rel["ci_id"], rel["target_date"]))
    return [dict(r) for r in rows if r["id"] not in have]


# ----------------------------------------------------------------------------- release sources (sync)
#
# A sync asks the CI's release source for everything and reconciles by the source's key: new items are
# created, known ones updated (except pinned fields), a same-named row entered by hand is adopted (or re-keyed
# when its old key is gone from the source entirely), and rows the source no longer lists become 'missing'. Nothing is deleted or guessed; people fix what doesn't line up
# (remap, detach, cancel, pin) from the CI's "Needs attention" list.

class SourceError(CMError):
    """The ticket or release source failed (it's outside our control): HTTP 502 / an alert in the page."""
    status = 502


def _issue(key, name, why):
    return {"key": None if key is None else str(key), "name": name, "why": why}


def _source_records(source, ci, params):
    """(release records, issues) from the source, with bad records turned into issues."""
    try:
        items = list(source.releases(ci, params) or [])
    except rsrc.SourceConfigError as e:
        raise CMError(f"release source {source.name!r}: {e}") from None
    except CMError:
        raise
    except NotImplementedError:
        raise CMError(f"release source {source.name!r} doesn't implement releases()") from None
    except Exception as e:
        raise SourceError(f"release source {source.name!r} failed: {e}") from e

    records, issues = [], []
    for it in items:
        if isinstance(it, rsrc.Unplaced):
            issues.append(_issue(it.key, it.name, it.why))
            continue
        try:
            rec = it if isinstance(it, rsrc.ReleaseRecord) else rsrc.ReleaseRecord.from_dict(it)
            rec.key, rec.name = str(rec.key or "").strip(), str(rec.name or "").strip()
            rec.target_date = _date(rec.target_date, f"date of {rec.name}")
            for b in rec.builds:
                b.key, b.name = str(b.key or "").strip(), str(b.name or "").strip()
                b.planned_date = _date(b.planned_date, f"date of {b.name}")
        except (TypeError, ValueError, CMError) as e:
            issues.append(_issue(None, str(getattr(it, "name", it))[:80], f"unreadable record: {e}"))
            continue
        records.append(rec)

    planned_keys = {r.key for r in records if r.kind == "planned"}
    good, keys, names, bkeys, bnames = [], set(), set(), set(), set()
    for rec in records:
        why = ("missing key or name" if not (rec.key and rec.name) else
               f"unknown kind {rec.kind!r}" if rec.kind not in rsrc.KINDS else
               f"duplicate key {rec.key}" if rec.key in keys else
               "duplicate name" if rec.name in names else
               f"{rec.kind} whose planned release ({rec.parent_key}) the source didn't list"
               if rec.kind != "planned" and rec.parent_key not in planned_keys else None)
        if why:
            issues.append(_issue(rec.key, rec.name, why))
            continue
        keys.add(rec.key)
        names.add(rec.name)
        builds = []
        for b in rec.builds:
            if not (b.key and b.name) or b.key in bkeys or b.name in bnames:
                issues.append(_issue(b.key, b.name, f"build of {rec.name} with a missing or duplicate key or name"))
                continue
            bkeys.add(b.key)
            bnames.add(b.name)
            builds.append(b)
        rec.builds = builds
        good.append(rec)
    return good, issues


def _adopt_or_find(conn, table, ci_id, key, name, listed, summary):
    """The row with this source key. Failing that, the row with the same name if it was entered by hand
    (adopted) or its own key is no longer listed at all (re-keyed: deleted and re-created in the source)."""
    row = conn.execute(f"SELECT * FROM {table} WHERE ci_id = ? AND source_key = ?", (ci_id, key)).fetchone()
    if row is not None:
        return row
    not_external = ("r.kind != 'external'" if table == "release" else
                    "release_id NOT IN (SELECT id FROM release WHERE kind = 'external')")
    row = conn.execute(f"SELECT * FROM {table} r WHERE ci_id = ? AND name = ? AND {not_external}",
                       (ci_id, name)).fetchone()
    if row is None or (row["source_key"] is not None and row["source_key"] in listed):
        return None
    extra = ", source = 'sync'" if table == "release" else ", planned = 1"
    conn.execute(f"UPDATE {table} SET source_key = ?, source_state = 'synced'{extra} WHERE id = ?", (key, row["id"]))
    if row["source_key"] is None:
        log(conn, table, row["id"], "adopted", source_key=key)
        summary["adopted"].append(name)
    else:
        log(conn, table, row["id"], "rekeyed", was=row["source_key"], source_key=key)
        summary["rekeyed"].append(name)
    return conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row["id"],)).fetchone()


def _apply(conn, table, row, values, summary):
    """Write the source's values to an existing row, except pinned fields (reported instead)."""
    pins = set(json.loads(row["pinned"] or "[]"))
    changes = {}
    for f, new in values.items():
        if row[f] == new:
            continue
        if f in pins:
            summary["pinned"].append({"name": row["name"], "field": f, "source": _label(conn, f, new),
                                      "kept": _label(conn, f, row[f])})
            continue
        changes[f] = new
    readable = {f: [_label(conn, f, row[f]), _label(conn, f, v)] for f, v in changes.items()}
    if "release_id" in changes:
        changes["seq"] = _next_seq(conn, changes["release_id"])
    restored = row["source_state"] != "synced"
    if restored:
        changes["source_state"] = "synced"
    if changes:
        conn.execute(f"UPDATE {table} SET {', '.join(k + ' = ?' for k in changes)} WHERE id = ?",
                     (*changes.values(), row["id"]))
    name = changes.get("name", row["name"])
    if readable:
        log(conn, table, row["id"], "synced", **readable)
        summary["updated"].append({"name": name, "changes": readable})
    if restored:
        log(conn, table, row["id"], "restored")
        summary["restored"].append(name)
    return conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row["id"],)).fetchone()


def sync_ci(conn, source, ci_ref, dry_run=False):
    """Reconcile the CI's releases and builds with its release source. Idempotent; returns what happened.
    ``dry_run`` works it all out and rolls it back."""
    ci = get_ci(conn, ci_ref)
    if not ci["release_source"]:
        raise CMError(f"{ci['name']} has no release source; its releases are managed by hand")
    if source is None:
        raise CMError(f"release source {ci['release_source']!r} is not configured (set CMTRACK_RELEASE_SOURCES)")
    records, issues = _source_records(source, to_dict(ci), json.loads(ci["source_params"] or "{}"))
    summary = {"ci": ci["name"], "source": ci["release_source"], "at": now(), "dry_run": bool(dry_run),
               "created": [], "updated": [], "adopted": [], "rekeyed": [], "restored": [], "missing": [], "pinned": [],
               "bases": [], "issues": issues}
    conn.execute("SAVEPOINT sync")
    try:
        _sync(conn, ci, records, summary)
        rebuild_lineage(conn, ci["id"])
        counts = {k: len(summary[k]) for k in ("created", "updated", "adopted", "rekeyed", "restored", "missing",
                                               "issues")}
        summary["counts"] = counts
        conn.execute("UPDATE ci SET last_sync = ? WHERE id = ?",
                     (json.dumps({k: v for k, v in summary.items() if k not in ("ci", "dry_run")}), ci["id"]))
        log(conn, "ci", ci["id"], "synced", source=ci["release_source"], **counts)
    except BaseException:
        conn.execute("ROLLBACK TO sync")
        conn.execute("RELEASE sync")
        raise
    if dry_run:
        conn.execute("ROLLBACK TO sync")
    conn.execute("RELEASE sync")
    return summary


def _sync(conn, ci, records, summary):
    seen = {"release": set(), "version": set()}
    rel_ids = {}
    listed = {"release": {r.key for r in records}, "version": {b.key for r in records for b in r.builds}}
    for rec in sorted(records, key=lambda r: r.kind != "planned"):          # planned first: children need them
        parent_id = None
        if rec.kind != "planned":
            parent_id = rel_ids.get(rec.parent_key)
            if parent_id is None:
                summary["issues"].append(_issue(rec.key, rec.name, "its planned release couldn't be synced"))
                continue
        values = {"name": rec.name, "kind": rec.kind, "target_date": rec.target_date,
                  "reason": (rec.reason or "").strip() or None, "parent_id": parent_id}
        try:
            rel = _adopt_or_find(conn, "release", ci["id"], rec.key, rec.name, listed["release"], summary)
            if rel is None:
                cur = conn.execute(
                    "INSERT INTO release (ci_id, name, kind, target_date, reason, parent_id, source, source_key, "
                    "source_state) VALUES (?, ?, ?, ?, ?, ?, 'sync', ?, 'synced')",
                    (ci["id"], *values.values(), rec.key))
                rel = get_release(conn, cur.lastrowid)
                log(conn, "release", rel["id"], "created", name=rec.name, kind=rec.kind, source_key=rec.key)
                summary["created"].append(rec.name)
            else:
                rel = _apply(conn, "release", rel, values, summary)
        except sqlite3.IntegrityError:
            summary["issues"].append(_issue(rec.key, rec.name, f"another release of {ci['name']} already has this name"))
            continue
        rel_ids[rec.key] = rel["id"]
        seen["release"].add(rel["id"])
        if rec.released and rel["status"] != "released":
            summary["issues"].append(_issue(rec.key, rec.name, "the source says it's released; cmtrack hasn't released it"))

        for b in rec.builds:
            try:
                ver = _adopt_or_find(conn, "version", ci["id"], b.key, b.name, listed["version"], summary)
                if ver is None:
                    ver = _insert_version(conn, rel, b.name, b.planned_date, planned=True, source_key=b.key)
                    summary["created"].append(b.name)
                else:
                    values = {"name": b.name, "planned_date": b.planned_date, "release_id": rel["id"]}
                    if ver["release_id"] != rel["id"] and ver["status"] != "planned":
                        summary["issues"].append(_issue(b.key, b.name, f"the source moved it to {rel['name']}, but it's "
                                                        f"already {ver['status']} in {_label(conn, 'release_id', ver['release_id'])}"))
                        del values["release_id"]
                    ver = _apply(conn, "version", ver, values, summary)
            except (sqlite3.IntegrityError, Conflict):
                summary["issues"].append(_issue(b.key, b.name, f"another version of {ci['name']} already has this name"))
                continue
            seen["version"].add(ver["id"])

    for table in ("release", "version"):
        for row in conn.execute(f"SELECT id, name FROM {table} WHERE ci_id = ? AND source_state = 'synced'",
                                (ci["id"],)).fetchall():
            if row["id"] not in seen[table]:
                conn.execute(f"UPDATE {table} SET source_state = 'missing' WHERE id = ?", (row["id"],))
                log(conn, table, row["id"], "missing_from_source")
                summary["missing"].append(row["name"])

    # a patch/emergency builds on its line's effective version, filled in once something there is released
    for rel in conn.execute("SELECT * FROM release WHERE ci_id = ? AND parent_id IS NOT NULL AND base_version_id IS NULL "
                            "AND status != 'cancelled'", (ci["id"],)).fetchall():
        if "base_version_id" in json.loads(rel["pinned"]):
            continue
        eff = effective_version(conn, rel["parent_id"])
        if eff and eff["release_id"] != rel["id"]:
            conn.execute("UPDATE release SET base_version_id = ? WHERE id = ?", (eff["id"], rel["id"]))
            log(conn, "release", rel["id"], "based", base=eff["name"])
            summary["bases"].append({"name": rel["name"], "base": eff["name"]})


def _in_use(conn, table, row_id):
    """Why a row can't be folded away by a remap (empty when it's a fresh, untouched sync result)."""
    vids = [row_id] if table == "version" else         [r[0] for r in conn.execute("SELECT id FROM version WHERE release_id = ?", (row_id,))]
    ids = ",".join("?" * len(vids))
    hit = lambda sql, n=1: bool(vids) and conn.execute(sql.format(ids=ids), vids * n).fetchone() is not None
    why = []
    if hit("SELECT 1 FROM version WHERE id IN ({ids}) AND status != 'planned'"):
        why.append("has builds that were already built or rejected")
    if table == "release" and conn.execute("SELECT 1 FROM release WHERE parent_id = ?", (row_id,)).fetchone():
        why.append("has patch or emergency releases")
    if hit("SELECT 1 FROM baseline_entry WHERE version_id IN ({ids})"):
        why.append("is in a baseline")
    if hit("SELECT 1 FROM manifest_entry WHERE parent_version_id IN ({ids}) OR child_version_id IN ({ids})", 2):
        why.append("is in a composite manifest")
    if hit("SELECT 1 FROM release WHERE base_version_id IN ({ids})"):
        why.append("is the base of a patch or emergency")
    if hit("SELECT 1 FROM version WHERE id IN ({ids}) AND lineage = 'manual'") or hit(
            "SELECT 1 FROM version_parent p JOIN version v ON v.id = p.version_id "
            "WHERE p.parent_id IN ({ids}) AND v.lineage = 'manual'"):
        why.append("has hand-set lineage")
    return why


def _drop_unused(conn, table, row):
    vids = [row["id"]] if table == "version" else \
        [r[0] for r in conn.execute("SELECT id FROM version WHERE release_id = ?", (row["id"],))]
    marks = ",".join("?" * len(vids))
    if vids:
        conn.execute(f"DELETE FROM version_parent WHERE version_id IN ({marks}) OR parent_id IN ({marks})", vids + vids)
        conn.execute(f"DELETE FROM version WHERE id IN ({marks})", vids)
    if table == "release":
        conn.execute("DELETE FROM release WHERE id = ?", (row["id"],))


def _take_over(conn, table, old, new, fields, extra=None):
    """``old`` takes ``new``'s source identity and (unpinned) source fields."""
    pins = set(json.loads(old["pinned"] or "[]"))
    sets = {f: new[f] for f in fields if f not in pins}
    sets.update(source_key=new["source_key"], source_state="synced", **(extra or {}))
    if table == "version" and sets.get("release_id", old["release_id"]) != old["release_id"]:
        sets["seq"] = _next_seq(conn, sets["release_id"])
    conn.execute(f"UPDATE {table} SET {', '.join(k + ' = ?' for k in sets)} WHERE id = ?", (*sets.values(), old["id"]))


def remap_release(conn, release_id, to):
    """``release_id`` is what the source now calls ``to`` (e.g. after a rename in Jira): it takes over ``to``'s
    source key, name and dates, and its builds by position; ``to``, a fresh release the sync just created, is
    deleted. Everything attached to ``release_id`` (builds, patches, baselines, lineage) stays attached."""
    old = get_release(conn, release_id)
    ci = get_ci(conn, old["ci_id"])
    new = find_release(conn, ci, to)
    if new["id"] == old["id"]:
        raise CMError("a release can't be remapped to itself")
    if not new["source_key"]:
        raise CMError(f"{new['name']} isn't from the release source; there's nothing to remap to")
    busy = _in_use(conn, "release", new["id"])
    if busy:
        raise Conflict(f"{new['name']} is already in use, so {old['name']} can't take it over", busy)
    new_versions = conn.execute("SELECT * FROM version WHERE release_id = ? ORDER BY seq", (new["id"],)).fetchall()
    old_versions = conn.execute("SELECT * FROM version WHERE release_id = ? ORDER BY seq", (old["id"],)).fetchall()
    _drop_unused(conn, "release", new)
    try:
        parent = {"parent_id": new["parent_id"]} if new["parent_id"] != old["id"] else {}
        _take_over(conn, "release", old, new, ("name", "kind", "target_date", "reason"), {"source": "sync", **parent})
        for i, nv in enumerate(new_versions):
            if i < len(old_versions):
                _take_over(conn, "version", old_versions[i], nv, ("name", "planned_date"), {"planned": 1})
            else:
                _insert_version(conn, get_release(conn, old["id"]), nv["name"], nv["planned_date"], planned=True,
                                source_key=nv["source_key"])
        for ov in old_versions[len(new_versions):]:
            if ov["source_key"]:
                conn.execute("UPDATE version SET source_state = 'missing' WHERE id = ?", (ov["id"],))
    except sqlite3.IntegrityError as e:
        raise Conflict(f"can't remap {old['name']} to {new['name']}: {e}") from None
    log(conn, "release", old["id"], "remapped", was=old["name"], to=new["name"], source_key=new["source_key"])
    rebuild_lineage(conn, ci["id"])
    return release_detail(conn, old["id"])


def remap_version(conn, version_id, to):
    """Like remap_release, for one build: ``version_id`` takes over ``to`` (a fresh synced build), which is deleted."""
    old = get_version(conn, version_id)
    ci = get_ci(conn, old["ci_id"])
    new = find_version(conn, ci, to)
    if new["id"] == old["id"]:
        raise CMError("a version can't be remapped to itself")
    if not new["source_key"]:
        raise CMError(f"{new['name']} isn't from the release source; there's nothing to remap to")
    busy = _in_use(conn, "version", new["id"])
    if busy:
        raise Conflict(f"{new['name']} is already in use, so {old['name']} can't take it over", busy)
    if new["release_id"] != old["release_id"] and old["status"] != "planned":
        raise CMError(f"{old['name']} is {old['status']} in {_label(conn, 'release_id', old['release_id'])}; "
                      f"it can't move to {_label(conn, 'release_id', new['release_id'])}")
    _drop_unused(conn, "version", new)
    try:
        _take_over(conn, "version", old, new, ("name", "planned_date", "release_id"), {"planned": 1})
    except sqlite3.IntegrityError as e:
        raise Conflict(f"can't remap {old['name']} to {new['name']}: {e}") from None
    log(conn, "version", old["id"], "remapped", was=old["name"], to=new["name"], source_key=new["source_key"])
    rebuild_lineage(conn, ci["id"])
    return get_version(conn, old["id"])


def detach_release(conn, release_id):
    """Stop syncing a release (and its builds): it stays, as if entered by hand. For things gone from the source."""
    rel = get_release(conn, release_id)
    conn.execute("UPDATE release SET source_key = NULL, source_state = NULL, source = 'manual', pinned = '[]' "
                 "WHERE id = ?", (rel["id"],))
    conn.execute("UPDATE version SET source_key = NULL, source_state = NULL, pinned = '[]' WHERE release_id = ?",
                 (rel["id"],))
    log(conn, "release", rel["id"], "detached", source_key=rel["source_key"])
    rebuild_lineage(conn, rel["ci_id"])
    return release_detail(conn, rel["id"])


def detach_version(conn, version_id):
    ver = get_version(conn, version_id)
    conn.execute("UPDATE version SET source_key = NULL, source_state = NULL, pinned = '[]' WHERE id = ?", (ver["id"],))
    log(conn, "version", ver["id"], "detached", source_key=ver["source_key"])
    return get_version(conn, ver["id"])


def ci_attention(conn, ci_ref):
    """What needs a person on this CI: what the last sync couldn't place, things missing from the source,
    patches without a base version, emergencies without a reason, more than one open patch/emergency per line."""
    ci = get_ci(conn, ci_ref)
    out = []
    add = lambda level, kind, name, text, **ids: out.append({"level": level, "kind": kind, "name": name,
                                                             "text": text, **ids})
    for i in (json.loads(ci["last_sync"]) if ci["last_sync"] else {}).get("issues", []):
        add("warning", "unplaced", i["name"], i["why"], key=i["key"])
    for r in conn.execute("SELECT id, name FROM release WHERE ci_id = ? AND source_state = 'missing' "
                          "AND status != 'cancelled' ORDER BY id", (ci["id"],)):
        add("danger", "missing_release", r["name"], "no longer in the release source", release_id=r["id"])
    for v in conn.execute("SELECT v.id, v.name, r.name AS release FROM version v JOIN release r ON r.id = v.release_id "
                          "WHERE v.ci_id = ? AND v.source_state = 'missing' AND v.status != 'rejected' "
                          "AND r.status != 'cancelled' AND COALESCE(r.source_state, '') != 'missing' ORDER BY v.id",
                          (ci["id"],)):
        add("danger", "missing_version", v["name"], f"build of {v['release']} no longer in the release source",
            version_id=v["id"])
    for r in conn.execute("SELECT id, name, kind, reason, base_version_id FROM release WHERE ci_id = ? "
                          "AND parent_id IS NOT NULL AND status IN ('planned', 'active') ORDER BY id", (ci["id"],)):
        if r["base_version_id"] is None:
            add("warning", "no_base", r["name"], "no base version yet: nothing on its line is released, or set one",
                release_id=r["id"])
        if r["kind"] == "emergency" and not r["reason"]:
            add("warning", "no_reason", r["name"], "emergency with no reason / change request", release_id=r["id"])
    for r in conn.execute("SELECT p.name, c.kind, COUNT(*) AS n FROM release c JOIN release p ON p.id = c.parent_id "
                          "WHERE c.ci_id = ? AND c.status IN ('planned', 'active') GROUP BY p.id, c.kind HAVING n > 1",
                          (ci["id"],)):
        add("warning", "open_children", r["name"], f"{r['n']} open {r['kind']} releases on this line")
    return out


def remap_candidates(conn, ci_ref):
    """Fresh synced releases and builds a missing one could be remapped to: {"releases": [...], "versions": [...]}."""
    ci = get_ci(conn, ci_ref)
    rels = [dict(r) for r in conn.execute("SELECT id, name, kind FROM release WHERE ci_id = ? AND source_state = 'synced' "
                                          "AND status = 'planned' ORDER BY id DESC", (ci["id"],))
            if not _in_use(conn, "release", r["id"])]
    vers = [dict(v) for v in conn.execute("SELECT v.id, v.name, r.name AS release FROM version v "
                                          "JOIN release r ON r.id = v.release_id WHERE v.ci_id = ? "
                                          "AND v.source_state = 'synced' AND v.status = 'planned' ORDER BY v.id DESC",
                                          (ci["id"],))
            if not _in_use(conn, "version", v["id"])]
    return {"releases": rels, "versions": vers}


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


# ----------------------------------------------------------------------------- tickets (work items)
#
# Tickets are not stored here: the TicketSource is the system of record and is asked on every request
# (it may cache if it wants to). cmtrack contributes the version set (lineage ranges) and the CSC mapping.

def _ask(source, method, *args):
    """Call a TicketSource method; normalize records; turn source failures into a 502 CMError."""
    if source is None:
        raise CMError("no ticket source configured (set CMTRACK_TICKET_SOURCES)")
    try:
        records = getattr(source, method)(*args)
        return [r if isinstance(r, tickets.TicketRecord) else tickets.TicketRecord.from_dict(r)
                for r in (records or [])]
    except CMError:
        raise
    except NotImplementedError as e:
        raise CMError(str(e) or f"ticket source {source.name!r} doesn't support {method}") from None
    except Exception as e:                       # the source is outside our control; report, don't crash
        raise SourceError(f"ticket source {source.name!r} failed in {method}: {e}") from e


def _progress(rows):
    """Ticket counts per state (all states, workflow order) plus total."""
    counts = {s: 0 for s in tickets.STATES}
    for r in rows:
        counts[r["state"] if r["state"] in counts else tickets.ERROR] += 1
    return {**counts, "total": len(rows)}


class _Resolver:
    """Maps source records onto cmtrack: Jira pair -> CSC/CI, fix version names -> versions of that CI."""

    def __init__(self, conn, source_name):
        self.conn, self.source = conn, source_name
        self.cscs = {(r["jira_project"], r["affected_product"]): dict(r) for r in conn.execute(
            "SELECT csc.*, ci.name AS ci FROM csc JOIN ci ON ci.id = csc.ci_id WHERE jira_project IS NOT NULL")}
        self.warnings = []

    def ticket(self, rec, only_versions=None):
        """Record -> dict for reports. ``only_versions`` ({name: id}) limits fix versions to a version set."""
        csc = self.cscs.get((rec.project, rec.affected_product)) if (rec.project or rec.affected_product) else None
        if csc is None and (rec.project or rec.affected_product):
            self.warnings.append(f"{rec.key}: no CSC mapped to ({rec.project}, {rec.affected_product})")
        versions = []
        for name in rec.fix_versions or []:
            if only_versions is not None:
                if name in only_versions:
                    versions.append({"id": only_versions[name], "name": name})
            elif csc:
                v = self.conn.execute("SELECT id FROM version WHERE ci_id = ? AND name = ?",
                                      (csc["ci_id"], name)).fetchone()
                if v is None:
                    self.warnings.append(f"{rec.key}: fix version {name!r} is not a version of {csc['ci']}")
                versions.append({"id": v["id"] if v else None, "name": name})
            else:
                versions.append({"id": None, "name": name})
        d = rec.to_dict()
        d.update(source=self.source, csc=csc["name"] if csc else None, team=csc["team"] if csc else None,
                 ci=csc["ci"] if csc else None, versions=versions)
        return d

    def parents(self, source, keys):
        """{key: ticket} for parent keys, with a placeholder 'error' ticket for any the source doesn't return."""
        keys = sorted(set(keys))
        found = {r.key: self.ticket(r) for r in _ask(source, "get_tickets", keys)} if keys else {}
        for k in keys:
            if k not in found:
                found[k] = self.ticket(tickets.TicketRecord(
                    key=k, state=tickets.ERROR, state_reason="parent ticket not found in the source"))
        return found


def _group_by_csc(rows):
    groups = {}
    for r in sorted(rows, key=lambda r: (r["ci"] or "~", r["csc"] or "~", r["key"])):
        groups.setdefault((r["ci"], r["csc"]), []).append(r)
    return [{"ci": ci, "csc": csc, "team": ts[0]["team"], "tickets": ts, "progress": _progress(ts)}
            for (ci, csc), ts in groups.items()]


def work_report(conn, source, ci_ref, to=None, frm=None, versions=None):
    """CSC tickets fixed in a set of versions (asked of the source), grouped under parent tickets, then by CSC.

    The set is ``frm..to`` over the version DAG, or an explicit list of ``versions``.
    """
    ci = get_ci(conn, ci_ref)
    if versions:
        vs = [find_version(conn, ci, v) for v in versions]
    elif to:
        vs = version_range(conn, ci["id"], to, frm)
    else:
        raise CMError("give 'to' (and optionally 'from'), or 'versions'")
    report = {"ci": ci["name"], "source": getattr(source, "name", None), "from": frm or None,
              "to": to if not versions else None,
              "versions": [{"id": v["id"], "name": v["name"], "status": v["status"]} for v in vs]}
    cscs = to_dicts(conn.execute("SELECT * FROM csc WHERE ci_id = ? ORDER BY name", (ci["id"],)))
    names = {v["name"]: v["id"] for v in vs}
    res = _Resolver(conn, getattr(source, "name", None))
    rows = []
    for rec in _ask(source, "tickets_for_versions", to_dict(ci), cscs, list(names)):
        t = res.ticket(rec, names)
        if t["ci"] not in (None, ci["name"]):
            res.warnings.append(f"{rec.key}: belongs to {t['ci']}, not {ci['name']}; skipped")
        elif not t["versions"]:
            res.warnings.append(f"{rec.key}: none of its fix versions {rec.fix_versions} are in the requested set")
        else:
            rows.append(t)
    parents = res.parents(source, [r["parent_key"] for r in rows if r["parent_key"]])
    by_parent = {}
    for r in rows:
        by_parent.setdefault(r["parent_key"], []).append(r)
    report.update(
        progress=_progress(rows), parents=len([k for k in by_parent if k]), warnings=res.warnings,
        items=[{"parent": parents.get(k), "progress": _progress(ts), "cscs": _group_by_csc(ts)}
               for k, ts in sorted(by_parent.items(), key=lambda kv: (kv[0] is None, kv[0] or ""))])
    return report


def ticket_detail(conn, source, key):
    """A ticket from the source, its parent, and the CSC tickets under it grouped by CI/CSC."""
    found = _ask(source, "get_tickets", [key])
    rec = next((r for r in found if r.key == key), None)
    if rec is None:
        raise NotFound(f"ticket {key!r} not found in {source.name}")
    res = _Resolver(conn, source.name)
    out = res.ticket(rec)
    out["parent"] = res.parents(source, [rec.parent_key])[rec.parent_key] if rec.parent_key else None
    children = [res.ticket(c) for c in _ask(source, "get_children", key)]
    out["children"] = _group_by_csc(children)
    out["progress"] = _progress(children)
    out["warnings"] = res.warnings
    return out


# ----------------------------------------------------------------------------- backlogs
#
# A backlog shared by several teams: cmtrack owns membership and order (a lexorank per item);
# the tickets themselves are read live from the ticket source.

def get_backlog(conn, ref):
    return _by_ref(conn, "backlog", ref, "backlog")


def _backlog_cis(conn, backlog_id):
    return to_dicts(conn.execute("SELECT ci.* FROM backlog_ci b JOIN ci ON ci.id = b.ci_id "
                                 "WHERE b.backlog_id = ? ORDER BY ci.name", (backlog_id,)))


def _set_backlog_cis(conn, backlog_id, cis):
    ids = [get_ci(conn, c)["id"] for c in cis or []]
    conn.execute("DELETE FROM backlog_ci WHERE backlog_id = ?", (backlog_id,))
    conn.executemany("INSERT OR IGNORE INTO backlog_ci VALUES (?, ?)", [(backlog_id, i) for i in ids])


def _teams(teams):
    if teams is None:
        return []
    if isinstance(teams, str):
        teams = teams.split(",")
    if not isinstance(teams, list):
        raise CMError("teams must be a list of names")
    return [str(t).strip() for t in teams if str(t).strip()]


def list_backlogs(conn, ci_ref=None):
    sql = ("SELECT b.*, (SELECT COUNT(*) FROM backlog_item i WHERE i.backlog_id = b.id) AS item_count, "
           "(SELECT group_concat(ci.name, ', ') FROM backlog_ci x JOIN ci ON ci.id = x.ci_id "
           " WHERE x.backlog_id = b.id) AS ci_names FROM backlog b")
    args = []
    if ci_ref:
        sql += " WHERE b.id IN (SELECT backlog_id FROM backlog_ci WHERE ci_id = ?)"
        args.append(get_ci(conn, ci_ref)["id"])
    return to_dicts(conn.execute(sql + " ORDER BY b.name", args))


def create_backlog(conn, name, description=None, teams=None, cis=None, source=None):
    name = (name or "").strip()
    if not name:
        raise CMError("name is required")
    if name.isdigit():
        raise CMError("backlog name can't be only digits (it would read as an id)")
    try:
        cur = conn.execute("INSERT INTO backlog (name, description, teams, source) VALUES (?, ?, ?, ?)",
                           (name, description, json.dumps(_teams(teams)), source or None))
    except sqlite3.IntegrityError:
        raise Conflict(f"backlog {name!r} already exists") from None
    _set_backlog_cis(conn, cur.lastrowid, cis)
    log(conn, "backlog", cur.lastrowid, "created", name=name, teams=_teams(teams), cis=cis or [])
    return backlog_summary(conn, cur.lastrowid)


def update_backlog(conn, ref, **fields):
    b = get_backlog(conn, ref)
    unknown = set(fields) - {"name", "description", "teams", "cis", "source"}
    if unknown:
        raise CMError(f"cannot update {sorted(unknown)}")
    sets = {}
    if "name" in fields:
        if not str(fields["name"] or "").strip() or str(fields["name"]).strip().isdigit():
            raise CMError("name is required and can't be only digits")
        sets["name"] = str(fields["name"]).strip()
    if "description" in fields:
        sets["description"] = fields["description"]
    if "teams" in fields:
        sets["teams"] = json.dumps(_teams(fields["teams"]))
    if "source" in fields:
        sets["source"] = fields["source"] or None
    if sets:
        try:
            conn.execute(f"UPDATE backlog SET {', '.join(k + ' = ?' for k in sets)} WHERE id = ?",
                         (*sets.values(), b["id"]))
        except sqlite3.IntegrityError:
            raise Conflict(f"backlog {sets.get('name')!r} already exists") from None
    if "cis" in fields:
        _set_backlog_cis(conn, b["id"], fields["cis"])
    log(conn, "backlog", b["id"], "updated", **fields)
    return backlog_summary(conn, b["id"])


def backlog_summary(conn, ref):
    b = to_dict(get_backlog(conn, ref))
    b["cis"] = [c["name"] for c in _backlog_cis(conn, b["id"])]
    b["item_count"] = conn.execute("SELECT COUNT(*) FROM backlog_item WHERE backlog_id = ?",
                                   (b["id"],)).fetchone()[0]
    return b


def _items(conn, backlog_id):
    return conn.execute("SELECT * FROM backlog_item WHERE backlog_id = ? ORDER BY rank", (backlog_id,)).fetchall()


def _item(conn, backlog_id, key):
    return _one(conn, "SELECT * FROM backlog_item WHERE backlog_id = ? AND ticket_key = ?",
                (backlog_id, key), f"{key} in this backlog")


def _append(conn, backlog_id, keys, top=False):
    """Add keys (in order) at the bottom, or the top, of a backlog; returns their ranks."""
    edge = conn.execute(f"SELECT rank FROM backlog_item WHERE backlog_id = ? ORDER BY rank {'ASC' if top else 'DESC'} "
                        "LIMIT 1", (backlog_id,)).fetchone()
    edge = edge["rank"] if edge else None
    ranks = []
    for key in (reversed(keys) if top else keys):
        edge = rank.between(None, edge) if top else rank.between(edge, None)
        conn.execute("INSERT INTO backlog_item (backlog_id, ticket_key, rank) VALUES (?, ?, ?)",
                     (backlog_id, key, edge))
        ranks.append(edge)
    return ranks


def add_backlog_item(conn, source, ref, key, position="bottom"):
    """Put a top-level ticket on the backlog (checked against the source) at the top or bottom."""
    b = get_backlog(conn, ref)
    key = str(key or "").strip()
    if not key:
        raise CMError("key is required")
    if position not in ("top", "bottom"):
        raise CMError("position must be top or bottom")
    if conn.execute("SELECT 1 FROM backlog_item WHERE backlog_id = ? AND ticket_key = ?", (b["id"], key)).fetchone():
        raise Conflict(f"{key} is already in {b['name']}")
    rec = next((r for r in _ask(source, "get_tickets", [key]) if r.key == key), None)
    if rec is None:
        raise NotFound(f"ticket {key!r} not found in {source.name}")
    if rec.parent_key:
        raise CMError(f"{key} is a CSC ticket under {rec.parent_key}; add the top-level ticket instead")
    _append(conn, b["id"], [key], top=position == "top")
    log(conn, "backlog", b["id"], "item_added", key=key, position=position)
    return {"key": key, "rank": _item(conn, b["id"], key)["rank"]}


def remove_backlog_item(conn, ref, key):
    b = get_backlog(conn, ref)
    _item(conn, b["id"], key)
    conn.execute("DELETE FROM backlog_item WHERE backlog_id = ? AND ticket_key = ?", (b["id"], key))
    log(conn, "backlog", b["id"], "item_removed", key=key)
    return {"removed": key}


def move_backlog_item(conn, ref, key, after=None, before=None):
    """Re-rank ``key`` to sit between its new neighbours: ``after`` (the item now above it) and/or
    ``before`` (the item now below it). Give one to move next to an item, both after a drag and drop.
    Only the moved item's rank changes."""
    b = get_backlog(conn, ref)
    item = _item(conn, b["id"], key)
    if key in (after, before):
        raise CMError("an item can't be its own neighbour")
    if not after and not before:
        raise CMError("give 'after' and/or 'before' (the keys of the new neighbours)")
    lo = _item(conn, b["id"], after)["rank"] if after else None
    hi = _item(conn, b["id"], before)["rank"] if before else None
    others = "backlog_id = ? AND ticket_key != ?"
    if after and not before:
        row = conn.execute(f"SELECT rank FROM backlog_item WHERE {others} AND rank > ? ORDER BY rank LIMIT 1",
                           (b["id"], key, lo)).fetchone()
        hi = row["rank"] if row else None
    elif before and not after:
        row = conn.execute(f"SELECT rank FROM backlog_item WHERE {others} AND rank < ? ORDER BY rank DESC LIMIT 1",
                           (b["id"], key, hi)).fetchone()
        lo = row["rank"] if row else None
    if lo is not None and hi is not None and lo >= hi:
        raise Conflict(f"{after} is not above {before} any more; reload the backlog and try again")
    new = rank.between(lo, hi)
    conn.execute("UPDATE backlog_item SET rank = ? WHERE backlog_id = ? AND ticket_key = ?", (new, b["id"], key))
    log(conn, "backlog", b["id"], "item_moved", key=key, after=after, before=before)
    return {"key": key, "rank": new, "previous_rank": item["rank"]}


def rebalance_backlog(conn, ref):
    """Re-space every rank evenly (order unchanged), e.g. after many drops in the same spot."""
    b = get_backlog(conn, ref)
    keys = [r["ticket_key"] for r in _items(conn, b["id"])]
    ranks = rank.spread(len(keys))
    # two steps, because UNIQUE(backlog_id, rank) would trip over ranks that are still in use
    conn.execute("UPDATE backlog_item SET rank = '~' || ticket_key WHERE backlog_id = ?", (b["id"],))
    conn.executemany("UPDATE backlog_item SET rank = ? WHERE backlog_id = ? AND ticket_key = ?",
                     [(r, b["id"], k) for k, r in zip(keys, ranks)])
    log(conn, "backlog", b["id"], "rebalanced", items=len(keys))
    return {"items": len(keys), "max_rank_length": max(map(len, ranks), default=0)}


def pull_backlog(conn, source, ref):
    """Append top-level tickets the source offers for this backlog that aren't on it yet (source order)."""
    b = backlog_summary(conn, ref)
    have = {r["ticket_key"] for r in _items(conn, b["id"])}
    offered = _ask(source, "top_level_tickets", b, _backlog_cis(conn, b["id"]))
    new, seen = [], set()
    for rec in offered:
        if rec.parent_key or rec.key in have or rec.key in seen:
            continue
        seen.add(rec.key)
        new.append(rec.key)
    _append(conn, b["id"], new)
    if new:
        log(conn, "backlog", b["id"], "pulled", source=source.name, added=new)
    return {"added": new, "offered": len(offered), "already": len(offered) - len(new)}


def backlog_view(conn, source, ref):
    """The backlog in rank order with each ticket read live from the source (one get_tickets call)."""
    b = backlog_summary(conn, ref)
    rows = _items(conn, b["id"])
    found = {r.key: r for r in _ask(source, "get_tickets", [r["ticket_key"] for r in rows])} if rows else {}
    res = _Resolver(conn, getattr(source, "name", None))
    items = []
    for pos, row in enumerate(rows, 1):
        rec = found.get(row["ticket_key"]) or tickets.TicketRecord(
            key=row["ticket_key"], state=tickets.ERROR, state_reason="not found in the ticket source")
        t = res.ticket(rec)
        t.update(position=pos, rank=row["rank"], added_at=row["added_at"])
        items.append(t)
    b.update(items=items, progress=_progress(items), warnings=res.warnings,
             max_rank_length=max((len(r["rank"]) for r in rows), default=0))
    return b


# ----------------------------------------------------------------------------- events

def list_events(conn, entity=None, entity_id=None, limit=200, before_id=None):
    sql, args = "SELECT * FROM event WHERE 1=1", []
    if before_id is not None:
        sql += " AND id < ?"
        args.append(before_id)
    if entity:
        sql += " AND entity = ?"
        args.append(entity)
    if entity_id is not None:
        sql += " AND entity_id = ?"
        args.append(entity_id)
    return conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, int(limit))).fetchall()

"""Read-only HTML views (mounted at /). Jinja pages, with htmx fragments for the interactive parts.

A view renders its fragment template for an htmx request and the full page otherwise, so every
URL also works as a plain link, a bookmark or a history restore.
"""
from flask import Blueprint, render_template, request, url_for

from . import service as svc
from .db import get_db

bp = Blueprint("ui", __name__)

EVENT_PAGE = 50
ENTITY_ENDPOINTS = {"ci": "ui.ci", "release": "ui.release", "version": "ui.version",
                    "baseline": "ui.baseline", "ifc": "ui.ifc"}


def is_fragment():
    h = request.headers
    return "HX-Request" in h and "HX-Boosted" not in h and "HX-History-Restore-Request" not in h


def page(template, fragment, **ctx):
    return render_template(fragment if is_fragment() else template, **ctx), 200, {"Vary": "HX-Request"}


@bp.app_template_filter("ts")
def ts(value):
    """'2026-10-03T14:05:22+00:00' / '2026-10-03 14:05:22' -> '2026-10-03 14:05'."""
    return str(value)[:16].replace("T", " ") if value else ""


@bp.app_template_global()
def entity_url(entity, entity_id):
    endpoint = ENTITY_ENDPOINTS.get(entity)
    if endpoint is None or entity_id is None:
        return None
    arg = {"ci": "ref", "ifc": "ref", "release": "rid", "version": "vid", "baseline": "bid"}[entity]
    return url_for(endpoint, **{arg: entity_id})


# ----------------------------------------------------------------------------- helpers

def with_staleness(conn, entries):
    """Flag baseline entries whose version is no longer its release family's effective version."""
    out = []
    for e in entries:
        ver = svc.get_version(conn, e["version_id"])
        eff = svc.effective_version(conn, ver["release_id"])
        out.append({**e, "effective": eff["name"] if eff and eff["id"] != ver["id"] else None})
    return out


def release_families(releases):
    """Root releases in order, each followed by its patch/emergency children."""
    children = {}
    for r in releases:
        if r["parent_id"]:
            children.setdefault(r["parent_id"], []).append(r)
    return [(r, children.get(r["id"], [])) for r in releases if not r["parent_id"]]


# ----------------------------------------------------------------------------- dashboard

@bp.get("/")
def dashboard():
    conn = get_db()
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
    counts = {
        "managed": q("SELECT COUNT(*) FROM ci WHERE managed = 1"),
        "placeholders": q("SELECT COUNT(*) FROM ci WHERE managed = 0"),
        "open_releases": q("SELECT COUNT(*) FROM release WHERE status IN ('planned', 'active')"),
        "approved_baselines": q("SELECT COUNT(*) FROM baseline WHERE status = 'approved'"),
    }
    open_children = svc.to_dicts(conn.execute(
        "SELECT r.*, c.name AS ci, p.name AS parent FROM release r JOIN ci c ON c.id = r.ci_id "
        "JOIN release p ON p.id = r.parent_id WHERE r.status IN ('planned', 'active') "
        "ORDER BY r.kind DESC, r.created_at"))
    upcoming = svc.to_dicts(conn.execute(
        "SELECT r.*, c.name AS ci FROM release r JOIN ci c ON c.id = r.ci_id "
        "WHERE r.kind = 'planned' AND r.status IN ('planned', 'active') "
        "ORDER BY r.target_date IS NULL, r.target_date, c.name LIMIT 8"))
    stale = []
    for b in conn.execute("SELECT b.id, b.name, i.name AS ifc FROM baseline b JOIN ifc i ON i.id = b.ifc_id "
                          "WHERE b.status = 'approved' ORDER BY i.name"):
        stale += [{**e, "baseline": b["name"], "baseline_id": b["id"], "ifc": b["ifc"]}
                  for e in with_staleness(conn, svc.baseline_entries(conn, b["id"])) if e["effective"]]
    return render_template("dashboard.html", counts=counts, open_children=open_children, upcoming=upcoming,
                           stale=stale, ticket_errors=svc.tickets_in_error(conn),
                           events=svc.to_dicts(svc.list_events(conn, limit=10)))


@bp.get("/fragments/recent-events")
def recent_events():
    return render_template("_recent_events.html", events=svc.to_dicts(svc.list_events(get_db(), limit=10)))


# ----------------------------------------------------------------------------- CIs, releases, versions

@bp.get("/cis")
def cis():
    a = request.args
    filters = {"q": a.get("q", "").strip(), "type": a.get("type", ""), "managed": a.get("managed", "")}
    sql = """SELECT c.*, p.name AS policy,
                    (SELECT COUNT(*) FROM release r WHERE r.ci_id = c.id AND r.status IN ('planned', 'active'))
                        AS open_releases,
                    (SELECT r.name FROM release r WHERE r.ci_id = c.id AND r.status = 'released'
                        ORDER BY r.released_at DESC, r.id DESC LIMIT 1) AS last_release,
                    (SELECT r.name FROM release r WHERE r.ci_id = c.id AND r.kind = 'planned'
                        AND r.status IN ('planned', 'active') ORDER BY r.target_date LIMIT 1) AS next_release,
                    (SELECT r.target_date FROM release r WHERE r.ci_id = c.id AND r.kind = 'planned'
                        AND r.status IN ('planned', 'active') ORDER BY r.target_date LIMIT 1) AS next_target
             FROM ci c LEFT JOIN policy p ON p.id = c.policy_id WHERE 1=1"""
    args = []
    if filters["q"]:
        sql += " AND (c.name LIKE ? OR c.description LIKE ?)"
        args += [f"%{filters['q']}%"] * 2
    if filters["type"] in ("CSCI", "HWCI"):
        sql += " AND c.type = ?"
        args.append(filters["type"])
    if filters["managed"] in ("0", "1"):
        sql += " AND c.managed = ?"
        args.append(int(filters["managed"]))
    rows = svc.to_dicts(get_db().execute(sql + " ORDER BY c.managed DESC, c.name", args))
    return page("cis.html", "_ci_rows.html", cis=rows, filters=filters)


@bp.get("/cis/<ref>")
def ci(ref):
    conn = get_db()
    detail = svc.ci_detail(conn, ref)
    fielded = svc.to_dicts(conn.execute(
        "SELECT b.id, b.name, i.name AS ifc, v.id AS version_id, v.name AS version FROM baseline_entry e "
        "JOIN baseline b ON b.id = e.baseline_id JOIN ifc i ON i.id = b.ifc_id "
        "JOIN version v ON v.id = e.version_id WHERE e.ci_id = ? AND b.status = 'approved' ORDER BY i.name",
        (detail["id"],)))
    return render_template("ci.html", ci=detail, families=release_families(detail["releases"]), fielded=fielded)


@bp.get("/releases/<int:rid>")
def release(rid):
    conn = get_db()
    return page("release.html", "_release.html", r=svc.release_detail(conn, rid),
                ci=svc.get_ci(conn, svc.get_release(conn, rid)["ci_id"]), work_range=svc.release_range(conn, rid))


@bp.get("/versions/<int:vid>")
def version(vid):
    conn = get_db()
    ver = svc.to_dict(svc.get_version(conn, vid))
    return render_template(
        "version.html", v=ver, ci=svc.get_ci(conn, ver["ci_id"]), rel=svc.get_release(conn, ver["release_id"]),
        manifest=svc.manifest(conn, vid), used=svc.where_used(conn, vid), lineage=svc.lineage(conn, vid),
        report=svc.work_report(conn, ver["ci_id"], versions=[vid]),
        events=svc.to_dicts(svc.list_events(conn, "version", vid, 50)))


# ----------------------------------------------------------------------------- work items

@bp.get("/cis/<ref>/work")
def work(ref):
    """Parent tickets -> CSC -> CSC tickets for a version range (from..to over the lineage DAG)."""
    conn = get_db()
    ci = svc.get_ci(conn, ref)
    versions = svc.ci_versions(conn, ci["id"])
    presets = []
    for root, kids in release_families(svc.list_releases(conn, ci["id"])):
        for rel in [root] + kids:
            if rel["kind"] == "external" or rel["status"] == "cancelled":
                continue
            rng = svc.release_range(conn, rel["id"])
            if rng:
                presets.append({"release": rel["name"], "kind": rel["kind"], "status": rel["status"], **rng})
    frm, to = request.args.get("from") or None, request.args.get("to") or None
    if to is None and "to" not in request.args:
        # default: what's new in the latest release that has shipped, else the next one planned
        shipped = [p for p in presets if p["status"] == "released" and p["kind"] == "planned"]
        pick = shipped[-1] if shipped else (presets or [None])[0]
        frm, to = (pick["from"], pick["to"]) if pick else (None, None)
    report = svc.work_report(conn, ci["id"], to, frm) if to else None
    return page("work.html", "_work.html", ci=ci, versions=versions, presets=presets, frm=frm, to=to, report=report)


@bp.get("/tickets/<key>")
def ticket(key):
    return render_template("ticket.html", t=svc.ticket_detail(get_db(), key, request.args.get("source")))


# ----------------------------------------------------------------------------- IFCs & baselines

@bp.get("/ifcs")
def ifcs():
    conn = get_db()
    rows = svc.to_dicts(svc.list_ifcs(conn))
    for r in rows:
        cur = svc.current_baseline(conn, r["id"])
        r["current"] = svc.to_dict(cur)
    children = {}
    for r in rows:
        children.setdefault(r["parent_id"], []).append(r)
    return render_template("ifcs.html", children=children, total=len(rows))


@bp.get("/ifcs/<ref>")
def ifc(ref):
    conn = get_db()
    detail = svc.ifc_detail(conn, ref)
    current = detail["current_hscm"]
    entries = with_staleness(conn, current["entries"]) if current else []
    return render_template("ifc.html", ifc=detail, current=current, entries=entries)


@bp.get("/baselines/<int:bid>")
def baseline(bid):
    conn = get_db()
    b = svc.baseline_detail(conn, bid)
    others = svc.to_dicts(conn.execute(
        "SELECT id, name, status FROM baseline WHERE ifc_id = ? AND id != ? ORDER BY id DESC", (b["ifc_id"], bid)))
    return render_template("baseline.html", b=b, entries=with_staleness(conn, b["entries"]), others=others,
                           compare=request.args.get("compare", type=int) or b["supersedes_id"])


@bp.get("/baselines/<int:bid>/diff")
def baseline_diff(bid):
    """Changes from ?other=<id> (the older baseline) to this one."""
    conn = get_db()
    other = request.args.get("other", type=int)
    if not other:
        return render_template("_diff.html", diff=None)
    return render_template("_diff.html", diff=svc.diff_baselines(conn, other, bid),
                           a=svc.get_baseline(conn, other), b=svc.get_baseline(conn, bid))


# ----------------------------------------------------------------------------- events

@bp.get("/events")
def events():
    a = request.args
    entity, entity_id, before = a.get("entity") or None, a.get("entity_id", type=int), a.get("before", type=int)
    rows = svc.to_dicts(svc.list_events(get_db(), entity, entity_id, EVENT_PAGE + 1, before))
    more = len(rows) > EVENT_PAGE
    rows = rows[:EVENT_PAGE]
    entities = [r[0] for r in get_db().execute("SELECT DISTINCT entity FROM event ORDER BY entity")]
    return page("events.html", "_event_rows.html", events=rows, more=more, entities=entities,
                filters={"entity": entity or "", "entity_id": entity_id})

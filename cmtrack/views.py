"""HTML views (mounted at /). Jinja pages, with htmx fragments for the interactive parts.

A view renders its fragment template for an htmx request and the full page otherwise, so every
URL also works as a plain link, a bookmark or a history restore. Every page extends ui/layout.html and is
built from the macros in ui/components.html. Forms post here: backlog forms get the refreshed list fragment
back (drag-and-drop ranking in static/backlog.js calls the JSON API, POST /api/backlogs/<b>/items/<key>/move);
release and version forms (sync, add, correct, remap, detach) redirect back to the page they came from.
"""
import json

from flask import Blueprint, current_app, redirect, render_template, request, url_for

from . import graph, service as svc, tickets, ui
from .db import get_db

bp = Blueprint("ui", __name__)

EVENT_PAGE = 50
ENTITY_ENDPOINTS = {"ci": "ui.ci", "release": "ui.release", "version": "ui.version",
                    "baseline": "ui.baseline", "ifc": "ui.ifc", "backlog": "ui.backlog"}


def is_fragment():
    h = request.headers
    return "HX-Request" in h and "HX-Boosted" not in h and "HX-History-Restore-Request" not in h


def page(template, fragment, **ctx):
    return render_template(fragment if is_fragment() else template, **ctx), 200, {"Vary": "HX-Request"}


@bp.app_template_global()
def entity_url(entity, entity_id):
    endpoint = ENTITY_ENDPOINTS.get(entity)
    if endpoint is None or entity_id is None:
        return None
    arg = {"ci": "ref", "ifc": "ref", "backlog": "ref", "release": "rid", "version": "vid", "baseline": "bid"}[entity]
    return url_for(endpoint, **{arg: entity_id})


# ----------------------------------------------------------------------------- helpers

def ticket_source(default=None):
    try:
        return tickets.pick_source(current_app.config["TICKET_SOURCES"], request.args.get("source") or default)
    except KeyError as e:
        raise svc.CMError(e.args[0]) from None


def live(fn, *args, **kwargs):
    """Call something that asks the ticket source: (result, None), or (None, message) if it can't answer."""
    try:
        return fn(*args, **kwargs), None
    except svc.CMError as e:
        return None, e.message


def with_staleness(conn, entries):
    """Flag baseline entries whose version is no longer its release family's effective version."""
    out = []
    for e in entries:
        ver = svc.get_version(conn, e["version_id"])
        eff = svc.effective_version(conn, ver["release_id"])
        out.append({**e, "effective": eff["name"] if eff and eff["id"] != ver["id"] else None})
    return out


def ci_overview(conn, releases):
    """What the CI page's stat strip shows, plus which release panel to open first."""
    planned = [r for r in releases if r["kind"] == "planned"]
    shipped = [r for r in planned if r["status"] == "released"]
    upcoming = [r for r in planned if r["status"] in ("planned", "active")]
    current, nxt = (shipped[-1] if shipped else None), (upcoming[0] if upcoming else None)
    effective = svc.effective_version(conn, current["id"]) if current else None
    built = total = 0
    if nxt:
        built, total = conn.execute(
            "SELECT COALESCE(SUM(status IN ('built', 'tested', 'released')), 0), COUNT(*) FROM version "
            "WHERE release_id = ?", (nxt["id"],)).fetchone()
    focus = nxt or current or (releases[0] if releases else None)
    return {"current": current, "effective": effective["name"] if effective else None, "next": nxt,
            "next_built": built, "next_total": total,
            "open_children": [r for r in releases if r["parent_id"] and r["status"] in ("planned", "active")],
            "behind": svc.behind_effective(conn, current["id"]) if current else [],
            "focus_id": focus["id"] if focus else None}


def built_from(conn, rel):
    """The versions a release's first build was built from, outside the release itself (later ones are merges)."""
    if not rel["versions"]:
        return []
    parents = [p for p in svc.version_parents(conn, rel["versions"][0]["id"]) if p["release_id"] != rel["id"]]
    return [{"name": p["name"], "href": url_for("ui.version", vid=p["id"]), "merge": i > 0}
            for i, p in enumerate(parents)]


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
                           stale=stale, events=svc.to_dicts(svc.list_events(conn, limit=10)))


@bp.get("/fragments/recent-events")
def recent_events():
    return render_template("_recent_events.html", events=svc.to_dicts(svc.list_events(get_db(), limit=10)))


# ----------------------------------------------------------------------------- CIs, releases, versions

@bp.get("/cis")
def cis():
    a = request.args
    filters = {"q": a.get("q", "").strip(), "type": a.get("type", ""), "managed": a.get("managed", "")}
    sql = """SELECT c.*,
                    (SELECT COUNT(*) FROM release r WHERE r.ci_id = c.id AND r.status IN ('planned', 'active'))
                        AS open_releases,
                    (SELECT r.name FROM release r WHERE r.ci_id = c.id AND r.status = 'released'
                        ORDER BY r.released_at DESC, r.id DESC LIMIT 1) AS last_release,
                    (SELECT r.name FROM release r WHERE r.ci_id = c.id AND r.kind = 'planned'
                        AND r.status IN ('planned', 'active') ORDER BY r.target_date LIMIT 1) AS next_release,
                    (SELECT r.target_date FROM release r WHERE r.ci_id = c.id AND r.kind = 'planned'
                        AND r.status IN ('planned', 'active') ORDER BY r.target_date LIMIT 1) AS next_target
             FROM ci c WHERE 1=1"""
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
    return render_ci(get_db(), ref)


def render_ci(conn, ref, preview=None):
    detail = svc.ci_detail(conn, ref)
    fielded = svc.to_dicts(conn.execute(
        "SELECT b.id, b.name, i.name AS ifc, v.id AS version_id, v.name AS version FROM baseline_entry e "
        "JOIN baseline b ON b.id = e.baseline_id JOIN ifc i ON i.id = b.ifc_id "
        "JOIN version v ON v.id = e.version_id WHERE e.ci_id = ? AND b.status = 'approved' ORDER BY i.name",
        (detail["id"],)))
    missing = any(a["kind"].startswith("missing") for a in detail["attention"])
    return render_template("ci.html", ci=detail, families=release_families(detail["releases"]), fielded=fielded,
                           backlogs=svc.list_backlogs(conn, detail["id"]), overview=ci_overview(conn, detail["releases"]),
                           candidates=svc.remap_candidates(conn, detail["id"]) if missing else None,
                           source_configured=detail["release_source"] in (current_app.config["RELEASE_SOURCES"] or {}),
                           preview=preview)


def edit_options(conn, r):
    """Choices for a release's edit form: planned releases (parents) and released versions of its line (bases)."""
    if r["kind"] == "planned":
        return {}
    parents = conn.execute("SELECT id, name FROM release WHERE ci_id = ? AND kind = 'planned' ORDER BY "
                           "target_date IS NULL, target_date, id", (r["ci_id"],)).fetchall()
    bases = conn.execute("SELECT v.name FROM version v JOIN release x ON x.id = v.release_id "
                         "WHERE (x.id = ? OR x.parent_id = ?) AND x.id != ? AND v.status IN ('released', 'external') "
                         "ORDER BY v.id", (r["parent_id"], r["parent_id"], r["id"])).fetchall()
    return {"parents": [p["name"] for p in parents], "bases": [b["name"] for b in bases]}


@bp.get("/releases/<int:rid>")
def release(rid):
    conn = get_db()
    r = svc.release_detail(conn, rid)
    return page("release.html", "_release.html", r=r, built_from=built_from(conn, r),
                work_range=svc.release_range(conn, rid), options=edit_options(conn, r),
                ci_row=svc.get_ci(conn, r["ci_id"]))


@bp.get("/versions/<int:vid>")
def version(vid):
    conn = get_db()
    ver = svc.to_dict(svc.get_version(conn, vid))
    return render_template(
        "version.html", v=ver, ci=svc.get_ci(conn, ver["ci_id"]), rel=svc.to_dict(svc.get_release(conn, ver["release_id"])),
        manifest=svc.manifest(conn, vid), used=svc.where_used(conn, vid), lineage=svc.lineage(conn, vid),
        **dict(zip(("report", "source_error"), live(svc.work_report, conn, ticket_source(), ver["ci_id"],
                                                   versions=[vid]))),
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
    report, source_error = live(svc.work_report, conn, ticket_source(), ci["id"], to, frm) if to else (None, None)
    return page("work.html", "_work.html", ci=ci, versions=versions, presets=presets, frm=frm, to=to,
                report=report, source_error=source_error)


@bp.get("/tickets/<key>")
def ticket(key):
    return render_template("ticket.html", t=svc.ticket_detail(get_db(), ticket_source(), key))


# ----------------------------------------------------------------------------- IFCs & baselines

BASELINE_DOTS = {"draft": "hollow", "superseded": "muted"}
VERSION_DOTS = {"planned": "hollow", "rejected": "danger"}


def baseline_graph(conn, ifc_id):
    """The IFC's baselines as a lineage graph (newest first), each hanging off the one it was derived from."""
    rows = svc.baseline_lineage(conn, ifc_id)
    return graph.layout([{**b, "parents": [b["derived_from_id"]] if b["derived_from_id"] else [],
                          "dot": BASELINE_DOTS.get(b["status"]), "current": b["status"] == "approved"}
                         for b in reversed(rows)])


def version_graph(conn, ci_id):
    """A CI's versions as a lineage graph (newest first): release lines, patch branches and merges."""
    parents = {}
    for child, parent in conn.execute("SELECT p.version_id, p.parent_id FROM version_parent p "
                                      "JOIN version v ON v.id = p.version_id WHERE v.ci_id = ?", (ci_id,)):
        parents.setdefault(child, []).append(parent)
    releases = {r["id"]: r for r in svc.list_releases(conn, ci_id)}
    versions = {v["id"]: svc.to_dict(v) for v in svc.ci_versions(conn, ci_id)}
    promoted = {r["released_version_id"] for r in releases.values()}

    def first(v):
        """Parents in drawing order: the one in its own release, else on a planned line, keeps the lane."""
        def rank(p):
            pv = versions.get(p)
            return (not pv or pv["release_id"] != v["release_id"],
                    not pv or releases[pv["release_id"]]["kind"] != "planned", p)
        return sorted(parents.get(v["id"], []), key=rank)

    nodes = []
    for v in versions.values():
        rel = releases[v["release_id"]]
        when = v["built_at"] or v["planned_date"] or rel["target_date"] or v["created_at"]
        nodes.append({**v, "parents": first(v), "rel": rel, "when": when, "dot": VERSION_DOTS.get(v["status"]),
                      "current": v["id"] in promoted})
    return graph.layout(graph.newest_first(nodes, key=lambda n: (n["when"] or "", n["seq"], n["id"])))


def ifc_options(conn, exclude=None):
    return [(r["name"], r["name"]) for r in svc.list_ifcs(conn) if r["id"] != exclude]


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
    return render_template("ifcs.html", children=children, total=len(rows), parents=ifc_options(conn))


@bp.get("/ifcs/<ref>")
def ifc(ref):
    conn = get_db()
    detail = svc.ifc_detail(conn, ref)
    current = detail["current_hscm"]
    entries = with_staleness(conn, current["entries"]) if current else []
    return render_template("ifc.html", ifc=detail, current=current, entries=entries,
                           lineage=baseline_graph(conn, detail["id"]), parents=ifc_options(conn, detail["id"]))


@bp.get("/baselines/<int:bid>")
def baseline(bid):
    conn = get_db()
    b = svc.baseline_detail(conn, bid)
    others = svc.to_dicts(conn.execute(
        "SELECT id, name, status FROM baseline WHERE ifc_id = ? AND id != ? ORDER BY id DESC", (b["ifc_id"], bid)))
    imported = conn.execute("SELECT detail FROM event WHERE entity = 'baseline' AND entity_id = ? "
                            "AND action = 'imported' ORDER BY id DESC LIMIT 1", (bid,)).fetchone()
    draft = b["status"] == "draft"
    return render_template("baseline.html", b=b, entries=with_staleness(conn, b["entries"]), others=others,
                           compare=request.args.get("compare", type=int) or b["supersedes_id"] or b["derived_from_id"],
                           warnings=json.loads(imported["detail"]).get("warnings", []) if imported else [],
                           choices=entry_choices(conn, b["entries"]) if draft else {},
                           cis=[(c["name"], c["name"]) for c in svc.list_cis(conn)] if draft else [])


def entry_choices(conn, entries):
    """For each CI in a draft, the versions it could list: newest first, rejected ones left out."""
    return {e["ci"]: version_options(conn, e["ci"]) for e in entries}


def version_options(conn, ci_ref):
    ci = svc.get_ci(conn, ci_ref)
    return [(v["id"], f"{v['name']} ({ui_status(v['status'])})") for v in conn.execute(
        "SELECT id, name, status FROM version WHERE ci_id = ? AND status != 'rejected' "
        "ORDER BY status NOT IN ('released', 'external'), id DESC", (ci["id"],))]


def ui_status(status):
    return ui.VERSION_STATUSES.get(status, status).lower()


@bp.get("/fragments/version-options")
def version_options_fragment():
    """<option>s for the version picker of the add-entry form (the CI is chosen first)."""
    ci = request.args.get("ci")
    return render_template("_version_options.html", options=version_options(get_db(), ci) if ci else [])


@bp.get("/cis/<ref>/lineage")
def ci_lineage(ref):
    conn = get_db()
    ci = svc.get_ci(conn, ref)
    return render_template("lineage.html", ci=ci, g=version_graph(conn, ci["id"]))


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


# ----------------------------------------------------------------------------- UI component reference

@bp.get("/ui")
def styleguide():
    """Living reference for the component library (templates/ui/components.html, static/ui.css)."""
    return render_template("ui/styleguide.html", **ui.styleguide_samples())


# ----------------------------------------------------------------------------- backlogs

@bp.get("/backlogs")
def backlogs():
    conn = get_db()
    return render_template("backlogs.html", backlogs=svc.list_backlogs(conn),
                           cis=svc.to_dicts(svc.list_cis(conn, managed=True)), error=None, form={})


@bp.post("/backlogs")
def create_backlog():
    conn = get_db()
    f = request.form
    try:
        with conn:
            b = svc.create_backlog(conn, f.get("name"), f.get("description") or None, f.get("teams"),
                                   f.getlist("cis"))
    except svc.CMError as e:
        # htmx only swaps 2xx responses, so the re-rendered form with its error comes back as 200 there
        return render_template("backlogs.html", backlogs=svc.list_backlogs(conn), error=e.message, form=f,
                               cis=svc.to_dicts(svc.list_cis(conn, managed=True))), \
            200 if "HX-Request" in request.headers else e.status
    target = url_for("ui.backlog", ref=b["name"])
    if "HX-Request" in request.headers:
        return "", 204, {"HX-Redirect": target}
    return redirect(target, 303)


def _backlog_fragment(conn, ref, message=None, error=None):
    b = svc.backlog_summary(conn, ref)
    view, source_error = live(svc.backlog_view, conn, ticket_source(b["source"]), ref)
    return render_template("_backlog_items.html", b=view or b, source_error=source_error,
                           message=message, error=error)


@bp.get("/backlogs/<ref>")
def backlog(ref):
    conn = get_db()
    if is_fragment():
        return _backlog_fragment(conn, ref), 200, {"Vary": "HX-Request"}
    b = svc.backlog_summary(conn, ref)
    view, source_error = live(svc.backlog_view, conn, ticket_source(b["source"]), ref)
    return render_template("backlog.html", b=view or b, source_error=source_error, message=None, error=None)


def _backlog_action(ref, action):
    """Run a backlog change for an htmx form and answer with the refreshed item list plus a message."""
    conn = get_db()
    b = svc.backlog_summary(conn, ref)
    try:
        with conn:
            message = action(conn, ticket_source(b["source"]), b)
    except svc.CMError as e:
        return _backlog_fragment(conn, ref, error=e.message)
    return _backlog_fragment(conn, ref, message=message)


@bp.post("/backlogs/<ref>/pull")
def backlog_pull(ref):
    def pull(conn, source, b):
        out = svc.pull_backlog(conn, source, b["id"])
        return (f"Added {len(out['added'])} from {source.name}: {', '.join(out['added'])}" if out["added"]
                else f"Nothing new from {source.name} ({out['offered']} offered, all already here)")
    return _backlog_action(ref, pull)


@bp.post("/backlogs/<ref>/add")
def backlog_add(ref):
    key, position = request.form.get("key", "").strip().upper(), request.form.get("position", "bottom")
    def add(conn, source, b):
        svc.add_backlog_item(conn, source, b["id"], key, position)
        return f"Added {key} at the {position}"
    return _backlog_action(ref, add)


@bp.post("/backlogs/<ref>/items/<key>/remove")
def backlog_remove(ref, key):
    def remove(conn, source, b):
        svc.remove_backlog_item(conn, b["id"], key)
        return f"Removed {key}"
    return _backlog_action(ref, remove)


@bp.post("/backlogs/<ref>/rebalance")
def backlog_rebalance(ref):
    def rebalance(conn, source, b):
        out = svc.rebalance_backlog(conn, b["id"])
        return f"Re-spaced {out['items']} ranks (longest is now {out['max_rank_length']} characters)"
    return _backlog_action(ref, rebalance)


# ----------------------------------------------------------------------------- release & version forms
#
# Plain HTML forms (boosted by htmx). Each runs one service call in a transaction and redirects back to
# ``next`` (a local path the form carries) or the given default; errors render the error page.

def _back(default):
    nxt = request.form.get("next") or ""
    return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else default, 303)


def _run(fn, *args, **kwargs):
    conn = get_db()
    with conn:
        return fn(conn, *args, **kwargs)


def _form(*keys):
    """The named form fields that were sent (empty strings kept: they clear a value)."""
    return {k: request.form[k] for k in keys if k in request.form}


@bp.post("/cis/<ref>/sync")
def sync_ci(ref):
    conn = get_db()
    ci = svc.get_ci(conn, ref)
    source = (current_app.config["RELEASE_SOURCES"] or {}).get(ci["release_source"])
    if request.form.get("dry_run"):
        with conn:
            summary = svc.sync_ci(conn, source, ci["id"], dry_run=True)
        return render_ci(conn, ci["name"], preview=summary)
    _run(svc.sync_ci, source, ci["id"])
    return _back(url_for("ui.ci", ref=ci["name"]))


@bp.post("/cis/<ref>/releases")
def create_release(ref):
    f = _form("name", "kind", "target_date", "parent", "reason")
    builds = [b.strip() for b in request.form.get("builds", "").split(",") if b.strip()]
    rel = _run(svc.create_release, ref, builds=builds or None, **{k: v for k, v in f.items() if v})
    return _back(url_for("ui.ci", ref=rel["ci"]))


@bp.post("/releases/<int:rid>/edit")
def edit_release(rid):
    _run(svc.update_release, rid, **_form("name", "target_date", "reason", "parent", "base_version"))
    return _back(url_for("ui.release", rid=rid))


@bp.post("/releases/<int:rid>/correct")
def correct_release(rid):
    _run(svc.update_release, rid, released_at=request.form.get("released_at"), note=request.form.get("note"))
    return _back(url_for("ui.release", rid=rid))


@bp.post("/releases/<int:rid>/unpin")
def unpin_release(rid):
    _run(svc.update_release, rid, unpin=[request.form.get("field", "")])
    return _back(url_for("ui.release", rid=rid))


@bp.post("/releases/<int:rid>/builds")
def add_build(rid):
    _run(svc.add_version, rid, request.form.get("name"), request.form.get("planned_date"))
    return _back(url_for("ui.release", rid=rid))


@bp.post("/releases/<int:rid>/remap")
def remap_release(rid):
    rel = _run(svc.remap_release, rid, request.form.get("to"))
    return _back(url_for("ui.ci", ref=rel["ci"]))


@bp.post("/releases/<int:rid>/detach")
def detach_release(rid):
    rel = _run(svc.detach_release, rid)
    return _back(url_for("ui.ci", ref=rel["ci"]))


@bp.post("/releases/<int:rid>/cancel")
def cancel_release(rid):
    rel = _run(svc.cancel_release, rid, request.form.get("note"))
    return _back(url_for("ui.ci", ref=rel["ci"]))


@bp.post("/versions/<int:vid>/edit")
def edit_version(vid):
    _run(svc.update_version, vid, **_form("name", "planned_date"))
    return _back(url_for("ui.version", vid=vid))


@bp.post("/versions/<int:vid>/correct")
def correct_version(vid):
    _run(svc.update_version, vid, built_at=request.form.get("built_at"), note=request.form.get("note"))
    return _back(url_for("ui.version", vid=vid))


@bp.post("/versions/<int:vid>/unpin")
def unpin_version(vid):
    _run(svc.update_version, vid, unpin=[request.form.get("field", "")])
    return _back(url_for("ui.version", vid=vid))


@bp.post("/versions/<int:vid>/remap")
def remap_version(vid):
    ver = _run(svc.remap_version, vid, request.form.get("to"))
    return _back(url_for("ui.version", vid=ver["id"]))


@bp.post("/versions/<int:vid>/detach")
def detach_version(vid):
    _run(svc.detach_version, vid)
    return _back(url_for("ui.version", vid=vid))


# ----------------------------------------------------------------------------- IFC & baseline forms

@bp.post("/ifcs")
def create_ifc():
    f = request.form
    ifc = _run(svc.create_ifc, f.get("name", "").strip(), f.get("parent") or None, f.get("description") or None)
    return _back(url_for("ui.ifc", ref=ifc["name"]))


@bp.post("/ifcs/<ref>/edit")
def edit_ifc(ref):
    ifc = _run(svc.update_ifc, ref, **_form("parent", "description"))
    return _back(url_for("ui.ifc", ref=ifc["name"]))


@bp.post("/ifcs/<ref>/baselines")
def create_baseline(ref):
    f = request.form
    b = _run(svc.create_baseline, ref, f.get("name", "").strip(), derived_from=f.get("from", type=int))
    return redirect(url_for("ui.baseline", bid=b["id"]), 303)


@bp.post("/ifcs/<ref>/hscm")
def import_hscm(ref):
    f = request.form
    text = f.get("csv", "")
    upload = request.files.get("file")
    if upload and upload.filename:
        text = upload.read().decode("utf-8-sig")
    out = _run(svc.import_hscm, ref, f.get("name", "").strip(), svc.hscm_rows(text),
               f.get("source_ref") or None, approve=bool(f.get("approve")))
    return redirect(url_for("ui.baseline", bid=out["baseline"]["id"]), 303)


@bp.post("/baselines/<int:bid>/entries")
def set_entry(bid):
    _run(svc.set_baseline_entry, bid, request.form.get("ci"), request.form.get("version"))
    return _back(url_for("ui.baseline", bid=bid))


@bp.post("/baselines/<int:bid>/entries/<ci>/remove")
def remove_entry(bid, ci):
    _run(svc.remove_baseline_entry, bid, ci)
    return _back(url_for("ui.baseline", bid=bid))


@bp.post("/baselines/<int:bid>/refresh")
def refresh_baseline(bid):
    _run(svc.refresh_baseline, bid)
    return _back(url_for("ui.baseline", bid=bid))


@bp.post("/baselines/<int:bid>/approve")
def approve_baseline(bid):
    _run(svc.approve_baseline, bid)
    return _back(url_for("ui.baseline", bid=bid))


@bp.post("/baselines/<int:bid>/branch")
def branch_baseline(bid):
    b = _run(svc.clone_baseline, bid, request.form.get("name", "").strip())
    return redirect(url_for("ui.baseline", bid=b["id"]), 303)


@bp.post("/baselines/<int:bid>/discard")
def discard_baseline(bid):
    out = _run(svc.delete_baseline, bid)
    return redirect(url_for("ui.ifc", ref=out["ifc"]), 303)

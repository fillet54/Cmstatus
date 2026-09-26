"""HTTP API (mounted at /api). Thin: parse the request, call service, return JSON.

CI and IFC path segments accept an id or a name (e.g. /api/cis/NAV-SW).
Each mutating request runs in one transaction.
"""
import functools

from flask import Blueprint, current_app, jsonify, request

from . import service as svc, tickets
from .db import get_db

bp = Blueprint("api", __name__)


def body():
    data = request.get_json(silent=True)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise svc.CMError("expected a JSON object")
    return data


def pick(data, *keys):
    return {k: data[k] for k in keys if k in data}


def release_source(ci):
    """The configured release source a CI syncs from (None if it isn't configured)."""
    return (current_app.config["RELEASE_SOURCES"] or {}).get(ci["release_source"])


def tx(fn):
    """Run the view in a transaction: commit on success, roll back on any error."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with get_db():
            return fn(get_db(), *args, **kwargs)
    return wrapper


def created(obj):
    return jsonify(svc.to_dict(obj) if not isinstance(obj, dict) else obj), 201


# ----------------------------------------------------------------------------- index

@bp.get("/")
def index():
    rules = sorted((r.rule, sorted(r.methods - {"HEAD", "OPTIONS"})) for r in current_app.url_map.iter_rules()
                   if r.rule.startswith("/api"))
    return jsonify([{"path": p, "methods": m} for p, m in rules])


# ----------------------------------------------------------------------------- release sources

@bp.get("/release-sources")
def list_release_sources():
    return jsonify([{"name": n, "type": type(src).__name__}
                    for n, src in (current_app.config["RELEASE_SOURCES"] or {}).items()])


# ----------------------------------------------------------------------------- CIs & CSCs

@bp.get("/cis")
@tx
def list_cis(conn):
    managed = request.args.get("managed")
    return jsonify(svc.to_dicts(svc.list_cis(
        conn, request.args.get("type"), None if managed is None else managed in ("1", "true"))))


@bp.post("/cis")
@tx
def create_ci(conn):
    d = body()
    return created(svc.create_ci(conn, d.get("name"), **pick(
        d, "type", "kind", "managed", "release_source", "source_params", "require_tested", "description",
        "attributes")))


@bp.get("/cis/<ref>")
@tx
def get_ci(conn, ref):
    return jsonify(svc.ci_detail(conn, ref))


@bp.patch("/cis/<ref>")
@tx
def update_ci(conn, ref):
    return jsonify(svc.to_dict(svc.update_ci(conn, ref, **body())))


@bp.post("/cis/<ref>/cscs")
@tx
def add_csc(conn, ref):
    d = body()
    return created(svc.add_csc(conn, ref, d.get("name"), **pick(d, "jira_project", "affected_product", "team")))


@bp.get("/cscs/lookup")
@tx
def lookup_csc(conn):
    return jsonify(svc.to_dict(svc.find_csc(conn, request.args.get("project"), request.args.get("product"))))


@bp.post("/cis/<ref>/sync")
@tx
def sync_ci(conn, ref):
    """Reconcile the CI's releases with its release source. {dry_run: true} reports without changing anything."""
    ci = svc.get_ci(conn, ref)
    return jsonify(svc.sync_ci(conn, release_source(ci), ci["id"], bool(body().get("dry_run"))))


@bp.get("/cis/<ref>/attention")
@tx
def ci_attention(conn, ref):
    return jsonify(svc.ci_attention(conn, ref))


@bp.post("/cis/<ref>/releases")
@tx
def create_release(conn, ref):
    """A release entered by hand: {name?, kind, target_date?, parent?, base_version?, reason?, builds?}."""
    d = body()
    return created(svc.create_release(conn, ref, **pick(
        d, "name", "kind", "target_date", "parent", "base_version", "reason", "builds")))


@bp.get("/cis/<ref>/releases")
@tx
def list_releases(conn, ref):
    return jsonify(svc.list_releases(conn, ref))


# ----------------------------------------------------------------------------- releases & versions

@bp.get("/releases/<int:rid>")
@tx
def get_release(conn, rid):
    return jsonify(svc.release_detail(conn, rid))


@bp.patch("/releases/<int:rid>")
@tx
def update_release(conn, rid):
    """{name, target_date, reason, parent, base_version} (pins them on a synced release), {released_at, note}
    (a correction), {unpin: [field, ...]} (hand fields back to the source)."""
    d = body()
    return jsonify(svc.update_release(conn, rid, **pick(
        d, "name", "target_date", "reason", "parent", "base_version", "released_at", "note", "unpin")))


@bp.post("/releases/<int:rid>/versions")
@tx
def add_version(conn, rid):
    d = body()
    return created(svc.add_version(conn, rid, d.get("name"), d.get("planned_date")))


@bp.post("/releases/<int:rid>/remap")
@tx
def remap_release(conn, rid):
    """{to: release}: this (missing) release is what the source now calls ``to``; ``to`` is folded into it."""
    return jsonify(svc.remap_release(conn, rid, body().get("to")))


@bp.post("/releases/<int:rid>/detach")
@tx
def detach_release(conn, rid):
    return jsonify(svc.detach_release(conn, rid))


@bp.post("/releases/<int:rid>/cancel")
@tx
def cancel_release(conn, rid):
    return jsonify(svc.cancel_release(conn, rid, body().get("note")))


@bp.get("/versions/<int:vid>")
@tx
def get_version(conn, vid):
    out = svc.to_dict(svc.get_version(conn, vid))
    out["manifest"] = svc.manifest(conn, vid)
    return jsonify(out)


@bp.patch("/versions/<int:vid>")
@tx
def update_version(conn, vid):
    """{status, built_at?} moves it along; {built_at, note} corrects the build date; {name, planned_date} pin
    those on a synced build; {unpin: [...]} hands them back to the source."""
    return jsonify(svc.to_dict(svc.update_version(conn, vid, **pick(
        body(), "status", "artifact_ref", "built_at", "planned_date", "name", "note", "unpin"))))


@bp.post("/versions/<int:vid>/release")
@tx
def release_version(conn, vid):
    """{released_at?}: backdate the release (default now)."""
    return jsonify(svc.release_version(conn, vid, body().get("released_at")))


@bp.post("/versions/<int:vid>/remap")
@tx
def remap_version(conn, vid):
    return jsonify(svc.to_dict(svc.remap_version(conn, vid, body().get("to"))))


@bp.post("/versions/<int:vid>/detach")
@tx
def detach_version(conn, vid):
    return jsonify(svc.to_dict(svc.detach_version(conn, vid)))


@bp.put("/versions/<int:vid>/manifest")
@tx
def set_manifest(conn, vid):
    return jsonify(svc.set_manifest(conn, vid, body().get("children", [])))


@bp.get("/versions/<int:vid>/lineage")
@tx
def get_lineage(conn, vid):
    return jsonify(svc.lineage(conn, vid))


@bp.put("/versions/<int:vid>/parents")
@tx
def set_parents(conn, vid):
    """{parents: [version name or id, ...]} sets them by hand (a merge); DELETE reverts to automatic."""
    d = body()
    if "parents" not in d:
        raise svc.CMError("'parents' is required")
    return jsonify(svc.set_version_parents(conn, vid, d["parents"]))


@bp.delete("/versions/<int:vid>/parents")
@tx
def reset_parents(conn, vid):
    return jsonify(svc.set_version_parents(conn, vid, None))


@bp.get("/cis/<ref>/versions")
@tx
def list_version_range(conn, ref):
    """?to=<version>[&from=<version>]: the from..to range over the lineage DAG."""
    to = request.args.get("to")
    if not to:
        raise svc.CMError("'to' is required")
    return jsonify(svc.to_dicts(svc.version_range(conn, ref, to, request.args.get("from"))))


@bp.get("/versions/<int:vid>/where-used")
@tx
def where_used(conn, vid):
    return jsonify(svc.where_used(conn, vid))


# ----------------------------------------------------------------------------- IFCs & baselines

@bp.get("/ifcs")
@tx
def list_ifcs(conn):
    return jsonify(svc.to_dicts(svc.list_ifcs(conn)))


@bp.post("/ifcs")
@tx
def create_ifc(conn):
    d = body()
    return created(svc.create_ifc(conn, d.get("name"), d.get("parent"), d.get("description")))


@bp.get("/ifcs/<ref>")
@tx
def get_ifc(conn, ref):
    return jsonify(svc.ifc_detail(conn, ref))


@bp.patch("/ifcs/<ref>")
@tx
def update_ifc(conn, ref):
    d = body()
    if not {"parent", "description"} & d.keys():
        raise svc.CMError("only 'parent' and 'description' can be changed")
    return jsonify(svc.to_dict(svc.update_ifc(conn, ref, **{k: d[k] for k in ("parent", "description") if k in d})))


@bp.post("/ifcs/<ref>/baselines")
@tx
def create_baseline(conn, ref):
    """{name, from?: baseline id to derive from (copies its entries unless entries are given), entries?}"""
    d = body()
    return created(svc.create_baseline(conn, ref, d.get("name"), d.get("entries"),
                                       source_ref=d.get("source_ref"), derived_from=d.get("from")))


@bp.post("/ifcs/<ref>/hscm")
@tx
def import_hscm(conn, ref):
    """JSON {name, source_ref?, approve?, rows: [{ci, version, type?}]}
    or text/csv with columns ci,version[,type] and ?name=&source_ref=&approve= query args."""
    if request.mimetype == "text/csv":
        rows = svc.hscm_rows(request.get_data(as_text=True))
        opts = request.args
        approve = opts.get("approve", "true").lower() not in ("0", "false", "no")
    else:
        opts = body()
        rows = opts.get("rows", [])
        approve = bool(opts.get("approve", True))
    return created(svc.import_hscm(conn, ref, opts.get("name"), rows, opts.get("source_ref"), approve))


@bp.get("/baselines/<int:bid>")
@tx
def get_baseline(conn, bid):
    return jsonify(svc.baseline_detail(conn, bid))


@bp.put("/baselines/<int:bid>/entries")
@tx
def set_entries(conn, bid):
    return jsonify(svc.set_baseline_entries(conn, bid, body().get("entries", [])))


@bp.put("/baselines/<int:bid>/entries/<ci>")
@tx
def set_entry(conn, bid, ci):
    return jsonify(svc.set_baseline_entry(conn, bid, ci, body().get("version")))


@bp.delete("/baselines/<int:bid>/entries/<ci>")
@tx
def remove_entry(conn, bid, ci):
    return jsonify(svc.remove_baseline_entry(conn, bid, ci))


@bp.post("/baselines/<int:bid>/refresh")
@tx
def refresh_baseline(conn, bid):
    return jsonify(svc.refresh_baseline(conn, bid))


@bp.delete("/baselines/<int:bid>")
@tx
def delete_baseline(conn, bid):
    return jsonify(svc.delete_baseline(conn, bid))


@bp.post("/baselines/<int:bid>/clone")
@tx
def clone_baseline(conn, bid):
    return created(svc.clone_baseline(conn, bid, body().get("name")))


@bp.post("/baselines/<int:bid>/approve")
@tx
def approve_baseline(conn, bid):
    return jsonify(svc.approve_baseline(conn, bid))


@bp.get("/baselines/<int:a>/diff/<int:b>")
@tx
def diff_baselines(conn, a, b):
    return jsonify(svc.diff_baselines(conn, a, b))


# ----------------------------------------------------------------------------- tickets / work items
# Tickets are read live from the configured TicketSource on every call; nothing is stored here.

def ticket_source(default=None):
    try:
        return tickets.pick_source(current_app.config["TICKET_SOURCES"], request.args.get("source") or default)
    except KeyError as e:
        raise svc.CMError(e.args[0]) from None


def versions_arg(value):
    """A comma-separated string of version refs."""
    return [v.strip() for v in str(value).split(",") if v.strip()] if value else None


@bp.get("/ticket-states")
def ticket_states():
    """The workflow states a TicketSource may report, in order."""
    return jsonify([{"state": k, "label": v} for k, v in tickets.STATES.items()])


@bp.get("/cis/<ref>/work")
@tx
def work(conn, ref):
    """?to=&from= (range over the lineage DAG) or ?versions=a,b [&source=]: parent -> CSC -> CSC tickets."""
    a = request.args
    return jsonify(svc.work_report(conn, ticket_source(), ref, a.get("to"), a.get("from"),
                                   versions_arg(a.get("versions"))))


@bp.get("/tickets/<key>")
@tx
def get_ticket(conn, key):
    return jsonify(svc.ticket_detail(conn, ticket_source(), key))


# ----------------------------------------------------------------------------- backlogs
# Shared, ranked backlogs of top-level tickets. Order and membership live here; tickets are read live.

def backlog_source(conn, ref):
    return ticket_source(svc.get_backlog(conn, ref)["source"])


@bp.get("/backlogs")
@tx
def list_backlogs(conn):
    return jsonify(svc.list_backlogs(conn, request.args.get("ci")))


@bp.post("/backlogs")
@tx
def create_backlog(conn):
    d = body()
    return created(svc.create_backlog(conn, d.get("name"), **pick(d, "description", "teams", "cis", "source")))


@bp.get("/backlogs/<ref>")
@tx
def get_backlog(conn, ref):
    """The backlog in rank order, each ticket read live from the source."""
    return jsonify(svc.backlog_view(conn, backlog_source(conn, ref), ref))


@bp.patch("/backlogs/<ref>")
@tx
def update_backlog(conn, ref):
    return jsonify(svc.update_backlog(conn, ref, **body()))


@bp.post("/backlogs/<ref>/items")
@tx
def add_backlog_item(conn, ref):
    """{key, position?: "bottom" | "top"}"""
    d = body()
    return created(svc.add_backlog_item(conn, backlog_source(conn, ref), ref, d.get("key"),
                                        d.get("position") or "bottom"))


@bp.delete("/backlogs/<ref>/items/<key>")
@tx
def remove_backlog_item(conn, ref, key):
    return jsonify(svc.remove_backlog_item(conn, ref, key))


@bp.post("/backlogs/<ref>/items/<key>/move")
@tx
def move_backlog_item(conn, ref, key):
    """{after?: key above, before?: key below} -> {key, rank}. Only the moved item is re-ranked."""
    d = body()
    return jsonify(svc.move_backlog_item(conn, ref, key, d.get("after"), d.get("before")))


@bp.post("/backlogs/<ref>/pull")
@tx
def pull_backlog(conn, ref):
    """Append the source's top-level tickets for this backlog that aren't on it yet."""
    return jsonify(svc.pull_backlog(conn, backlog_source(conn, ref), ref))


@bp.post("/backlogs/<ref>/rebalance")
@tx
def rebalance_backlog(conn, ref):
    return jsonify(svc.rebalance_backlog(conn, ref))


# ----------------------------------------------------------------------------- events

@bp.get("/events")
@tx
def list_events(conn):
    a = request.args
    return jsonify(svc.to_dicts(svc.list_events(
        conn, a.get("entity"), a.get("entity_id", type=int), a.get("limit", 200, type=int))))

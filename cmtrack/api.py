"""HTTP API (mounted at /api). Thin: parse the request, call service, return JSON.

CI and IFC path segments accept an id or a name (e.g. /api/cis/NAV-SW).
Each mutating request runs in one transaction.
"""
import csv
import functools
import io

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


def policy_dir():
    return current_app.config["POLICY_DIR"]


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


# ----------------------------------------------------------------------------- policies

@bp.get("/policies")
@tx
def list_policies(conn):
    return jsonify(svc.to_dicts(svc.list_policies(conn)))


@bp.post("/policies")
@tx
def create_policy(conn):
    d = body()
    return created(svc.create_policy(conn, d.get("name"), d.get("type"), d.get("params"), policy_dir()))


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
        d, "type", "kind", "managed", "policy", "description", "attributes")))


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


@bp.post("/cis/<ref>/plan")
@tx
def plan_ci(conn, ref):
    d = body()
    return jsonify(svc.plan_ci(conn, ref, d.get("start"), d.get("end"), policy_dir()))


@bp.get("/cis/<ref>/releases")
@tx
def list_releases(conn, ref):
    return jsonify(svc.list_releases(conn, ref))


# ----------------------------------------------------------------------------- releases & versions

@bp.get("/releases/<int:rid>")
@tx
def get_release(conn, rid):
    return jsonify(svc.release_detail(conn, rid))


@bp.post("/releases/<int:rid>/versions")
@tx
def add_version(conn, rid):
    d = body()
    return created(svc.add_version(conn, rid, d.get("name"), d.get("planned_date"), policy_dir()))


@bp.post("/releases/<int:rid>/spawn")
@tx
def spawn_release(conn, rid):
    """201 when a new release is spawned; 200 with the existing one if this change request already has one."""
    d = body()
    out = svc.spawn_release(conn, rid, d.get("kind"), base_dir=policy_dir(),
                            **pick(d, "reason", "base_version", "target_date"))
    return jsonify(out), 201 if out["spawned"] else 200


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
    return jsonify(svc.to_dict(svc.update_version(conn, vid, **pick(body(), "status", "artifact_ref", "built_at"))))


@bp.post("/versions/<int:vid>/release")
@tx
def release_version(conn, vid):
    return jsonify(svc.release_version(conn, vid, policy_dir()))


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
    if "parent" not in d:
        raise svc.CMError("only 'parent' can be changed")
    return jsonify(svc.to_dict(svc.set_ifc_parent(conn, ref, d["parent"])))


@bp.post("/ifcs/<ref>/baselines")
@tx
def create_baseline(conn, ref):
    d = body()
    return created(svc.create_baseline(conn, ref, d.get("name"), d.get("entries", []),
                                       source_ref=d.get("source_ref")))


@bp.post("/ifcs/<ref>/hscm")
@tx
def import_hscm(conn, ref):
    """JSON {name, source_ref?, approve?, rows: [{ci, version, type?}]}
    or text/csv with columns ci,version[,type] and ?name=&source_ref=&approve= query args."""
    if request.mimetype == "text/csv":
        rows = list(csv.DictReader(io.StringIO(request.get_data(as_text=True))))
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

def ticket_source():
    try:
        return tickets.pick_source(current_app.config["TICKET_SOURCES"], request.args.get("source"))
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


# ----------------------------------------------------------------------------- events

@bp.get("/events")
@tx
def list_events(conn):
    a = request.args
    return jsonify(svc.to_dicts(svc.list_events(
        conn, a.get("entity"), a.get("entity_id", type=int), a.get("limit", 200, type=int))))

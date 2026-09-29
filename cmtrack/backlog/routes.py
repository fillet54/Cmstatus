"""Backlog routes: one blueprint with the JSON API (/api/backlogs...) and the pages (/backlogs...).

JSON API (each request runs in one transaction; see cmtrack/api.py for tx / body / pick / created):
    GET    /api/backlogs[?ci=]                       list (optionally only those related to a CI)
    POST   /api/backlogs                             {name, description?, teams?, cis?, source?, store?, store_params?}
    GET    /api/backlogs/<b>                         items in rank order, tickets read live
    PATCH  /api/backlogs/<b>                         {name?, description?, teams?, cis?, source?, store?, store_params?}
                                                     (a new store/params moves every item there, ranks kept)
    POST   /api/backlogs/<b>/items                   {key, position?: bottom|top}
    DELETE /api/backlogs/<b>/items/<key>
    POST   /api/backlogs/<b>/items/<key>/move        {after?, before?} -> {key, rank}; 409 if the neighbours moved
    POST   /api/backlogs/<b>/pull                    append the source's top-level tickets not already here
    POST   /api/backlogs/<b>/rebalance               re-space every rank, order unchanged

Pages (htmx: actions answer with the refreshed item list, templates/backlog/_items.html):
    GET  /backlogs                    list + "New backlog" form          POST /backlogs  create
    GET  /backlogs/<b>                the ranked list (drag and drop: static/backlog.js calls the move API)
    POST /backlogs/<b>/pull | /add | /items/<key>/remove | /rebalance
"""
from flask import Blueprint, current_app, jsonify, redirect, render_template, request, url_for

from .. import service as core
from ..api import body, created, pick, ticket_source as api_ticket_source, tx
from ..db import get_db
from ..views import live, page, ticket_source
from . import service as svc

bp = Blueprint("backlog", __name__, template_folder="templates", static_folder="static",
               static_url_path="/backlog-static")


def stores():
    """The configured rank stores by name (BACKLOG_STORES); "sqlite" is always there."""
    return current_app.config.get("BACKLOG_STORES") or {}


def default_store():
    return current_app.config.get("BACKLOG_DEFAULT_STORE") or "sqlite"


# ----------------------------------------------------------------------------- JSON API

def backlog_source(conn, ref):
    """The ticket source a backlog reads from (its own ``source``, else the default one)."""
    return api_ticket_source(svc.get_backlog(conn, ref)["source"])


@bp.get("/api/backlogs")
@tx
def api_list(conn):
    return jsonify(svc.list_backlogs(conn, request.args.get("ci")))


@bp.post("/api/backlogs")
@tx
def api_create(conn):
    d = body()
    return created(svc.create_backlog(conn, d.get("name"), store=d.get("store") or default_store(), stores=stores(),
                                      **pick(d, "description", "teams", "cis", "source", "store_params")))


@bp.get("/api/backlogs/<ref>")
@tx
def api_get(conn, ref):
    """The backlog in rank order, each ticket read live from the source."""
    return jsonify(svc.backlog_view(conn, backlog_source(conn, ref), ref, stores()))


@bp.patch("/api/backlogs/<ref>")
@tx
def api_update(conn, ref):
    return jsonify(svc.update_backlog(conn, ref, stores(), **body()))


@bp.post("/api/backlogs/<ref>/items")
@tx
def api_add_item(conn, ref):
    d = body()
    return created(svc.add_backlog_item(conn, backlog_source(conn, ref), ref, d.get("key"),
                                        d.get("position", "bottom"), stores()))


@bp.delete("/api/backlogs/<ref>/items/<key>")
@tx
def api_remove_item(conn, ref, key):
    return jsonify(svc.remove_backlog_item(conn, ref, key, stores()))


@bp.post("/api/backlogs/<ref>/items/<key>/move")
@tx
def api_move_item(conn, ref, key):
    """The drag-and-drop callback: {after: <key above>, before: <key below>} (one is enough)."""
    d = body()
    return jsonify(svc.move_backlog_item(conn, ref, key, d.get("after"), d.get("before"), stores()))


@bp.post("/api/backlogs/<ref>/pull")
@tx
def api_pull(conn, ref):
    """Append the source's top-level tickets for this backlog that aren't on it yet."""
    return jsonify(svc.pull_backlog(conn, backlog_source(conn, ref), ref, stores()))


@bp.post("/api/backlogs/<ref>/rebalance")
@tx
def api_rebalance(conn, ref):
    return jsonify(svc.rebalance_backlog(conn, ref, stores()))


# ----------------------------------------------------------------------------- pages

@bp.get("/backlogs")
def backlogs():
    conn = get_db()
    return _list_page(conn)


def _list_page(conn, error=None, form=None):
    return render_template("backlog/list.html", backlogs=svc.list_backlogs(conn), error=error, form=form or {},
                           cis=core.to_dicts(core.list_cis(conn, managed=True)),
                           store_names=sorted(svc.all_stores(stores())), default_store=default_store())


@bp.post("/backlogs")
def create_backlog():
    conn = get_db()
    f = request.form
    store = f.get("store") or default_store()
    params = {"rank_field": f.get("rank_field", "").strip(), "scope": f.get("scope", "").strip()} \
        if store != "sqlite" else {}
    try:
        with conn:
            b = svc.create_backlog(conn, f.get("name"), f.get("description") or None, f.get("teams"),
                                   f.getlist("cis"), store=store, store_params=params, stores=stores())
    except core.CMError as e:
        # htmx only swaps 2xx responses, so the re-rendered form with its error comes back as 200 there
        return _list_page(conn, e.message, f), 200 if "HX-Request" in request.headers else e.status
    target = url_for("backlog.backlog", ref=b["name"])
    if "HX-Request" in request.headers:
        return "", 204, {"HX-Redirect": target}
    return redirect(target, 303)


def _fragment(conn, ref, message=None, error=None):
    b = svc.backlog_summary(conn, ref, stores())
    view, source_error = live(svc.backlog_view, conn, ticket_source(b["source"]), ref, stores())
    return render_template("backlog/_items.html", b=view or b, source_error=source_error,
                           message=message, error=error)


@bp.get("/backlogs/<ref>")
def backlog(ref):
    conn = get_db()
    b = svc.backlog_summary(conn, ref, stores())
    view, source_error = live(svc.backlog_view, conn, ticket_source(b["source"]), ref, stores())
    return page("backlog/page.html", "backlog/_items.html", b=view or b, source_error=source_error,
                message=None, error=None)


def _action(ref, action):
    """Run a backlog change for an htmx form and answer with the refreshed item list plus a message."""
    conn = get_db()
    b = svc.backlog_summary(conn, ref, stores())
    try:
        with conn:
            message = action(conn, ticket_source(b["source"]), b)
    except core.CMError as e:
        return _fragment(conn, ref, error=e.message)
    return _fragment(conn, ref, message=message)


@bp.post("/backlogs/<ref>/pull")
def pull(ref):
    def run(conn, source, b):
        out = svc.pull_backlog(conn, source, b["id"], stores())
        return (f"Added {len(out['added'])} from {source.name}: {', '.join(out['added'])}" if out["added"]
                else f"Nothing new from {source.name} ({out['offered']} offered, all already here)")
    return _action(ref, run)


@bp.post("/backlogs/<ref>/add")
def add(ref):
    key, position = request.form.get("key", "").strip().upper(), request.form.get("position", "bottom")
    def run(conn, source, b):
        svc.add_backlog_item(conn, source, b["id"], key, position, stores())
        return f"Added {key} at the {position}"
    return _action(ref, run)


@bp.post("/backlogs/<ref>/items/<key>/remove")
def remove(ref, key):
    def run(conn, source, b):
        svc.remove_backlog_item(conn, b["id"], key, stores())
        return f"Removed {key}"
    return _action(ref, run)


@bp.post("/backlogs/<ref>/rebalance")
def rebalance(ref):
    def run(conn, source, b):
        out = svc.rebalance_backlog(conn, b["id"], stores())
        return f"Re-spaced {out['items']} ranks (longest is now {out['max_rank_length']} characters)"
    return _action(ref, run)

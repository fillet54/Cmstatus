"""Backlog routes: one blueprint with the JSON API (/api/backlogs...) and the pages (/backlogs...).

JSON API (each request runs in one transaction; see cmtrack/api.py for tx / body / pick / created):
    GET    /api/backlogs[?ci=]                       list (optionally only those related to a CI)
    POST   /api/backlogs                             {name, description?, teams?, cis?, source?}
    GET    /api/backlogs/<b>                         items in rank order, tickets read live
    PATCH  /api/backlogs/<b>                         {name?, description?, teams?, cis?, source?}
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
from flask import Blueprint, jsonify, redirect, render_template, request, url_for

from .. import service as core
from ..api import body, created, pick, ticket_source as api_ticket_source, tx
from ..db import get_db
from ..views import live, page, ticket_source
from . import service as svc

bp = Blueprint("backlog", __name__, template_folder="templates", static_folder="static",
               static_url_path="/backlog-static")


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
    return created(svc.create_backlog(conn, d.get("name"), **pick(d, "description", "teams", "cis", "source")))


@bp.get("/api/backlogs/<ref>")
@tx
def api_get(conn, ref):
    """The backlog in rank order, each ticket read live from the source."""
    return jsonify(svc.backlog_view(conn, backlog_source(conn, ref), ref))


@bp.patch("/api/backlogs/<ref>")
@tx
def api_update(conn, ref):
    return jsonify(svc.update_backlog(conn, ref, **body()))


@bp.post("/api/backlogs/<ref>/items")
@tx
def api_add_item(conn, ref):
    d = body()
    return created(svc.add_backlog_item(conn, backlog_source(conn, ref), ref, d.get("key"),
                                        d.get("position", "bottom")))


@bp.delete("/api/backlogs/<ref>/items/<key>")
@tx
def api_remove_item(conn, ref, key):
    return jsonify(svc.remove_backlog_item(conn, ref, key))


@bp.post("/api/backlogs/<ref>/items/<key>/move")
@tx
def api_move_item(conn, ref, key):
    """The drag-and-drop callback: {after: <key above>, before: <key below>} (one is enough)."""
    d = body()
    return jsonify(svc.move_backlog_item(conn, ref, key, d.get("after"), d.get("before")))


@bp.post("/api/backlogs/<ref>/pull")
@tx
def api_pull(conn, ref):
    """Append the source's top-level tickets for this backlog that aren't on it yet."""
    return jsonify(svc.pull_backlog(conn, backlog_source(conn, ref), ref))


@bp.post("/api/backlogs/<ref>/rebalance")
@tx
def api_rebalance(conn, ref):
    return jsonify(svc.rebalance_backlog(conn, ref))


# ----------------------------------------------------------------------------- pages

@bp.get("/backlogs")
def backlogs():
    conn = get_db()
    return render_template("backlog/list.html", backlogs=svc.list_backlogs(conn),
                           cis=core.to_dicts(core.list_cis(conn, managed=True)), error=None, form={})


@bp.post("/backlogs")
def create_backlog():
    conn = get_db()
    f = request.form
    try:
        with conn:
            b = svc.create_backlog(conn, f.get("name"), f.get("description") or None, f.get("teams"),
                                   f.getlist("cis"))
    except core.CMError as e:
        # htmx only swaps 2xx responses, so the re-rendered form with its error comes back as 200 there
        return render_template("backlog/list.html", backlogs=svc.list_backlogs(conn), error=e.message, form=f,
                               cis=core.to_dicts(core.list_cis(conn, managed=True))), \
            200 if "HX-Request" in request.headers else e.status
    target = url_for("backlog.backlog", ref=b["name"])
    if "HX-Request" in request.headers:
        return "", 204, {"HX-Redirect": target}
    return redirect(target, 303)


def _fragment(conn, ref, message=None, error=None):
    b = svc.backlog_summary(conn, ref)
    view, source_error = live(svc.backlog_view, conn, ticket_source(b["source"]), ref)
    return render_template("backlog/_items.html", b=view or b, source_error=source_error,
                           message=message, error=error)


@bp.get("/backlogs/<ref>")
def backlog(ref):
    conn = get_db()
    b = svc.backlog_summary(conn, ref)
    view, source_error = live(svc.backlog_view, conn, ticket_source(b["source"]), ref)
    return page("backlog/page.html", "backlog/_items.html", b=view or b, source_error=source_error,
                message=None, error=None)


def _action(ref, action):
    """Run a backlog change for an htmx form and answer with the refreshed item list plus a message."""
    conn = get_db()
    b = svc.backlog_summary(conn, ref)
    try:
        with conn:
            message = action(conn, ticket_source(b["source"]), b)
    except core.CMError as e:
        return _fragment(conn, ref, error=e.message)
    return _fragment(conn, ref, message=message)


@bp.post("/backlogs/<ref>/pull")
def pull(ref):
    def run(conn, source, b):
        out = svc.pull_backlog(conn, source, b["id"])
        return (f"Added {len(out['added'])} from {source.name}: {', '.join(out['added'])}" if out["added"]
                else f"Nothing new from {source.name} ({out['offered']} offered, all already here)")
    return _action(ref, run)


@bp.post("/backlogs/<ref>/add")
def add(ref):
    key, position = request.form.get("key", "").strip().upper(), request.form.get("position", "bottom")
    def run(conn, source, b):
        svc.add_backlog_item(conn, source, b["id"], key, position)
        return f"Added {key} at the {position}"
    return _action(ref, run)


@bp.post("/backlogs/<ref>/items/<key>/remove")
def remove(ref, key):
    def run(conn, source, b):
        svc.remove_backlog_item(conn, b["id"], key)
        return f"Removed {key}"
    return _action(ref, run)


@bp.post("/backlogs/<ref>/rebalance")
def rebalance(ref):
    def run(conn, source, b):
        out = svc.rebalance_backlog(conn, b["id"])
        return f"Re-spaced {out['items']} ranks (longest is now {out['max_rank_length']} characters)"
    return _action(ref, run)

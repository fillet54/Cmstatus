"""Backlog domain logic: shared, ranked backlogs of top-level tickets.

A backlog is shared by several teams and related to CIs. cmtrack owns only its membership and order: each
item is a ticket key plus a lexorank string (rank.py), and a move rewrites just the moved item's rank. The
tickets themselves (summary, state, affected CIs) are read live from the ticket source on every view.

Like the rest of cmtrack's service layer, every function takes an open connection and does NOT commit; the
caller owns the transaction. Backlog references accept an id or a name.

What it uses from the core (cmtrack/service.py, cmtrack/tickets.py):
    CMError / NotFound / Conflict    errors that become 400 / 404 / 409
    _by_ref, _one, log, to_dict(s)   row lookup, the event log, row -> dict
    get_ci                           the ``ci`` table (a backlog relates to CIs)
    _ask, _Resolver, _progress       calling the ticket source, placing tickets, state counts
    tickets.TicketRecord, ERROR      a placeholder for keys the source no longer knows
The ticket source's ``get_tickets(keys)`` is required; ``top_level_tickets(backlog, cis)`` only for pull.
"""
import json
import sqlite3

from .. import tickets
from ..service import (CMError, Conflict, NotFound, _ask, _by_ref, _one, _progress, _Resolver, get_ci, log,
                       to_dict, to_dicts)
from . import rank


def get_backlog(conn, ref):
    return _by_ref(conn, "backlog", ref, "backlog")


def _backlog_cis(conn, backlog_id):
    return to_dicts(conn.execute("SELECT ci.* FROM backlog_ci b JOIN ci ON ci.id = b.ci_id "
                                 "WHERE b.backlog_id = ? ORDER BY ci.name", (backlog_id,)))


def _set_backlog_cis(conn, backlog_id, cis):
    ids = [get_ci(conn, c)["id"] for c in cis or []]
    conn.execute("DELETE FROM backlog_ci WHERE backlog_id = ?", (backlog_id,))
    conn.executemany("INSERT OR IGNORE INTO backlog_ci VALUES (?, ?)", [(backlog_id, i) for i in ids])


def _decode(d):
    """A backlog row as a dict, with its JSON ``teams`` column as a list."""
    if isinstance(d.get("teams"), str):
        d["teams"] = json.loads(d["teams"])
    return d


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
    return [_decode(d) for d in to_dicts(conn.execute(sql + " ORDER BY b.name", args))]


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
    b = _decode(to_dict(get_backlog(conn, ref)))
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

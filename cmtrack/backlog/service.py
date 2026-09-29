"""Backlog domain logic: shared, ranked backlogs of top-level tickets.

A backlog (name, teams, related CIs, ticket source, store) is a row in cmtrack's database. Its items are
(ticket key, lexorank) pairs (rank.py) kept by the backlog's rank store (stores.py): the backlog_item table,
or a Jira custom field. A move rewrites only the moved item's rank. Ticket data (summary, state, affected
CIs) is read live from the ticket source on every view.

Functions take an open connection and don't commit (the caller owns the transaction). They run inside the
Flask app, whose config holds the stores: BACKLOG_STORES ({name: store}, beside the built-in "sqlite") and
BACKLOG_DEFAULT_STORE. Backlog references accept an id or a name.
"""
import json
import sqlite3

from flask import current_app

from .. import tickets
from ..service import (CMError, Conflict, NotFound, SourceError, _ask, _by_ref, _progress, _Resolver, get_ci, log,
                       to_dict, to_dicts)
from . import rank
from .stores import SqliteStore, StoreError


def stores():
    return {"sqlite": SqliteStore(), **(current_app.config.get("BACKLOG_STORES") or {})}


def _store(b):
    if b["store"] not in stores():
        raise CMError(f"backlog {b['name']!r} keeps its order in store {b['store']!r}, which isn't configured "
                      f"(CMTRACK_BACKLOG_STORES)")
    return stores()[b["store"]]


def _io(fn, *args):
    """Call a store; a failure outside cmtrack (Jira down or refusing) becomes a 502."""
    try:
        return fn(*args)
    except (StoreError, OSError) as e:
        raise SourceError(f"backlog store: {e}") from e


def get_backlog(conn, ref):
    return _by_ref(conn, "backlog", ref, "backlog")


def _decode(d):
    for col in ("teams", "store_params"):
        if isinstance(d.get(col), str):
            d[col] = json.loads(d[col])
    return d


def _teams(teams):
    if isinstance(teams, str):
        teams = teams.split(",")
    if not isinstance(teams or [], list):
        raise CMError("teams must be a list of names")
    return [str(t).strip() for t in teams or [] if str(t).strip()]


def _backlog_cis(conn, backlog_id):
    return to_dicts(conn.execute("SELECT ci.* FROM backlog_ci b JOIN ci ON ci.id = b.ci_id "
                                 "WHERE b.backlog_id = ? ORDER BY ci.name", (backlog_id,)))


def _set_backlog_cis(conn, backlog_id, cis):
    ids = [get_ci(conn, c)["id"] for c in cis or []]
    conn.execute("DELETE FROM backlog_ci WHERE backlog_id = ?", (backlog_id,))
    conn.executemany("INSERT OR IGNORE INTO backlog_ci VALUES (?, ?)", [(backlog_id, i) for i in ids])


def _check_store(conn, name, params, backlog_id=None):
    """(store name, params as JSON), validated. Two backlogs can't share one Jira rank field."""
    name = name or current_app.config.get("BACKLOG_DEFAULT_STORE") or "sqlite"
    if name not in stores():
        raise CMError(f"unknown backlog store {name!r}; configured: {sorted(stores())}")
    try:
        params = stores()[name].check_params(json.loads(params) if isinstance(params, str) else params or {})
    except ValueError as e:
        raise CMError(str(e)) from None
    for other in conn.execute("SELECT name, store_params FROM backlog WHERE store = ? AND id IS NOT ?", (name, backlog_id)):
        if params.get("rank_field") and json.loads(other["store_params"]).get("rank_field") == params["rank_field"]:
            raise Conflict(f"{params['rank_field']} already holds the ranks of backlog {other['name']!r}; "
                           f"each backlog needs its own field")
    return name, json.dumps(params)


def _items(conn, b):
    """(items in rank order, warnings). Ties sort by key; invalid ranks sort last until a rebalance."""
    items = sorted(_io(_store(b).items, conn, b), key=lambda i: (not rank.valid(i.rank), i.rank or "", i.key))
    warnings = [f"{i.key} has no valid rank ({i.rank!r}); it sorts last until you rebalance"
                for i in items if not rank.valid(i.rank)]
    if any(a.rank == z.rank for a, z in zip(items, items[1:]) if rank.valid(a.rank)):
        warnings.append("some items share a rank (e.g. two moves at once); rebalance to separate them")
    return items, warnings


def _find(items, key):
    item = next((i for i in items if i.key == key), None)
    if item is None:
        raise NotFound(f"{key} in this backlog")
    return item


def _append(conn, b, items, keys, top=False):
    """Add keys (in order) at the bottom, or the top; returns their ranks."""
    ranks = [i.rank for i in items if rank.valid(i.rank)]
    edge, pairs = (ranks[0] if top else ranks[-1]) if ranks else None, []
    for key in (reversed(keys) if top else keys):
        edge = rank.between(None, edge) if top else rank.between(edge, None)
        pairs.append((key, edge))
    if pairs:
        _io(_store(b).add, conn, b, pairs)
    return [r for _, r in pairs]


# ----------------------------------------------------------------------------- backlogs

def list_backlogs(conn, ci_ref=None):
    """Backlogs (optionally only those related to a CI). ``item_count`` is None unless the store is sqlite:
    counting the others would mean asking Jira once per backlog."""
    sql = ("SELECT b.*, CASE WHEN b.store = 'sqlite' THEN (SELECT COUNT(*) FROM backlog_item i "
           "WHERE i.backlog_id = b.id) END AS item_count, (SELECT group_concat(ci.name, ', ') FROM backlog_ci x "
           "JOIN ci ON ci.id = x.ci_id WHERE x.backlog_id = b.id) AS ci_names FROM backlog b")
    if ci_ref:
        sql += f" WHERE b.id IN (SELECT backlog_id FROM backlog_ci WHERE ci_id = {int(get_ci(conn, ci_ref)['id'])})"
    return [_decode(d) for d in to_dicts(conn.execute(sql + " ORDER BY b.name"))]


def create_backlog(conn, name, description=None, teams=None, cis=None, source=None, store=None, store_params=None):
    name = (name or "").strip()
    if not name or name.isdigit():
        raise CMError("name is required and can't be only digits (it would read as an id)")
    store, params = _check_store(conn, store, store_params)
    try:
        cur = conn.execute("INSERT INTO backlog (name, description, teams, source, store, store_params) "
                           "VALUES (?, ?, ?, ?, ?, ?)", (name, description, json.dumps(_teams(teams)), source or None,
                                                         store, params))
    except sqlite3.IntegrityError:
        raise Conflict(f"backlog {name!r} already exists") from None
    _set_backlog_cis(conn, cur.lastrowid, cis)
    log(conn, "backlog", cur.lastrowid, "created", name=name, teams=_teams(teams), cis=cis or [], store=store)
    return backlog_summary(conn, cur.lastrowid)


def update_backlog(conn, ref, **fields):
    """Change name/description/teams/cis/source, or move the items to another store (``store`` and/or
    ``store_params``): each is copied with its rank, then removed from the old store."""
    b = backlog_summary(conn, ref)
    unknown = set(fields) - {"name", "description", "teams", "cis", "source", "store", "store_params"}
    if unknown:
        raise CMError(f"cannot update {sorted(unknown)}")
    sets = {k: fields[k] or None if k == "source" else fields[k] for k in ("description", "source") if k in fields}
    if "name" in fields:
        sets["name"] = str(fields["name"] or "").strip()
        if not sets["name"] or sets["name"].isdigit():
            raise CMError("name is required and can't be only digits")
    if "teams" in fields:
        sets["teams"] = json.dumps(_teams(fields["teams"]))
    if "store" in fields or "store_params" in fields:
        name, params = _check_store(conn, fields.get("store", b["store"]), fields.get("store_params", b["store_params"]),
                                    b["id"])
        new = {**b, "store": name, "store_params": json.loads(params)}
        if (name, new["store_params"]) != (b["store"], b["store_params"]):
            items = _items(conn, b)[0]
            _io(_store(new).add, conn, new, [(i.key, i.rank) for i in items])
            for i in items:
                _io(_store(b).remove, conn, b, i.key)
            sets.update(store=name, store_params=params)
    if sets:
        try:
            conn.execute(f"UPDATE backlog SET {', '.join(k + ' = ?' for k in sets)} WHERE id = ?", (*sets.values(), b["id"]))
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
                                   (b["id"],)).fetchone()[0] if b["store"] == "sqlite" else None
    return b


def add_backlog_item(conn, source, ref, key, position="bottom"):
    """Put a top-level ticket on the backlog (checked against the source) at the top or bottom."""
    b = backlog_summary(conn, ref)
    key = str(key or "").strip()
    if not key or position not in ("top", "bottom"):
        raise CMError("give a key, and a position of top or bottom")
    items = _items(conn, b)[0]
    if any(i.key == key for i in items):
        raise Conflict(f"{key} is already in {b['name']}")
    rec = next((r for r in _ask(source, "get_tickets", [key]) if r.key == key), None)
    if rec is None:
        raise NotFound(f"ticket {key!r} not found in {source.name}")
    if rec.parent_key:
        raise CMError(f"{key} is a CSC ticket under {rec.parent_key}; add the top-level ticket instead")
    new = _append(conn, b, items, [key], top=position == "top")
    log(conn, "backlog", b["id"], "item_added", key=key, position=position)
    return {"key": key, "rank": new[0]}


def remove_backlog_item(conn, ref, key):
    b = backlog_summary(conn, ref)
    _find(_items(conn, b)[0], key)
    _io(_store(b).remove, conn, b, key)
    log(conn, "backlog", b["id"], "item_removed", key=key)
    return {"removed": key}


def move_backlog_item(conn, ref, key, after=None, before=None):
    """Re-rank ``key`` between its new neighbours: ``after`` (the item now above it) and/or ``before`` (the item
    now below it); one is enough. Only the moved item's rank changes. 409 if they're no longer in that order."""
    b = backlog_summary(conn, ref)
    if key in (after, before) or not (after or before):
        raise CMError("give 'after' and/or 'before': the keys of the new neighbours, not the item itself")
    items = _items(conn, b)[0]
    item = _find(items, key)
    others = [i for i in items if i.key != key]
    lo = _find(others, after) if after else None
    hi = _find(others, before) if before else None
    if not before:
        hi = next(iter(others[others.index(lo) + 1:]), None)
    if not after:
        lo = others[others.index(hi) - 1] if others.index(hi) else None
    for n in (lo, hi):
        if n and not rank.valid(n.rank):
            raise CMError(f"{n.key} has no valid rank; rebalance the backlog first")
    if lo and hi and lo.rank >= hi.rank:
        why = "share a rank; rebalance the backlog" if lo.rank == hi.rank else "are not in that order any more; reload"
        raise Conflict(f"{lo.key} and {hi.key} {why} and try again")
    new = rank.between(lo and lo.rank, hi and hi.rank)
    _io(_store(b).set_rank, conn, b, key, new)
    log(conn, "backlog", b["id"], "item_moved", key=key, after=after, before=before)
    return {"key": key, "rank": new, "previous_rank": item.rank}


def rebalance_backlog(conn, ref):
    """Re-space every rank evenly, order unchanged: after many drops in one spot, or to repair a Jira field."""
    b = backlog_summary(conn, ref)
    items = _items(conn, b)[0]
    ranks = rank.spread(len(items))
    _io(_store(b).set_ranks, conn, b, {i.key: r for i, r in zip(items, ranks)})
    log(conn, "backlog", b["id"], "rebalanced", items=len(items))
    return {"items": len(items), "max_rank_length": max(map(len, ranks), default=0)}


def pull_backlog(conn, source, ref):
    """Append the top-level tickets the source offers for this backlog that aren't on it yet (source order)."""
    b = backlog_summary(conn, ref)
    items = _items(conn, b)[0]
    offered = _ask(source, "top_level_tickets", b, _backlog_cis(conn, b["id"]))
    have, new = {i.key for i in items}, []
    for rec in offered:
        if not rec.parent_key and rec.key not in have:
            have.add(rec.key)
            new.append(rec.key)
    _append(conn, b, items, new)
    if new:
        log(conn, "backlog", b["id"], "pulled", source=source.name, added=new)
    return {"added": new, "offered": len(offered), "already": len(offered) - len(new)}


def backlog_view(conn, source, ref):
    """The backlog in rank order, each ticket read live from the source (one get_tickets call)."""
    b = backlog_summary(conn, ref)
    rows, warnings = _items(conn, b)
    found = {r.key: r for r in _ask(source, "get_tickets", [r.key for r in rows])} if rows else {}
    res = _Resolver(conn, getattr(source, "name", None))
    items = [{**res.ticket(found.get(r.key) or tickets.TicketRecord(
                 key=r.key, state=tickets.ERROR, state_reason="not found in the ticket source")),
              "position": pos, "rank": r.rank, "added_at": r.added_at} for pos, r in enumerate(rows, 1)]
    longest = max((len(r.rank or "") for r in rows), default=0)
    b.update(items=items, item_count=len(items), progress=_progress(items), warnings=warnings + res.warnings,
             max_rank_length=longest, needs_rebalance=longest > 12 or bool(warnings))
    return b

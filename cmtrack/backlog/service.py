"""Backlog domain logic: shared, ranked backlogs of top-level tickets.

A backlog is shared by several teams and related to CIs. Its membership and order are a set of
(ticket key, lexorank) pairs (rank.py), kept by the backlog's **rank store** (stores.py): cmtrack's own
SQLite table, or a text custom field in Jira. A move rewrites just the moved item's rank. The tickets
themselves (summary, state, affected CIs) are read live from the ticket source on every view.

The backlog row itself (name, teams, CIs, ticket source, which store and its params) is always in SQLite.
Functions that touch items take ``stores``: the configured stores by name (``sqlite`` is always there).

Like the rest of cmtrack's service layer, every function takes an open connection and does NOT commit; the
caller owns the transaction. Backlog references accept an id or a name.

What it uses from the core (cmtrack/service.py, cmtrack/tickets.py):
    CMError / NotFound / Conflict    errors that become 400 / 404 / 409
    _by_ref, log, to_dict(s)         row lookup, the event log, row -> dict
    SourceError                      502, for a store (Jira) that fails
    get_ci                           the ``ci`` table (a backlog relates to CIs)
    _ask, _Resolver, _progress       calling the ticket source, placing tickets, state counts
    tickets.TicketRecord, ERROR      a placeholder for keys the source no longer knows
The ticket source's ``get_tickets(keys)`` is required; ``top_level_tickets(backlog, cis)`` only for pull.
"""
import json
import sqlite3

from .. import tickets
from ..service import (CMError, Conflict, NotFound, SourceError, _ask, _by_ref, _progress, _Resolver, get_ci, log,
                       to_dict, to_dicts)
from . import rank
from .stores import SqliteStore, StoreError

DEFAULT_STORES = {"sqlite": SqliteStore()}


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
    """A backlog row as a dict, with its JSON columns (``teams``, ``store_params``) decoded."""
    for col in ("teams", "store_params"):
        if isinstance(d.get(col), str):
            d[col] = json.loads(d[col])
    return d


def _teams(teams):
    if teams is None:
        return []
    if isinstance(teams, str):
        teams = teams.split(",")
    if not isinstance(teams, list):
        raise CMError("teams must be a list of names")
    return [str(t).strip() for t in teams if str(t).strip()]


# ----------------------------------------------------------------------------- stores

def all_stores(stores=None):
    return {**DEFAULT_STORES, **(stores or {})}


def _store(stores, b):
    name = b.get("store") or "sqlite"
    st = all_stores(stores).get(name)
    if st is None:
        raise CMError(f"backlog {b['name']!r} keeps its order in store {name!r}, which isn't configured "
                      f"(CMTRACK_BACKLOG_STORES); configured: {sorted(all_stores(stores))}")
    return st


def _io(fn, *args):
    """Call a store; a store failure (Jira down, update refused) becomes a 502."""
    try:
        return fn(*args)
    except StoreError as e:
        raise SourceError(str(e)) from e


def _check_store(conn, stores, name, params, backlog_id=None):
    """(store name, params as JSON) after validation. Two backlogs can't share one Jira rank field."""
    name = (name or "sqlite").strip()
    st = all_stores(stores).get(name)
    if st is None:
        raise CMError(f"unknown backlog store {name!r}; configured: {sorted(all_stores(stores))}")
    if isinstance(params, str):
        params = json.loads(params or "{}")
    try:
        params = st.check_params(params or {})
    except ValueError as e:
        raise CMError(str(e)) from None
    if params.get("rank_field"):
        for other in conn.execute("SELECT id, name, store_params FROM backlog WHERE store = ? AND id IS NOT ?",
                                  (name, backlog_id)):
            if json.loads(other["store_params"] or "{}").get("rank_field") == params["rank_field"]:
                raise Conflict(f"{params['rank_field']} already holds the ranks of backlog {other['name']!r}; "
                               f"each backlog needs its own field")
    return name, json.dumps(params)


def _items(conn, stores, b):
    """(items in rank order, warnings). Invalid ranks sort last, by key; ties are broken by key."""
    good, bad = [], []
    for i in _io(_store(stores, b).items, conn, b):
        try:
            rank.validate(i.rank or "")
            good.append(i)
        except rank.RankError:
            bad.append(i)
    good.sort(key=lambda i: (i.rank, i.key))
    warnings = [f"{i.key} has no valid rank ({i.rank!r}); it sorts last until you rebalance"
                for i in sorted(bad, key=lambda i: i.key)]
    ties = sorted({a.rank for a, z in zip(good, good[1:]) if a.rank == z.rank})
    if ties:
        warnings.append(f"{len(ties)} rank(s) shared by more than one item (e.g. two moves at once); "
                        f"rebalance to separate them")
    return good + sorted(bad, key=lambda i: i.key), warnings


def _find(items, key, b):
    for i in items:
        if i.key == key:
            return i
    raise NotFound(f"{key} in this backlog")


def _append(conn, stores, b, items, keys, top=False):
    """Add keys (in order) at the bottom, or the top, of a backlog; returns their ranks."""
    valid = [i.rank for i in items if _valid(i.rank)]
    edge = (valid[0] if top else valid[-1]) if valid else None
    pairs = []
    for key in (reversed(keys) if top else keys):
        edge = rank.between(None, edge) if top else rank.between(edge, None)
        pairs.append((key, edge))
    if pairs:
        _io(_store(stores, b).add, conn, b, pairs)
    return [r for _, r in pairs]


def _valid(r):
    try:
        rank.validate(r or "")
        return True
    except rank.RankError:
        return False


# ----------------------------------------------------------------------------- backlogs

def list_backlogs(conn, ci_ref=None):
    """Backlogs (optionally only those related to a CI). ``item_count`` is only known without a call for
    SQLite backlogs; it's None for the others (counting them would mean asking Jira for each one)."""
    sql = ("SELECT b.*, CASE WHEN b.store = 'sqlite' THEN "
           "(SELECT COUNT(*) FROM backlog_item i WHERE i.backlog_id = b.id) END AS item_count, "
           "(SELECT group_concat(ci.name, ', ') FROM backlog_ci x JOIN ci ON ci.id = x.ci_id "
           " WHERE x.backlog_id = b.id) AS ci_names FROM backlog b")
    args = []
    if ci_ref:
        sql += " WHERE b.id IN (SELECT backlog_id FROM backlog_ci WHERE ci_id = ?)"
        args.append(get_ci(conn, ci_ref)["id"])
    return [_decode(d) for d in to_dicts(conn.execute(sql + " ORDER BY b.name", args))]


def create_backlog(conn, name, description=None, teams=None, cis=None, source=None, store=None,
                   store_params=None, stores=None):
    name = (name or "").strip()
    if not name:
        raise CMError("name is required")
    if name.isdigit():
        raise CMError("backlog name can't be only digits (it would read as an id)")
    store, params = _check_store(conn, stores, store, store_params)
    try:
        cur = conn.execute("INSERT INTO backlog (name, description, teams, source, store, store_params) "
                           "VALUES (?, ?, ?, ?, ?, ?)",
                           (name, description, json.dumps(_teams(teams)), source or None, store, params))
    except sqlite3.IntegrityError:
        raise Conflict(f"backlog {name!r} already exists") from None
    _set_backlog_cis(conn, cur.lastrowid, cis)
    log(conn, "backlog", cur.lastrowid, "created", name=name, teams=_teams(teams), cis=cis or [], store=store)
    return backlog_summary(conn, cur.lastrowid, stores)


def update_backlog(conn, ref, stores=None, **fields):
    """Change name/description/teams/cis/source, or move the order to another store (``store`` +
    ``store_params``): every item is copied with its rank, then removed from the old store."""
    b = _decode(to_dict(get_backlog(conn, ref)))
    unknown = set(fields) - {"name", "description", "teams", "cis", "source", "store", "store_params"}
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
    if "store" in fields or "store_params" in fields:
        name, params = _check_store(conn, stores, fields.get("store", b["store"]),
                                    fields.get("store_params", b["store_params"]), b["id"])
        if (name, json.loads(params)) != (b["store"], b["store_params"]):
            items, _ = _items(conn, stores, b)
            new = {**b, "store": name, "store_params": json.loads(params)}
            _io(_store(stores, new).add, conn, new, [(i.key, i.rank) for i in items])
            old = _store(stores, b)
            for i in items:
                _io(old.remove, conn, b, i.key)
            sets.update(store=name, store_params=params)
    if sets:
        try:
            conn.execute(f"UPDATE backlog SET {', '.join(k + ' = ?' for k in sets)} WHERE id = ?",
                         (*sets.values(), b["id"]))
        except sqlite3.IntegrityError:
            raise Conflict(f"backlog {sets.get('name')!r} already exists") from None
    if "cis" in fields:
        _set_backlog_cis(conn, b["id"], fields["cis"])
    log(conn, "backlog", b["id"], "updated", **fields)
    return backlog_summary(conn, b["id"], stores)


def backlog_summary(conn, ref, stores=None):
    b = _decode(to_dict(get_backlog(conn, ref)))
    b["cis"] = [c["name"] for c in _backlog_cis(conn, b["id"])]
    b["item_count"] = conn.execute("SELECT COUNT(*) FROM backlog_item WHERE backlog_id = ?",
                                   (b["id"],)).fetchone()[0] if b["store"] == "sqlite" else None
    st = all_stores(stores).get(b["store"])
    b["store_label"] = st.describe(b) if st else f"{b['store']} (not configured)"
    return b


def add_backlog_item(conn, source, ref, key, position="bottom", stores=None):
    """Put a top-level ticket on the backlog (checked against the source) at the top or bottom."""
    b = backlog_summary(conn, ref, stores)
    key = str(key or "").strip()
    if not key:
        raise CMError("key is required")
    if position not in ("top", "bottom"):
        raise CMError("position must be top or bottom")
    items, _ = _items(conn, stores, b)
    if any(i.key == key for i in items):
        raise Conflict(f"{key} is already in {b['name']}")
    rec = next((r for r in _ask(source, "get_tickets", [key]) if r.key == key), None)
    if rec is None:
        raise NotFound(f"ticket {key!r} not found in {source.name}")
    if rec.parent_key:
        raise CMError(f"{key} is a CSC ticket under {rec.parent_key}; add the top-level ticket instead")
    new = _append(conn, stores, b, items, [key], top=position == "top")
    log(conn, "backlog", b["id"], "item_added", key=key, position=position)
    return {"key": key, "rank": new[0]}


def remove_backlog_item(conn, ref, key, stores=None):
    b = backlog_summary(conn, ref, stores)
    items, _ = _items(conn, stores, b)
    _find(items, key, b)
    _io(_store(stores, b).remove, conn, b, key)
    log(conn, "backlog", b["id"], "item_removed", key=key)
    return {"removed": key}


def move_backlog_item(conn, ref, key, after=None, before=None, stores=None):
    """Re-rank ``key`` to sit between its new neighbours: ``after`` (the item now above it) and/or
    ``before`` (the item now below it). Give one to move next to an item, both after a drag and drop.
    Only the moved item's rank changes. 409 when the neighbours are no longer in that order."""
    b = backlog_summary(conn, ref, stores)
    if key in (after, before):
        raise CMError("an item can't be its own neighbour")
    if not after and not before:
        raise CMError("give 'after' and/or 'before' (the keys of the new neighbours)")
    items, _ = _items(conn, stores, b)
    item = _find(items, key, b)
    others = [i for i in items if i.key != key]
    lo_i = _find(others, after, b) if after else None
    hi_i = _find(others, before, b) if before else None
    if after and not before:
        pos = others.index(lo_i)
        hi_i = others[pos + 1] if pos + 1 < len(others) else None
    elif before and not after:
        pos = others.index(hi_i)
        lo_i = others[pos - 1] if pos > 0 else None
    for n in (lo_i, hi_i):
        if n is not None and not _valid(n.rank):
            raise CMError(f"{n.key} has no valid rank; rebalance the backlog first")
    lo, hi = (lo_i.rank if lo_i else None), (hi_i.rank if hi_i else None)
    if lo is not None and hi is not None and lo >= hi:
        why = "share a rank; rebalance the backlog" if lo == hi else "are not in that order any more; reload"
        raise Conflict(f"{lo_i.key} and {hi_i.key} {why} and try again")
    new = rank.between(lo, hi)
    _io(_store(stores, b).set_rank, conn, b, key, new)
    log(conn, "backlog", b["id"], "item_moved", key=key, after=after, before=before)
    return {"key": key, "rank": new, "previous_rank": item.rank}


def rebalance_backlog(conn, ref, stores=None):
    """Re-space every rank evenly (order unchanged): after many drops in the same spot, or to repair ties
    and invalid values in a Jira field."""
    b = backlog_summary(conn, ref, stores)
    items, _ = _items(conn, stores, b)
    ranks = rank.spread(len(items))
    _io(_store(stores, b).set_ranks, conn, b, {i.key: r for i, r in zip(items, ranks)})
    log(conn, "backlog", b["id"], "rebalanced", items=len(items))
    return {"items": len(items), "max_rank_length": max(map(len, ranks), default=0)}


def pull_backlog(conn, source, ref, stores=None):
    """Append top-level tickets the source offers for this backlog that aren't on it yet (source order)."""
    b = backlog_summary(conn, ref, stores)
    items, _ = _items(conn, stores, b)
    have = {i.key for i in items}
    offered = _ask(source, "top_level_tickets", b, _backlog_cis(conn, b["id"]))
    new, seen = [], set()
    for rec in offered:
        if rec.parent_key or rec.key in have or rec.key in seen:
            continue
        seen.add(rec.key)
        new.append(rec.key)
    _append(conn, stores, b, items, new)
    if new:
        log(conn, "backlog", b["id"], "pulled", source=source.name, added=new)
    return {"added": new, "offered": len(offered), "already": len(offered) - len(new)}


def backlog_view(conn, source, ref, stores=None):
    """The backlog in rank order with each ticket read live from the source (one get_tickets call)."""
    b = backlog_summary(conn, ref, stores)
    rows, store_warnings = _items(conn, stores, b)
    found = {r.key: r for r in _ask(source, "get_tickets", [r.key for r in rows])} if rows else {}
    res = _Resolver(conn, getattr(source, "name", None))
    items = []
    for pos, row in enumerate(rows, 1):
        rec = found.get(row.key) or tickets.TicketRecord(
            key=row.key, state=tickets.ERROR, state_reason="not found in the ticket source")
        t = res.ticket(rec)
        t.update(position=pos, rank=row.rank, added_at=row.added_at)
        items.append(t)
    longest = max((len(r.rank or "") for r in rows), default=0)
    b.update(items=items, item_count=len(items), progress=_progress(items), warnings=store_warnings + res.warnings,
             max_rank_length=longest, needs_rebalance=longest > 12 or bool(store_warnings))
    return b

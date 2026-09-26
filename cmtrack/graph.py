"""Lay out a lineage DAG as a git-style graph: one row per node, newest first, branches in lanes.

``layout(nodes)`` takes nodes in display order, children before parents, each a dict with ``id`` and
``parents`` (ids; parents not in ``nodes`` are ignored), and returns what ``ui.graph`` draws: every node with
its ``row``, ``lane`` and ``x``/``y``, the SVG path of every edge, and the size. ``horizontal=True`` lays the
same graph out left to right, oldest first (``ui.graph_strip``). A node's first parent continues its lane
(so a line of releases stays in one lane and a branch rejoins it where it forked); further parents (merges)
join the lane already heading to that parent, or open a new one.
"""
import heapq

ROW = 40     # px per row (vertical)
LANE = 16    # px per lane (vertical)
COL = 104    # px per column (horizontal: room for a label under each node)
HLANE = 64   # px per lane (horizontal: room for the label, tag and highlight)
LANES = 6    # lane colours before they repeat (ui.css .ui-graph__l0 … l5)


def _path(a0, c0, cl, a1, c1, step, pt):
    """Edge from a child at (along a0, across c0) to its parent at (a1, c1), running in lane ``cl``.
    ``pt(along, across)`` maps to SVG "x y" for the orientation."""
    s = step if a1 > a0 else -step
    h = s // 2
    if abs(a1 - a0) == step:
        return f"M{pt(a0, c0)} L{pt(a1, c1)}" if c0 == c1 else f"M{pt(a0, c0)} C{pt(a0 + h, c0)} {pt(a1 - h, c1)} {pt(a1, c1)}"
    d = f"M{pt(a0, c0)} "
    d += f"L{pt(a0 + s, cl)} " if cl == c0 else f"C{pt(a0 + h, c0)} {pt(a0 + h, cl)} {pt(a0 + s, cl)} "
    d += f"L{pt(a1 - s, cl)} "
    d += f"L{pt(a1, c1)}" if cl == c1 else f"C{pt(a1 - h, cl)} {pt(a1 - h, c1)} {pt(a1, c1)}"
    return d


def _free(lanes):
    if None in lanes:
        return lanes.index(None)
    lanes.append(None)
    return len(lanes) - 1


def layout(nodes, horizontal=False):
    row_of = {n["id"]: i for i, n in enumerate(nodes)}
    lanes = []                       # lane -> id of the node the lane is heading to (None = free)
    placed, pending, width = [], [], 1
    for row, n in enumerate(nodes):
        mine = [i for i, x in enumerate(lanes) if x == n["id"]]
        col = mine[0] if mine else _free(lanes)
        for i in mine:
            lanes[i] = None
        parents = [p for p in n["parents"] if row_of.get(p, -1) > row]
        for k, p in enumerate(parents):
            if k == 0:
                lane = col
            elif p in lanes:
                lane = lanes.index(p)
            else:
                lane = _free(lanes)
            lanes[lane] = p
            pending.append((row, col, lane, p, k > 0))
        width = max(width, len(lanes), col + 1)
        while lanes and lanes[-1] is None:
            lanes.pop()
        placed.append({**n, "row": row, "lane": col, "color": col % LANES})
    return _geometry(placed, pending, width, horizontal)


def swimlanes(nodes, lane, horizontal=False):
    """Lay out nodes (oldest first) in fixed lanes, ``lane(node)`` -> 0, 1, …: one lane per line of work (e.g.
    per IFC), so a line keeps its lane however the lines interleave in time. An edge to a parent in another lane
    runs along the child's lane and turns into the parent at the end: fine as long as a lane's nodes are a
    straight sequence and its first node is the only one with a parent elsewhere."""
    placed = [{**n, "row": row, "lane": lane(n), "color": lane(n) % LANES} for row, n in enumerate(reversed(nodes))]
    row_of = {n["id"]: n["row"] for n in placed}
    pending = [(n["row"], n["lane"], n["lane"], p, k > 0) for n in placed
               for k, p in enumerate(q for q in n["parents"] if row_of.get(q, -1) > n["row"])]
    return _geometry(placed, pending, max([n["lane"] + 1 for n in placed] or [1]), horizontal)


def _geometry(placed, pending, width, horizontal):
    row_of = {p["id"]: p["row"] for p in placed}
    n = len(placed)
    if horizontal:   # oldest on the left: columns run the other way from rows
        step, across = COL, HLANE
        along = lambda row: step // 2 + (n - 1 - row) * step
        pt = lambda a, c: f"{a} {c}"
    else:
        step, across = ROW, LANE
        along = lambda row: step // 2 + row * step
        pt = lambda a, c: f"{c} {a}"
    off = lambda lane: across // 2 + lane * across
    for p in placed:
        a, c = along(p["row"]), off(p["lane"])
        p["x"], p["y"] = (a, c) if horizontal else (c, a)
    edges = [{"d": _path(along(row), off(col), off(lane), along(row_of[p]), off(placed[row_of[p]]["lane"]), step, pt),
              "color": lane % LANES, "merge": merge}
             for row, col, lane, p, merge in pending]
    size = (n * step, width * across)
    return {"nodes": placed, "edges": edges, "width": size[0] if horizontal else size[1],
            "height": size[1] if horizontal else size[0], "row": step, "lane": across}


def newest_first(nodes, key):
    """``nodes`` ordered for ``layout``: children before parents, otherwise by ``key`` descending (so rows read
    as a timeline, and the line with the newest work takes the first lane)."""
    by_id = {n["id"]: n for n in nodes}
    waiting = {i: 0 for i in by_id}
    children = {}
    for n in nodes:
        for p in n["parents"]:
            if p in by_id:
                waiting[n["id"]] += 1
                children.setdefault(p, []).append(n["id"])
    ready = [(key(n), n["id"]) for n in nodes if waiting[n["id"]] == 0]
    heapq.heapify(ready)
    order = []
    while ready:
        _, i = heapq.heappop(ready)
        order.append(by_id[i])
        for c in children.get(i, []):
            waiting[c] -= 1
            if waiting[c] == 0:
                heapq.heappush(ready, (key(by_id[c]), c))
    seen = {n["id"] for n in order}
    return order[::-1] + [n for n in nodes if n["id"] not in seen]   # leftovers are on a cycle

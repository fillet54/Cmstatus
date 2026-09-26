"""Lay out a lineage DAG as a git-style graph: one row per node, newest first, branches in lanes.

``layout(nodes)`` takes nodes in display order, children before parents, each a dict with ``id`` and
``parents`` (ids; parents not in ``nodes`` are ignored), and returns what ``ui.graph`` draws: every node with
its ``row`` and ``lane``, the SVG path of every edge, and the size. A node's first parent continues its lane
(so a line of releases stays in one lane and a branch rejoins it where it forked); further parents (merges)
join the lane already heading to that parent, or open a new one.
"""
import heapq

ROW = 40     # px per row
LANE = 16    # px per lane
LANES = 6    # lane colours before they repeat (ui.css .ui-graph__l0 … l5)


def _x(lane):
    return LANE // 2 + lane * LANE


def _y(row):
    return ROW // 2 + row * ROW


def _path(row, col, lane, prow, pcol):
    """Child (row, col) down lane ``lane`` to parent (prow, pcol)."""
    x0, y0, xl, x1, y1 = _x(col), _y(row), _x(lane), _x(pcol), _y(prow)
    h = ROW // 2
    if prow == row + 1:
        return f"M{x0} {y0} L{x1} {y1}" if x0 == x1 else f"M{x0} {y0} C{x0} {y0 + h} {x1} {y1 - h} {x1} {y1}"
    d = f"M{x0} {y0} "
    d += f"L{xl} {y0 + ROW} " if xl == x0 else f"C{x0} {y0 + h} {xl} {y0 + h} {xl} {y0 + ROW} "
    d += f"L{xl} {y1 - ROW} "
    d += f"L{x1} {y1}" if xl == x1 else f"C{xl} {y1 - h} {x1} {y1 - h} {x1} {y1}"
    return d


def _free(lanes):
    if None in lanes:
        return lanes.index(None)
    lanes.append(None)
    return len(lanes) - 1


def layout(nodes):
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
    edges = [{"d": _path(row, col, lane, row_of[p], placed[row_of[p]]["lane"]), "color": lane % LANES, "merge": merge}
             for row, col, lane, p, merge in pending]
    return {"nodes": placed, "edges": edges, "width": width * LANE, "height": len(nodes) * ROW, "row": ROW,
            "lane": LANE}


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

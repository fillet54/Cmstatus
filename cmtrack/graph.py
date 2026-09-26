"""Lay out a lineage DAG as a git-style graph: one row per node, newest first, branches in lanes.

``layout(nodes)`` takes nodes in display order, children before parents, each a dict with ``id`` and
``parents`` (ids; parents not in ``nodes`` are ignored), and returns what ``ui.graph`` draws: every node with
its ``row``, ``lane`` and ``x``/``y``, the SVG path of every edge, and the size. ``horizontal=True`` lays the
same graph out left to right, oldest first. A node's first parent continues its lane
(so a line of releases stays in one lane and a branch rejoins it where it forked); further parents (merges)
join the lane already heading to that parent, or open a new one.
"""
import datetime as dt
import heapq

ROW = 40     # px per row (vertical)
LANE = 16    # px per lane (vertical)
COL = 104    # px per column (horizontal: room for a label under each node)
HLANE = 84   # px per lane (horizontal: room for the name above, label and tag below)
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


def swimlanes(nodes, group, horizontal=False):
    """Lay out nodes (oldest first) in lanes by ``group(node)``: one lane per line of work (e.g. per IFC), so a
    line keeps one lane however lines interleave in time. A lane is held from where the line branches off (its
    first node's parent) to its last node, then reused by a later line, so finished lines don't widen the
    graph. An edge to a parent in another lane runs along the child's lane and turns into the parent at the
    end: fine as long as a line's nodes are a straight sequence and only its first has a parent elsewhere."""
    index = {n["id"]: i for i, n in enumerate(nodes)}
    pos = index
    spans, first = {}, {}
    for n in nodes:
        g, at = group(n), pos[n["id"]]
        first.setdefault(g, index[n["id"]])
        start = min([at] + [pos[p] for p in n["parents"] if p in pos])
        lo, hi = spans.get(g, (start, at))
        spans[g] = (min(lo, start), max(hi, at))
    lane_of, ends = {}, []                             # ends[lane] = last position the lane is held to
    for g, (lo, hi) in sorted(spans.items(), key=lambda kv: (kv[1][0], first[kv[0]])):   # ties: older line first
        lane = next((k for k, end in enumerate(ends) if end < lo), None)
        if lane is None:
            lane = len(ends)
            ends.append(hi)
        ends[lane] = hi
        lane_of[g] = lane
    count = max(pos.values(), default=-1) + 1
    placed = [{**n, "row": count - 1 - pos[n["id"]], "lane": lane_of[group(n)], "color": lane_of[group(n)] % LANES}
              for n in reversed(nodes)]
    row_of = {n["id"]: n["row"] for n in placed}
    pending = [(n["row"], n["lane"], n["lane"], p, k > 0) for n in placed
               for k, p in enumerate(q for q in n["parents"] if row_of.get(q, -1) > n["row"])]
    return _geometry(placed, pending, len(ends) or 1, horizontal, count)


def _geometry(placed, pending, width, horizontal, n=None):
    """Pixel positions and edge paths for placed nodes (``row``: 0 = newest; ``n`` positions in all)."""
    row_of = {p["id"]: p["row"] for p in placed}
    n = len(placed) if n is None else n
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
    by_id = {p["id"]: p for p in placed}
    edges = [{"d": _path(along(row), off(col), off(lane), along(row_of[p]), off(by_id[p]["lane"]), step, pt),
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


# ----------------------------------------------------------------------------- timelines

TL_LANE = 30        # px per lane
TL_AXIS = 34        # px for the date axis above the lanes
ZOOMS = {"years": 0.12, "quarters": 0.4, "months": 1.2, "weeks": 4.0}   # px per day
LABEL_PX = 34       # room a short node label takes to the right of its dot
CHAR_PX = 6.2       # rough width of a caption character


def short_label(name):
    """Build 3 -> B3, HSC1.1 -> H1.1; anything else as it is, cut to 7 characters."""
    for word, letter in (("build", "B"), ("hsc", "H")):
        if name.lower().startswith(word):
            rest = name[len(word):].strip()
            if rest and rest[0].isdigit():
                return letter + rest
    return name[:7]


def _day(value):
    return dt.date.fromisoformat(str(value)[:10])


def timeline(nodes, group, today, px_per_day=ZOOMS["months"], caption=None, fit_width=None):
    """Lay out nodes (oldest first; each with ``id``, ``parents``, ``date``) on a time axis: x is the date,
    one lane per ``group`` while it is active. A lane is held from the parent a line branches off to its last
    node plus room for its labels, then reused, so finished lines don't add height. ``caption(node)`` names
    the line at its first node. Returns positioned nodes, edge paths, lane captions, axis ticks, today's x
    and the size; the axis runs from a quarter before the first node to a year after today, so today can
    always be scrolled to the middle. ``fit_width`` (px) stretches the scale so the timeline is at least that
    wide, for zooms that would otherwise leave its container part empty."""
    origin = min([_day(n["date"]) for n in nodes] + [today]) - dt.timedelta(days=90)
    end = max([_day(n["date"]) for n in nodes] + [today]) + dt.timedelta(days=365)
    if fit_width:
        px_per_day = max(px_per_day, (fit_width - 16) / (end - origin).days)
    x = lambda d: round((_day(d) - origin).days * px_per_day) + 8
    by_id = {n["id"]: n for n in nodes}
    dense = px_per_day < 0.35                          # too cramped for labels: dots only, names on hover
    room = 6 if dense else LABEL_PX

    spans, first, members = {}, {}, {}
    for i, n in enumerate(nodes):
        g = group(n)
        members.setdefault(g, []).append(n)
        first.setdefault(g, i)
        lo = min([x(n["date"])] + [x(by_id[p]["date"]) for p in n["parents"] if p in by_id])
        hi = x(n["date"]) + room
        if first[g] == i and caption and not dense:
            hi = max(hi, x(n["date"]) + round(len(caption(n)) * CHAR_PX))
        a, b = spans.get(g, (lo, hi))
        spans[g] = (min(a, lo), max(b, hi))
    lane_of, ends = {}, []
    for g, (lo, hi) in sorted(spans.items(), key=lambda kv: (kv[1][0], first[kv[0]])):
        lane = next((k for k, e in enumerate(ends) if e + 6 < lo), None)
        if lane is None:
            lane, ends = len(ends), ends + [hi]
        ends[lane] = hi
        lane_of[g] = lane

    y = lambda lane: TL_AXIS + lane * TL_LANE + TL_LANE // 2
    placed = [{**n, "x": x(n["date"]), "y": y(lane_of[group(n)]), "lane": lane_of[group(n)],
               "color": lane_of[group(n)] % LANES, "label": short_label(n["name"])} for n in nodes]
    at = {n["id"]: n for n in placed}
    edges = []
    for n in placed:
        for k, p in enumerate(q for q in n["parents"] if q in at):
            a = at[p]
            if a["y"] == n["y"]:
                d = f"M{a['x']} {a['y']} L{n['x']} {n['y']}"
            else:                                            # turn out of the parent into the child's lane
                c = max(2, min(12, (n["x"] - a["x"]) // 2))
                d = (f"M{a['x']} {a['y']} C{a['x'] + c} {a['y']} {a['x'] + c} {n['y']} {a['x'] + 2 * c} {n['y']} "
                     f"L{n['x']} {n['y']}")
            edges.append({"d": d, "color": n["color"], "merge": k > 0, "from": p, "to": n["id"]})
    captions = [{"text": caption(members[g][0]), "x": x(members[g][0]["date"]) - 4, "y": y(lane_of[g]) - 9,
                 "color": lane_of[g] % LANES, "href": members[g][0].get("group_href"), "lane": lane_of[g],
                 "node": members[g][0]["id"]}
                for g in members] if caption else []

    ticks, year = [], origin.year
    step = 1 if px_per_day >= 2 else 3 if px_per_day >= 0.3 else 12
    d = dt.date(origin.year, 1, 1)
    while d <= end:
        if d >= origin:
            ticks.append({"x": x(d), "major": d.month == 1,
                          "label": str(d.year) if d.month == 1 else
                          (f"Q{(d.month - 1) // 3 + 1}" if step == 3 else d.strftime("%b"))})
        d = dt.date(d.year + (d.month - 1 + step) // 12, (d.month - 1 + step) % 12 + 1, 1)
    return {"nodes": placed, "edges": edges, "captions": captions, "ticks": ticks, "today_x": x(today),
            "today": today.isoformat(), "width": x(end) + 8, "height": y(len(ends) or 1) - TL_LANE // 2 + 6,
            "lanes": len(ends), "px_per_day": px_per_day, "dense": dense,
            "origin": origin.isoformat(), "end": end.isoformat()}

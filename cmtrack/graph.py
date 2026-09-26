"""SVG layouts for lineage: a git-style graph of a CI's versions, and the IFC timeline.

``layout(nodes)`` takes nodes in display order, children before parents, each a dict with ``id`` and
``parents`` (ids; parents not in ``nodes`` are ignored), and returns what ``ui.graph`` draws: every node with
its ``row``, ``lane`` and ``x``/``y``, the SVG path of every edge, and the size. A node's first parent continues
its lane (so a line of releases stays in one lane and a branch rejoins it where it forked); further parents
(merges) join the lane already heading to that parent, or open a new one.
"""
import datetime as dt
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
    x0, y0, xl, x1, y1, h = _x(col), _y(row), _x(lane), _x(pcol), _y(prow), ROW // 2
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
        for k, p in enumerate(p for p in n["parents"] if row_of.get(p, -1) > row):
            lane = col if k == 0 else lanes.index(p) if p in lanes else _free(lanes)
            lanes[lane] = p
            pending.append((row, col, lane, p, k > 0))
        width = max(width, len(lanes), col + 1)
        while lanes and lanes[-1] is None:
            lanes.pop()
        placed.append({**n, "row": row, "lane": col, "color": col % LANES, "x": _x(col), "y": _y(row)})
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
            edges.append({"d": d, "color": n["color"], "merge": k > 0})
    captions = [{"text": caption(members[g][0]), "x": x(members[g][0]["date"]) - 4, "y": y(lane_of[g]) - 9,
                 "color": lane_of[g] % LANES, "href": members[g][0].get("group_href")}
                for g in members] if caption else []

    ticks = []
    step = 1 if px_per_day >= 2 else 3 if px_per_day >= 0.3 else 12
    d = dt.date(origin.year, 1, 1)
    while d <= end:
        if d >= origin:
            ticks.append({"x": x(d), "major": d.month == 1,
                          "label": str(d.year) if d.month == 1 else
                          (f"Q{(d.month - 1) // 3 + 1}" if step == 3 else d.strftime("%b"))})
        d = dt.date(d.year + (d.month - 1 + step) // 12, (d.month - 1 + step) % 12 + 1, 1)
    return {"nodes": placed, "edges": edges, "captions": captions, "ticks": ticks, "today_x": x(today),
            "width": x(end) + 8, "height": y(len(ends) or 1) - TL_LANE // 2 + 6, "dense": dense}

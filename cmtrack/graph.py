"""Timelines: lineage laid out on a time axis, one lane per line of work while it's active.

``timeline`` draws a CI's versions (release lines, fix branches, merges) and the IFCs' HSCM builds; ``newest_first``
orders a DAG for display (the service uses it to sort versions parents-first).
"""
import datetime as dt
import heapq

LANES = 6    # lane colours before they repeat (ui.css .ui-graph__l0 … l5)


def newest_first(nodes, key):
    """``nodes`` (each with ``id`` and ``parents``) children before parents, otherwise by ``key`` descending."""
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


def timeline(nodes, group, today, px_per_day=ZOOMS["months"], fit_width=None):
    """Lay out nodes (oldest first; each with ``id``, ``parents``, ``date``, ``name``, and optionally ``label``
    and ``caption``) on a time axis: x is the date, one lane per ``group`` while it is active. A lane is held
    from the parent a line branches off to its last node plus room for its labels, then reused, so finished
    lines don't add height. A node's ``caption`` is drawn above it (e.g. the line's name at its first node),
    its ``label`` (default ``short_label(name)``) beside it. A first parent in another lane is a branch: the
    edge turns out of the parent into the child's lane. Further parents are merges: the edge runs along the
    parent's lane and turns into the child. Returns positioned nodes, edge paths, captions, axis ticks, today's
    x and the size; the axis runs from a quarter before the first node to a year after today, so today can
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

    spans, first = {}, {}
    for i, n in enumerate(nodes):
        g = group(n)
        first.setdefault(g, i)
        lo = min([x(n["date"])] + [x(by_id[p]["date"]) for p in n["parents"][:1] if p in by_id])
        hi = x(n["date"]) + room
        if n.get("caption") and not dense:
            hi = max(hi, x(n["date"]) + round(len(n["caption"]) * CHAR_PX))
        a, b = spans.get(g, (lo, hi))
        spans[g] = (min(a, lo), max(b, hi))
    for n in nodes:                                        # a merge edge runs along its parent's lane: hold it
        for p in n["parents"][1:]:
            if p in by_id:
                g = group(by_id[p])
                spans[g] = (spans[g][0], max(spans[g][1], x(n["date"])))
    lane_of, ends = {}, []
    for g, (lo, hi) in sorted(spans.items(), key=lambda kv: (kv[1][0], first[kv[0]])):
        lane = next((k for k, e in enumerate(ends) if e + 6 < lo), None)
        if lane is None:
            lane, ends = len(ends), ends + [hi]
        ends[lane] = hi
        lane_of[g] = lane

    y = lambda lane: TL_AXIS + lane * TL_LANE + TL_LANE // 2
    placed = [{**n, "x": x(n["date"]), "y": y(lane_of[group(n)]), "lane": lane_of[group(n)],
               "color": lane_of[group(n)] % LANES, "label": n.get("label") or short_label(n["name"])} for n in nodes]
    at = {n["id"]: n for n in placed}
    edges = []
    for n in placed:
        for k, p in enumerate(q for q in n["parents"] if q in at):
            a = at[p]
            c = max(2, min(12, (n["x"] - a["x"]) // 2))
            if a["y"] == n["y"]:
                d = f"M{a['x']} {a['y']} L{n['x']} {n['y']}"
            elif k == 0:                                     # branch: turn out of the parent into the child's lane
                d = (f"M{a['x']} {a['y']} C{a['x'] + c} {a['y']} {a['x'] + c} {n['y']} {a['x'] + 2 * c} {n['y']} "
                     f"L{n['x']} {n['y']}")
            else:                                            # merge: along the parent's lane, then into the child
                d = (f"M{a['x']} {a['y']} L{n['x'] - 2 * c} {a['y']} C{n['x'] - c} {a['y']} {n['x'] - c} {n['y']} "
                     f"{n['x']} {n['y']}")
            edges.append({"d": d, "color": a["color"] if k else n["color"], "merge": k > 0})
    captions = [{"text": n["caption"], "x": n["x"] - 4, "y": n["y"] - 9, "color": n["color"], "href": n.get("caption_href")}
                for n in placed if n.get("caption")]

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

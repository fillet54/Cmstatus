"""Support for the UI component library (templates/ui/, static/ui.css): config, filters and shell context.

Config (app.config, or the environment at startup):
    UI_MARKING      {"text", "bg", "fg"} for the top/bottom marking banners.
                    CMTRACK_MARKING="CUI" (UNCLASSIFIED and CUI have standard colours; anything else also
                    needs CMTRACK_MARKING_COLORS="#bg,#fg"). Unset shows "[Marking not configured]" on purpose.
    UI_PROGRAM      program name in the header (CMTRACK_PROGRAM).
    UI_FONTS_CSS    stylesheet that loads IBM Plex / Source Serif (CMTRACK_UI_FONTS_CSS). Defaults to Google
                    Fonts; point it at self-hosted fonts on a closed network, or "" for system fonts.
    UI_HTMX_JS      htmx script URL (CMTRACK_UI_HTMX_JS); self-host it the same way.
"""
import datetime as dt
import json
import os

from flask import current_app, g, url_for
from werkzeug.routing import BuildError

from . import graph, tickets

MARKING_COLORS = {"UNCLASSIFIED": ("#007A33", "#FFFFFF"), "CUI": ("#502B85", "#FFFFFF")}

GOOGLE_FONTS = ("https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600"
                "&family=IBM+Plex+Sans:wght@400;500;600;700&family=Source+Serif+4:opsz,wght@8..60,600&display=swap")
HTMX = "https://cdn.jsdelivr.net/npm/htmx.org@2.0.4/dist/htmx.min.js"

# (key, label, endpoint) for the header nav; entries whose endpoint doesn't exist are skipped.
NAV = [
    ("overview", "Overview", "ui.dashboard"),
    ("cis", "Configuration items", "ui.cis"),
    ("backlogs", "Backlogs", "ui.backlogs"),
    ("ifcs", "Capabilities", "ui.ifcs"),
    ("events", "Audit log", "ui.events"),
]

VERSION_STATUSES = {"planned": "Planned", "built": "Built", "tested": "Tested", "released": "Released",
                    "rejected": "Rejected", "external": "External", "active": "Active", "cancelled": "Cancelled"}


def marking_from_env(text=None, colors=None):
    """Banner config from CMTRACK_MARKING / CMTRACK_MARKING_COLORS; None when not configured."""
    text = (text if text is not None else os.environ.get("CMTRACK_MARKING", "")).strip()
    if not text:
        return None
    colors = colors if colors is not None else os.environ.get("CMTRACK_MARKING_COLORS", "")
    if colors:
        bg, _, fg = colors.partition(",")
    elif text.upper() in MARKING_COLORS:
        bg, fg = MARKING_COLORS[text.upper()]
    else:
        raise ValueError(f"marking {text!r} has no standard colours; set CMTRACK_MARKING_COLORS=\"#bg,#fg\"")
    return {"text": text, "bg": bg.strip(), "fg": (fg or "#FFFFFF").strip()}


def _as_utc(value):
    """datetime (aware = converted, naive = assumed UTC, as SQLite's datetime('now') is) or None for dates/junk."""
    if isinstance(value, dt.datetime):
        d = value
    elif isinstance(value, str) and len(value) > 10:
        try:
            d = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00").replace(" ", "T", 1))
        except ValueError:
            return None
    else:
        return None
    return d.replace(tzinfo=dt.timezone.utc) if d.tzinfo is None else d.astimezone(dt.timezone.utc)


def utc(value, seconds=False):
    """'2026-09-23 14:24Z'. Dates stay dates ('2027-03-15'); empty stays empty."""
    if not value:
        return ""
    d = _as_utc(value)
    if d is None:
        return str(value)
    return d.strftime("%Y-%m-%d %H:%M:%SZ" if seconds else "%Y-%m-%d %H:%MZ")


def iso(value):
    """Machine-readable form for <time datetime>."""
    d = _as_utc(value)
    return d.strftime("%Y-%m-%dT%H:%M:%SZ") if d else (str(value) if value else "")


def pretty_json(value):
    """Indented JSON for display in <pre> (autoescaped by Jinja, unlike tojson's \u003c escapes)."""
    return json.dumps(value, indent=2, ensure_ascii=False)


def _nav():
    items = []
    for key, label, endpoint in current_app.config["UI_NAV"]:
        try:
            items.append({"key": key, "label": label, "href": url_for(endpoint)})
        except BuildError:
            continue
    return items


def init_app(app):
    app.config.setdefault("UI_MARKING", marking_from_env())
    app.config.setdefault("UI_PROGRAM", os.environ.get("CMTRACK_PROGRAM") or None)
    app.config.setdefault("UI_FONTS_CSS", os.environ.get("CMTRACK_UI_FONTS_CSS", GOOGLE_FONTS))
    app.config.setdefault("UI_HTMX_JS", os.environ.get("CMTRACK_UI_HTMX_JS", HTMX))
    app.config.setdefault("UI_NAV", NAV)
    app.jinja_env.filters["utc"] = utc
    app.jinja_env.filters["iso"] = iso
    app.jinja_env.filters["pretty_json"] = pretty_json
    app.jinja_env.globals["ui_states"] = tickets.STATES
    app.jinja_env.globals["ui_version_statuses"] = VERSION_STATUSES

    @app.context_processor
    def ui_shell():
        c = current_app.config
        return {"ui_marking": c["UI_MARKING"], "ui_program": c["UI_PROGRAM"], "ui_fonts_css": c["UI_FONTS_CSS"],
                "ui_htmx_js": c["UI_HTMX_JS"], "ui_nav": _nav, "ui_user": g.get("ui_user")}


def styleguide_samples():
    """Fixed sample data for the /ui style guide (mirrors the demo scenario)."""
    progress = {s: 0 for s in tickets.STATES}
    progress.update(analysis_required=1, in_progress=1, peer_review=1, merge_blocked=1, done=2, error=1, total=7)
    jira = "https://jira.example.com/browse/"
    ticket = lambda key, summary, state, versions, reason=None, status=None, type=None: {
        "key": key, "summary": summary, "state": state, "state_reason": reason, "status": status, "type": type,
        "url": jira + key, "versions": [{"name": v} for v in versions]}
    v = lambda name, release, status, *parents, **kw: {"id": name, "name": name, "release": release,
                                                          "status": status, "parents": list(parents), **kw}
    lineage = [v("2027.Q1-b2", "2027.Q1", "built", "2027.Q1-b1"),
               v("2026.Q4.P1", "2026.Q4.P1", "planned", "2026.Q4-b4", dot="hollow"),
               v("2027.Q1-b1", "2027.Q1", "built", "2026.Q4-b4", "2026.Q4.ER1"),
               v("2026.Q4.ER1", "2026.Q4.ER1", "released", "2026.Q4-b4", current=True),
               v("2026.Q4-b4", "2026.Q4", "released", "2026.Q4-b3", current=True),
               v("2026.Q4-b3", "2026.Q4", "rejected", dot="danger")]
    return {
        "graph_sample": graph.layout(lineage),
        "timeline_sample": graph.timeline([
            {"id": 1, "name": "Build 1", "date": "2024-01-10", "parents": [], "ifc": "A 1.0", "dot": "muted"},
            {"id": 2, "name": "HSC1", "date": "2024-04-02", "parents": [1], "ifc": "A 1.0", "final": True},
            {"id": 3, "name": "HSC1", "date": "2024-06-20", "parents": [2], "ifc": "A 1.0.1", "current": True},
            {"id": 4, "name": "HSC1.1", "date": "2024-09-15", "parents": [3], "ifc": "A 1.0.1", "dot": "hollow"}],
            lambda n: n["ifc"], dt.date(2024, 10, 1), 0.6, caption=lambda n: n["ifc"]),
        "progress": progress,
        "tickets": [
            ticket("NAVL-105", "Blend terrain-referenced fixes into the filter", "peer_review", ["2027.Q1-b1"],
                   status="In Review"),
            ticket("NAVL-106", "Terrain fix quality gating", "merge_blocked", ["2027.Q1-b2"],
                   "Waiting on NAVX-205 (shared interface change) to merge first", "Ready to Merge"),
            ticket("NAVX-221", "Declutter preset storage", "error", ["2027.Q1-b3"],
                   "Closed as Done but its sub-task NAVX-222 is still open", "Closed"),
        ],
        "backlog": [
            {"key": "PRG-22", "summary": "Route re-planning around restricted airspace", "type": "Feature",
             "state": "analysis_in_progress", "rank": "h", "cis": ["NAV-SW"], "url": jira + "PRG-22"},
            {"key": "PRG-10", "summary": "GPS-denied navigation", "type": "Feature", "state": "in_progress",
             "rank": "i", "cis": ["NAV-SW", "DISPLAY-SW"], "url": jira + "PRG-10"},
            {"key": "PRG-18", "summary": "Moving map declutter", "type": "Feature", "state": "analysis_in_progress",
             "rank": "k", "cis": ["NAV-SW", "DISPLAY-SW"], "url": jira + "PRG-18"},
            {"key": "PRG-15", "summary": "CR-1234: heading drift after cold start", "type": "Change Request",
             "state": "done", "rank": "p", "cis": ["NAV-SW"], "url": jira + "PRG-15"},
        ],
    }

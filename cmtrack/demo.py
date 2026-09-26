"""Load a demo scenario through the HTTP API: python -m cmtrack.demo [--db demo.db]

NAV-SW syncs its releases from a (stand-in) Jira project; DISPLAY-SW and the composite SUITE are managed by
hand. A failed build and an ad hoc respin, patch and emergency releases that come from the source, a
build the source can't place, IFC-1's HSCM builds (a scraped Build 1 with placeholder CIs, a hand-made
Build 2 marked final) and IFC-2 spawned from it with a draft Build 1, and an emergency fix merged into the
next quarter (a two-parent version in the lineage DAG). Tickets aren't stored; ``demo_source`` serves Jira-style parent and CSC tickets for the demo.
"""
import argparse
import os

from . import create_app
from .releases import StaticVersionSource
from .tickets import StaticSource

LINE = r"(?P<line>\d{4}\.Q\d)"
NAV_PARAMS = {"project": "NAV", "patterns": {"planned": LINE, "build": LINE + r"-b(?P<n>\d+)",
                                             "patch": LINE + r"\.P(?P<n>\d+)", "emergency": LINE + r"\.ER(?P<n>\d+)"}}


def seed(client):
    def call(method, path, json=None, **kw):
        r = getattr(client, method)("/api" + path, json=json, **kw)
        if r.status_code >= 400:
            raise SystemExit(f"{method.upper()} {path} -> {r.status_code}: {r.get_json()}")
        return r.get_json()

    def version(release_id, name):
        return next(v["id"] for v in call("get", f"/releases/{release_id}")["versions"] if v["name"] == name)

    def release(ci, name):
        return next(r["id"] for r in call("get", f"/cis/{ci}/releases") if r["name"] == name)

    def ship(vid, statuses=("built", "tested")):
        for s in statuses:
            call("patch", f"/versions/{vid}", {"status": s})
        call("post", f"/versions/{vid}/release")

    # the demo's stand-in Jira project, unless the app was given a real release source called "jira"
    client.application.config["RELEASE_SOURCES"].setdefault("jira", demo_release_source())
    call("post", "/cis", {"name": "NAV-SW", "description": "Navigation software", "release_source": "jira",
                          "source_params": NAV_PARAMS})
    call("post", "/cis", {"name": "DISPLAY-SW", "description": "Cockpit display software", "require_tested": False})
    call("post", "/cis", {"name": "SUITE", "kind": "composite", "description": "Integrated mission suite"})
    call("post", "/cis/NAV-SW/cscs", {"name": "nav-core", "jira_project": "NAVL", "affected_product": "core",
                                      "team": "Nav"})
    call("post", "/cis/NAV-SW/cscs", {"name": "nav-maps", "jira_project": "NAVX", "affected_product": "maps",
                                      "team": "Maps"})
    call("post", "/cis/DISPLAY-SW/cscs", {"name": "hud", "jira_project": "DSP", "affected_product": "hud"})
    call("post", "/cis/NAV-SW/sync")
    for name, date in (("3.2.0", "2026-11-30"), ("3.3.0", "2027-05-31")):
        call("post", "/cis/DISPLAY-SW/releases", {"name": name, "target_date": date,
                                                  "builds": [f"{name}-rc1", name]})
    call("post", "/cis/SUITE/releases", {"name": "2026.Q4", "target_date": "2026-12-15", "builds": ["2026.Q4-b1"]})

    # NAV Q4: three builds, b3 fails, a b4 added by hand ships; the next sync gives the patch and emergency
    # (already in Jira) their base version
    q4 = release("NAV-SW", "2026.Q4")
    for n in ("2026.Q4-b1", "2026.Q4-b2", "2026.Q4-b3"):
        call("patch", f"/versions/{version(q4, n)}", {"status": "built"})
    call("patch", f"/versions/{version(q4, '2026.Q4-b3')}", {"status": "rejected"})
    b4 = call("post", f"/releases/{q4}/versions", {})
    ship(b4["id"])
    call("post", "/cis/NAV-SW/sync")
    er1 = release("NAV-SW", "2026.Q4.ER1")

    # DISPLAY 3.2.0 ('built' is enough for this CI), composite suite pins both
    ship(version(release("DISPLAY-SW", "3.2.0"), "3.2.0"), ("built",))
    sv = version(release("SUITE", "2026.Q4"), "2026.Q4-b1")
    call("patch", f"/versions/{sv}", {"status": "built"})
    call("patch", f"/versions/{sv}", {"status": "tested"})
    call("put", f"/versions/{sv}/manifest", {"children": [{"ci": "NAV-SW", "version": "2026.Q4-b4"},
                                                          {"ci": "DISPLAY-SW", "version": "3.2.0"}]})
    call("post", f"/versions/{sv}/release")

    # IFC-1: Build 1 is a scraped HSCM with placeholders, Build 2 adds the suite and is final.
    # IFC-2 spawns from IFC-1 Build 2; its Build 1 (a draft) adds the display.
    call("post", "/ifcs", {"name": "IFC-1", "description": "Initial navigation capability"})
    csv_text = "ci,version,type\nNAV-SW,2026.Q4-b4,CSCI\nRADAR-SW,7.4,\nANTENNA,Rev C,HWCI\n"
    call("post", "/ifcs/IFC-1/hscm?source_ref=DOC-001", data=csv_text, content_type="text/csv")
    b2 = call("post", "/ifcs/IFC-1/baselines", {})
    call("put", f"/baselines/{b2['id']}/entries/SUITE", {"version": str(sv)})
    call("post", f"/baselines/{b2['id']}/approve")
    call("post", "/ifcs/IFC-1/final")
    call("post", "/ifcs", {"name": "IFC-2", "spawned_from": b2["id"], "description": "Display increment"})
    d1 = call("post", "/ifcs/IFC-2/baselines", {})
    call("put", f"/baselines/{d1['id']}/entries/DISPLAY-SW", {"version": "3.2.0"})

    # ship the emergency after the baselines were approved, so both now field an older NAV version
    ship(version(er1, "2026.Q4.ER1"))
    # ...and fold it into the next quarter: 2027.Q1-b1 builds on Q4's release *and* the emergency fix
    q1 = release("NAV-SW", "2027.Q1")
    call("put", f"/versions/{version(q1, '2027.Q1-b1')}/parents", {"parents": ["2026.Q4-b4", "2026.Q4.ER1"]})

    # a backlog shared by the nav and display teams; filled from the ticket source when one is configured
    call("post", "/backlogs", {"name": "Nav & Display", "description": "Shared feature backlog",
                               "teams": ["Nav", "Maps", "Display"], "cis": ["NAV-SW", "DISPLAY-SW"]})
    if client.application.config.get("TICKET_SOURCES"):
        call("post", "/backlogs/Nav & Display/pull")
        call("post", "/backlogs/Nav & Display/items/PRG-20/move", {"before": "PRG-10"})      # to the top
        call("post", "/backlogs/Nav & Display/items/PRG-15/move", {"after": "PRG-22"})      # to the bottom


def _v(key, name, date=None, description=None):
    return {"key": key, "name": name, "date": date, "description": description}


# NAV's Jira project versions, as the Jira release source would list them. "2027.Q3-b1" has no release yet,
# so the sync reports it on the CI page.
NAV_VERSIONS = [
    _v("10001", "2026.Q4", "2026-12-15"),
    _v("10002", "2026.Q4-b1", "2026-10-15"), _v("10003", "2026.Q4-b2", "2026-11-15"),
    _v("10004", "2026.Q4-b3", "2026-12-15"),
    _v("10010", "2026.Q4.ER1", "2027-01-20", "CR-1234"), _v("10011", "2026.Q4.P1", "2027-02-10", "CR-1301"),
    _v("10020", "2027.Q1", "2027-03-15"),
    _v("10021", "2027.Q1-b1", "2027-01-15"), _v("10022", "2027.Q1-b2", "2027-02-15"),
    _v("10023", "2027.Q1-b3", "2027-03-15"),
    _v("10030", "2027.Q2", "2027-06-15"),
    _v("10031", "2027.Q2-b1", "2027-04-15"), _v("10032", "2027.Q2-b2", "2027-05-15"),
    _v("10033", "2027.Q2-b3", "2027-06-15"),
    _v("10041", "2027.Q3-b1", "2027-07-15"),
    _v("10099", "Backlog triage", None),
]


def demo_release_source():
    """Stand-in release source: CMTRACK_RELEASE_SOURCES=jira=cmtrack.demo:demo_release_source"""
    return StaticVersionSource({"NAV": NAV_VERSIONS}, name="jira")


def demo_source():
    """Stand-in ticket source for the demo: CMTRACK_TICKET_SOURCES=jira=cmtrack.demo:demo_source"""
    return StaticSource(DEMO_TICKETS, name="jira")


def _t(key, summary, parent=None, pair=None, versions=None, state="done", type="Story", status=None, reason=None,
       cis=None):
    project, product = pair or (None, None)
    return {"key": key, "summary": summary, "type": type, "parent_key": parent, "project": project,
            "affected_product": product, "fix_versions": versions, "state": state, "state_reason": reason, "cis": cis,
            "status": status or state.replace("_", " ").title(), "url": f"https://jira.example.com/browse/{key}"}


CORE, MAPS, HUD = ("NAVL", "core"), ("NAVX", "maps"), ("DSP", "hud")
DEMO_TICKETS = [
    _t("PRG-10", "GPS-denied navigation", type="Feature", state="in_progress", cis=["NAV-SW", "DISPLAY-SW"]),
    _t("PRG-12", "Terrain database refresh", type="Feature", state="verification", cis=["NAV-SW"]),
    _t("PRG-15", "CR-1234: heading drift after cold start", type="Change Request", cis=["NAV-SW"]),
    _t("PRG-18", "Moving map declutter", type="Feature", state="in_analysis", cis=["NAV-SW", "DISPLAY-SW"]),
    _t("PRG-20", "Weather radar overlay on the moving map", type="Feature", state="analysis_required",
       cis=["NAV-SW", "DISPLAY-SW"]),
    _t("PRG-21", "Night mode palette", type="Feature", state="ready_for_work", cis=["DISPLAY-SW"]),
    _t("PRG-22", "Route re-planning around restricted airspace", type="Feature", state="in_analysis",
       cis=["NAV-SW"]),
    _t("PRG-23", "Radar mode scheduling", type="Feature", state="analysis_required", cis=["RADAR-SW"]),
    _t("NAVL-101", "Inertial-only dead reckoning mode", "PRG-10", CORE, ["2026.Q4-b1"]),
    _t("NAVL-105", "Blend terrain-referenced fixes into the filter", "PRG-10", CORE, ["2027.Q1-b1"], "peer_review",
       status="In Review"),
    _t("NAVL-106", "Terrain fix quality gating", "PRG-10", CORE, ["2027.Q1-b2"], "blocked",
       status="Ready to Merge", reason="waiting on NAVX-205 (shared interface change) to merge first"),
    _t("NAVX-201", "Terrain correlation service", "PRG-10", MAPS, ["2026.Q4-b2"]),
    _t("NAVX-205", "Expose correlation quality in the terrain API", "PRG-10", MAPS, ["2027.Q1-b2"], "in_progress"),
    _t("DSP-31", "GPS-denied annunciator on PFD", "PRG-10", HUD, ["3.2.0"]),
    _t("NAVX-210", "Load 2026 terrain tiles", "PRG-12", MAPS, ["2026.Q4-b3"], "verification", status="In Test"),
    _t("NAVX-211", "Fix tile index overflow found in b3", "PRG-12", MAPS, ["2026.Q4-b4"], "verification",
       type="Bug", status="In Test"),
    _t("NAVL-120", "Re-seed heading from magnetometer on cold start", "PRG-15", CORE, ["2026.Q4.ER1"], type="Bug"),
    _t("NAVX-220", "Declutter levels for moving map", "PRG-18", MAPS, ["2027.Q1-b2"], "analysis_required",
       status="Open"),
    _t("NAVX-221", "Declutter preset storage", "PRG-18", MAPS, ["2027.Q1-b3"], "error", status="Closed",
       reason="closed as Done but its sub-task NAVX-222 is still open"),
    _t("DSP-40", "Declutter softkey", "PRG-18", HUD, ["3.3.0-rc1"], "ready_for_work", status="Ready"),
    _t("NAVL-130", "Log spam in nav filter", None, CORE, ["2027.Q1-b1"], type="Bug"),
]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=os.environ.get("CMTRACK_DB", "cmtrack.db"))
    args = parser.parse_args()
    if os.path.exists(args.db):
        raise SystemExit(f"{args.db} already exists; pick another --db or delete it")
    seed(create_app({"DATABASE": args.db, "TICKET_SOURCES": {"jira": demo_source()},
                     "RELEASE_SOURCES": {"jira": demo_release_source()}}).test_client())
    print(f"seeded {args.db}; run: CMTRACK_DB={args.db} CMTRACK_TICKET_SOURCES=jira=cmtrack.demo:demo_source "
          f"CMTRACK_RELEASE_SOURCES=jira=cmtrack.demo:demo_release_source python -m cmtrack")

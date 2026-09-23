"""Load a demo scenario through the HTTP API: python -m cmtrack.demo [--db demo.db]

Two planned CIs, a composite suite, a failed build and an ad hoc respin, patch and emergency
releases, an IFC hierarchy, a scraped HSCM with placeholder CIs and a hand-made successor
baseline, and Jira-style work items: parent tickets with CSC tickets under them, including an
emergency fix merged into the next quarter (a two-parent version in the lineage DAG).
Uses CMTRACK_POLICY_DIR (default ./policies) for the manual DISPLAY-SW plan.
"""
import argparse
import os

from . import create_app


def seed(client):
    def call(method, path, json=None, **kw):
        r = getattr(client, method)("/api" + path, json=json, **kw)
        if r.status_code >= 400:
            raise SystemExit(f"{method.upper()} {path} -> {r.status_code}: {r.get_json()}")
        return r.get_json()

    def version(release_id, name):
        return next(v["id"] for v in call("get", f"/releases/{release_id}")["versions"] if v["name"] == name)

    def ship(vid, statuses=("built", "tested")):
        for s in statuses:
            call("patch", f"/versions/{vid}", {"status": s})
        call("post", f"/versions/{vid}/release")

    call("post", "/policies", {"name": "quarterly-monthly", "type": "cadence"})
    call("post", "/policies", {"name": "display-manual", "type": "manual", "params": {"path": "display-sw.txt"}})
    call("post", "/cis", {"name": "NAV-SW", "policy": "quarterly-monthly", "description": "Navigation software"})
    call("post", "/cis", {"name": "DISPLAY-SW", "policy": "display-manual", "description": "Cockpit display software"})
    call("post", "/cis", {"name": "SUITE", "kind": "composite", "policy": "quarterly-monthly",
                          "description": "Integrated mission suite"})
    call("post", "/cis/NAV-SW/cscs", {"name": "nav-core", "jira_project": "NAVL", "affected_product": "core",
                                      "team": "Nav"})
    call("post", "/cis/NAV-SW/cscs", {"name": "nav-maps", "jira_project": "NAVX", "affected_product": "maps",
                                      "team": "Maps"})
    call("post", "/cis/DISPLAY-SW/cscs", {"name": "hud", "jira_project": "DSP", "affected_product": "hud"})
    for ci in ("NAV-SW", "SUITE"):
        call("post", f"/cis/{ci}/plan", {"start": "2026-10-01", "end": "2027-07-01"})
    call("post", "/cis/DISPLAY-SW/plan", {"start": "2026-10-01", "end": "2027-07-01"})

    # NAV Q4: three builds, b3 fails, ad hoc b4 ships; then an emergency (released) and an open patch
    q4 = call("get", "/cis/NAV-SW/releases")[0]["id"]
    for n in ("2026.Q4-b1", "2026.Q4-b2", "2026.Q4-b3"):
        call("patch", f"/versions/{version(q4, n)}", {"status": "built"})
    call("patch", f"/versions/{version(q4, '2026.Q4-b3')}", {"status": "rejected"})
    b4 = call("post", f"/releases/{q4}/versions", {})
    ship(b4["id"])
    er1 = call("post", f"/releases/{q4}/spawn", {"kind": "emergency", "reason": "CR-1234"})
    call("post", f"/releases/{q4}/spawn", {"kind": "patch", "reason": "CR-1301"})

    # DISPLAY 3.2.0 (manual policy: built is enough), composite suite pins both
    d32 = call("get", "/cis/DISPLAY-SW/releases")[0]["id"]
    ship(version(d32, "3.2.0"), ("built",))
    sq4 = call("get", "/cis/SUITE/releases")[0]["id"]
    sv = call("get", f"/releases/{sq4}")["versions"][-1]["id"]
    call("patch", f"/versions/{sv}", {"status": "built"})
    call("patch", f"/versions/{sv}", {"status": "tested"})
    call("put", f"/versions/{sv}/manifest", {"children": [{"ci": "NAV-SW", "version": "2026.Q4-b4"},
                                                          {"ci": "DISPLAY-SW", "version": "3.2.0"}]})
    call("post", f"/versions/{sv}/release")

    # IFCs: scraped HSCM-A with placeholders, then HSCM-B adds the suite
    call("post", "/ifcs", {"name": "IFC-2", "description": "Mission capability"})
    call("post", "/ifcs", {"name": "IFC-2.1", "parent": "IFC-2", "description": "Navigation increment"})
    call("post", "/ifcs", {"name": "IFC-2.2", "parent": "IFC-2", "description": "Display increment"})
    csv_text = "ci,version,type\nNAV-SW,2026.Q4-b4,CSCI\nRADAR-SW,7.4,\nANTENNA,Rev C,HWCI\n"
    a = call("post", "/ifcs/IFC-2.1/hscm?name=HSCM-A&source_ref=DOC-001", data=csv_text,
             content_type="text/csv")["baseline"]
    b = call("post", f"/baselines/{a['id']}/clone", {"name": "HSCM-B"})
    entries = [{"ci": e["ci"], "version": e["version"]} for e in b["entries"]] + [{"ci": "SUITE", "version": str(sv)}]
    call("put", f"/baselines/{b['id']}/entries", {"entries": entries})
    call("post", f"/baselines/{b['id']}/approve")
    call("post", "/ifcs/IFC-2.2/baselines", {"name": "HSCM-D1", "entries": [{"ci": "DISPLAY-SW", "version": "3.2.0"}]})

    # ship the emergency after the baselines were approved, so both now field an older NAV version
    ship(er1["versions"][0]["id"])
    # ...and fold it into the next quarter: 2027.Q1-b1 builds on Q4's release *and* the emergency fix
    q1 = next(r["id"] for r in call("get", "/cis/NAV-SW/releases") if r["name"] == "2027.Q1")
    call("put", f"/versions/{version(q1, '2027.Q1-b1')}/parents", {"parents": ["2026.Q4-b4", "2026.Q4.ER1"]})
    call("post", "/tickets", {"source": "jira", "records": DEMO_TICKETS})


def _t(key, summary, parent=None, pair=None, versions=None, cat="done", type="Story", status=None):
    project, product = pair or (None, None)
    return {"key": key, "summary": summary, "type": type, "parent_key": parent, "project": project,
            "affected_product": product, "fix_versions": versions, "status_category": cat,
            "status": status or {"done": "Done", "in_progress": "In Progress", "todo": "To Do"}[cat],
            "url": f"https://jira.example.com/browse/{key}"}


CORE, MAPS, HUD = ("NAVL", "core"), ("NAVX", "maps"), ("DSP", "hud")
DEMO_TICKETS = [
    _t("PRG-10", "GPS-denied navigation", type="Feature", cat="in_progress"),
    _t("PRG-12", "Terrain database refresh", type="Feature"),
    _t("PRG-15", "CR-1234: heading drift after cold start", type="Change Request"),
    _t("PRG-18", "Moving map declutter", type="Feature", cat="todo"),
    _t("NAVL-101", "Inertial-only dead reckoning mode", "PRG-10", CORE, ["2026.Q4-b1"]),
    _t("NAVL-105", "Blend terrain-referenced fixes into the filter", "PRG-10", CORE, ["2027.Q1-b1"], "in_progress"),
    _t("NAVX-201", "Terrain correlation service", "PRG-10", MAPS, ["2026.Q4-b2"]),
    _t("DSP-31", "GPS-denied annunciator on PFD", "PRG-10", HUD, ["3.2.0"]),
    _t("NAVX-210", "Load 2026 terrain tiles", "PRG-12", MAPS, ["2026.Q4-b3"]),
    _t("NAVX-211", "Fix tile index overflow found in b3", "PRG-12", MAPS, ["2026.Q4-b4"], type="Bug"),
    _t("NAVL-120", "Re-seed heading from magnetometer on cold start", "PRG-15", CORE, ["2026.Q4.ER1"], type="Bug"),
    _t("NAVX-220", "Declutter levels for moving map", "PRG-18", MAPS, ["2027.Q1-b2"], "todo"),
    _t("DSP-40", "Declutter softkey", "PRG-18", HUD, ["3.3.0-rc1"], "todo"),
    _t("NAVL-130", "Log spam in nav filter", None, CORE, ["2027.Q1-b1"], type="Bug"),
]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=os.environ.get("CMTRACK_DB", "cmtrack.db"))
    args = parser.parse_args()
    if os.path.exists(args.db):
        raise SystemExit(f"{args.db} already exists; pick another --db or delete it")
    seed(create_app({"DATABASE": args.db}).test_client())
    print(f"seeded {args.db}; run: CMTRACK_DB={args.db} python -m cmtrack")

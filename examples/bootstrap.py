"""Bootstrap example: configure cmtrack in code and create two CIs, each with a few CSCs.

    python examples/bootstrap.py            create what's missing (safe to run again: existing things are left alone)
    python examples/bootstrap.py serve      then run the app with the same configuration

Everything is set here, in code, instead of through CMTRACK_* environment variables:

    NAV-SW    releases synced from Jira: the versions of its CSCs' Jira projects, sorted by the version scheme below
    DISP-SW   releases kept in cmtrack (the "manual" release source), sorted by the same scheme

Both report tickets from the one Jira ticket source; a CI's CSCs map it onto Jira (project + Affected Product).
Copy this file and change the settings below for your program.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # run from a checkout without installing cmtrack

from cmtrack import create_app, manual_releases as manual, service as svc
from cmtrack.db import get_db
from cmtrack.jira_releases import JiraReleaseSource
from cmtrack.jira_tickets import JiraClient, JiraTicketSource

# ----------------------------------------------------------------------------- settings

DATABASE = "cmtrack.db"
JIRA_URL = "https://jira.example.com"
JIRA_TOKEN_FILE = Path.home() / ".cmtrack" / "jira-token"     # a personal access token, kept out of the repo

TICKETS = dict(
    top_projects=["PRG"],                                       # where features and discrepancies live
    top_types=["Feature", "Discrepancy"],
    fields={"parent": "Parent Ticket", "affected_product": "Affected Product", "cis": "Affected CIs"},
    status_map={"Awaiting CCB": "blocked"},
    roles={"analysis": {"label": "Analysis"}, "verification": {"summary": "VER:"}},
)

# Quarterly releases with monthly drops:
#   2026.01                  the quarter's release (only its last drop ships)
#   2026.01.01.00 .. 03.00   the three monthly drops, builds of 2026.01; 2026.01.01.01 is a patch build of drop 01
#   2026.01.ER01             an emergency release after the quarter shipped; its drops continue the count:
#                            drop 04 (= 3 + 1) is ER01's, 2026.01.04.00, 2026.01.04.01, ...; ER09's is drop 12
#   2026.01.00.00, 00.01     snapshot / one-off builds: tracked, but in no release
LINE = r"(?P<line>\d{4}\.\d{2})"
QUARTERLY = {
    "patterns": {"planned": LINE,
                 "build": LINE + r"\.(?P<n1>0[1-9]|[1-9]\d)\.(?P<n2>\d{2})",     # ordered by drop, then patch
                 "emergency": LINE + r"\.ER(?P<n>\d+)",
                 "snapshot": LINE + r"\.00\.(?P<n>\d{2})"},
    "child_builds": {"kind": "emergency", "group": "n1", "after": 3},      # drop 3 + k belongs to ERk
    "self_build": [],                                                    # an ER's version isn't one of its builds
}

CIS = [
    {"name": "NAV-SW", "description": "Navigation software",
     "release_source": "jira", "source_params": {**QUARTERLY, "match": r"\d{4}\.\d{2}(\.ER\d+|\.\d{2}\.\d{2})?"},
     "cscs": [{"name": "nav-core", "jira_project": "NAVL", "affected_product": "core", "team": "Nav"},
              {"name": "nav-maps", "jira_project": "NAVX", "affected_product": "maps", "team": "Maps"},
              {"name": "nav-io", "jira_project": "NAVL", "affected_product": "io", "team": "Nav"}]},
    {"name": "DISP-SW", "description": "Cockpit display software",
     "release_source": "manual", "source_params": QUARTERLY,
     "cscs": [{"name": "disp-render", "jira_project": "DSPL", "affected_product": "render", "team": "Display"},
              {"name": "disp-symbology", "jira_project": "DSPL", "affected_product": "symbology", "team": "Display"},
              {"name": "disp-input", "jira_project": "DSPI", "affected_product": "input", "team": "HMI"}],
     # the manual source's version list: releases and builds by name, sorted by the patterns above
     "versions": [{"name": "2026.01", "date": "2026-03-31"},
                  {"name": "2026.01.01.00", "date": "2026-01-30"},
                  {"name": "2026.01.01.01", "date": "2026-02-06"},
                  {"name": "2026.01.02.00", "date": "2026-02-27"},
                  {"name": "2026.01.03.00", "date": "2026-03-27"},
                  {"name": "2026.01.00.00", "date": "2026-02-12"}]},
]


# ----------------------------------------------------------------------------- the app

def jira_client():
    token = JIRA_TOKEN_FILE.read_text().strip() if JIRA_TOKEN_FILE.exists() else "set-me"
    return JiraClient(JIRA_URL, token=token)          # nothing is fetched until a page or sync asks Jira


def make_app():
    client = jira_client()
    return create_app({
        "DATABASE": DATABASE,
        "TICKET_SOURCES": {"jira": JiraTicketSource(client, **TICKETS)},
        "RELEASE_SOURCES": {"jira": JiraReleaseSource(client)},      # "manual" is always there
    })


# ----------------------------------------------------------------------------- bootstrap

def exists(conn, sql, *args):
    return conn.execute(sql, args).fetchone() is not None


def bootstrap(app, say=print):
    """Create the CIs, their CSCs and the manual versions that don't exist yet. Never changes existing ones."""
    with app.app_context():
        conn = get_db()
        with conn:                                    # one transaction: all of it, or nothing on an error
            for spec in CIS:
                name = spec["name"]
                if exists(conn, "SELECT 1 FROM ci WHERE name = ?", name):
                    say(f"CI {name}: exists, left as is")
                else:
                    svc.create_ci(conn, name, description=spec["description"], release_source=spec["release_source"],
                                  source_params=spec["source_params"])
                    say(f"CI {name}: created (releases from {spec['release_source']})")
                ci = svc.get_ci(conn, name)
                for c in spec["cscs"]:
                    if exists(conn, "SELECT 1 FROM csc WHERE ci_id = ? AND name = ?", ci["id"], c["name"]):
                        say(f"  CSC {c['name']}: exists")
                    elif exists(conn, "SELECT 1 FROM csc WHERE jira_project = ? AND affected_product = ?",
                                c["jira_project"], c["affected_product"]):
                        say(f"  CSC {c['name']}: skipped, {c['jira_project']}/{c['affected_product']} is already mapped")
                    else:
                        svc.add_csc(conn, name, c["name"], c["jira_project"], c["affected_product"], c.get("team"))
                        say(f"  CSC {c['name']}: created ({c['jira_project']}/{c['affected_product']})")
                added = 0
                for v in spec.get("versions", []):
                    if ci["release_source"] != manual.NAME:
                        say(f"  version {v['name']}: skipped, {name} doesn't use the manual release source")
                    elif exists(conn, "SELECT 1 FROM manual_version WHERE ci_id = ? AND name = ?", ci["id"], v["name"]):
                        say(f"  version {v['name']}: exists")
                    else:
                        manual.add_version(conn, name, v["name"], v.get("date"), v.get("description"))
                        say(f"  version {v['name']}: created")
                        added += 1
                if added:                             # sort the new versions into releases and builds
                    s = manual.sync(conn, name, app.config["RELEASE_SOURCES"])
                    say(f"  synced: {', '.join(s['created']) or 'nothing new'}")


def main():
    parser = argparse.ArgumentParser(description="cmtrack bootstrap example")
    parser.add_argument("command", nargs="?", choices=["seed", "serve"], default="seed")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    app = make_app()
    bootstrap(app)
    if args.command == "serve":
        app.run(port=args.port)


if __name__ == "__main__":
    main()

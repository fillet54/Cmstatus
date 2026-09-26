"""Years of history for trying the views at scale: IFCs and their HSCMs, and hand-managed CSCIs.

    python -m cmtrack.history load --db cmtrack.db [--reset]       # generate and load; tickets go to tickets.json
    python -m cmtrack.history load --url http://127.0.0.1:5000
    python -m cmtrack.history generate -o history.json [--seed 7]   # write it out instead (load history.json ...)
    CMTRACK_TICKET_SOURCES=jira=cmtrack.history:ticket_file python -m cmtrack   # serve the tickets

IFCs are a letter and a dotted version (IFC A 1.0, IFC A 1.0.2.1). A first child spawns from its parent
(1.0.2 -> 1.0.2.1) and each later sibling from the sibling before it (1.0.2.1 -> 1.0.2.2), from the source's
HSC at the time; the source can still get HSC fixes afterwards. Short versions (1.0, 2.1) have Build 1-3
before their HSCs; longer ones only have HSCs. HSC1 means complete; HSC1.1, HSC1.2 fix what was found after.

The managed CSCIs (ENGINE-SW, PORTAL-SW, LEDGER-SW) have no release source: quarterly releases entered by hand,
2-4 builds each (sometimes one rejected), the last tested and released at quarter end; emergency and patch
releases after some quarters, usually merged into the next quarter's first build (a two-parent version in the
lineage), sometimes a quarter later (unabsorbed until then); the current quarter part-built, the next ones planned.

Each managed CSCI has 1-5 CSCs, each with its own Jira project. Tickets: top-level FEAT (feature) and DR
(discrepancy) tickets, each split into CSC tickets in those projects and fixed in a build; a CSC ticket's state
follows its build, and a parent's is rolled up from its CSC tickets (tickets.rollup). cmtrack stores no tickets,
so ``load`` writes them to a file (``--tickets``, default tickets.json) that ``ticket_file`` serves as the ticket
source (``CMTRACK_TICKETS_FILE`` points it elsewhere).

``generate`` writes plain data (CIs, IFCs, dated HSCM events with their full CI -> version lists, and the CSCI
steps); ``load`` replays it through the HTTP API in date order, dating each HSCM by its document (``date``) and
backdating builds and releases, then marks the IFCs that are done final. ``--db`` runs the same calls in-process against a database file (a running
server sees the rows at once); ``--reset`` deletes that file first.
"""
import argparse
import datetime as dt
import json
import os
import random
import sys
import urllib.error
import urllib.parse
import urllib.request

from .tickets import StaticSource

CIS = {
    "A": [("CORE-SW", "CSCI"), ("DATA-SW", "CSCI"), ("UI-SW", "CSCI"), ("NET-SW", "CSCI"), ("SVC-SW", "CSCI"),
          ("API-SW", "CSCI"), ("TOOLS-SW", "CSCI"), ("PROC-HW", "HWCI"), ("DISPLAY-HW", "HWCI"), ("NET-HW", "HWCI")],
    "B": [("GATEWAY-SW", "CSCI"), ("SYNC-SW", "CSCI"), ("PLAN-SW", "CSCI"), ("UI-SW", "CSCI"), ("NET-SW", "CSCI"),
          ("SERVER-HW", "HWCI")],
}
LATE_CIS = {"IFC A 2.0": [("PLAN-SW", "CSCI")], "IFC B 1.1": [("REPORT-SW", "CSCI")]}   # capability added later

# (IFC, spawned from (IFC, HSCM) or None, builds before the HSCs, HSCs, how far it got)
# "how far": None = all of it; an int = only that many HSCMs, the last one left as a draft (still in progress).
TREE = [
    ("IFC A 1.0", None, 3, ["HSC1", "HSC1.1", "HSC1.2"], None),
    ("IFC A 1.1", ("IFC A 1.0", "HSC1"), 3, ["HSC1", "HSC1.1"], None),
    ("IFC A 1.0.1", ("IFC A 1.0", "HSC1.2"), 0, ["HSC1"], None),
    ("IFC A 1.0.2", ("IFC A 1.0.1", "HSC1"), 0, ["HSC1", "HSC1.1"], None),
    ("IFC A 1.2", ("IFC A 1.1", "HSC1.1"), 3, ["HSC1"], None),
    ("IFC A 1.0.2.1", ("IFC A 1.0.2", "HSC1"), 0, ["HSC1"], None),
    ("IFC A 1.1.1", ("IFC A 1.1", "HSC1.1"), 0, ["HSC1"], None),
    ("IFC A 1.0.2.2", ("IFC A 1.0.2.1", "HSC1"), 0, ["HSC1", "HSC1.1"], None),
    ("IFC A 2.0", ("IFC A 1.2", "HSC1"), 3, ["HSC1", "HSC1.1"], None),
    ("IFC A 1.1.2", ("IFC A 1.1.1", "HSC1"), 0, ["HSC1", "HSC1.1"], None),
    ("IFC A 1.2.1", ("IFC A 1.2", "HSC1"), 0, ["HSC1"], None),
    ("IFC A 1.0.2.3", ("IFC A 1.0.2.2", "HSC1.1"), 0, ["HSC1"], None),
    ("IFC A 2.0.1", ("IFC A 2.0", "HSC1"), 0, ["HSC1"], None),
    ("IFC A 2.1", ("IFC A 2.0", "HSC1.1"), 3, ["HSC1", "HSC1.1"], None),
    ("IFC A 1.1.2.1", ("IFC A 1.1.2", "HSC1.1"), 0, ["HSC1"], None),
    ("IFC A 2.0.1.1", ("IFC A 2.0.1", "HSC1"), 0, ["HSC1", "HSC1.1"], None),
    ("IFC A 2.1.1", ("IFC A 2.1", "HSC1.1"), 0, ["HSC1", "HSC1.1"], None),
    ("IFC A 2.2", ("IFC A 2.1", "HSC1"), 3, ["HSC1", "HSC1.1"], None),
    ("IFC A 2.1.1.1", ("IFC A 2.1.1", "HSC1"), 0, ["HSC1"], None),
    ("IFC A 2.2.1", ("IFC A 2.2", "HSC1"), 0, ["HSC1", "HSC1.1"], None),
    ("IFC A 3.0", ("IFC A 2.2", "HSC1.1"), 3, ["HSC1", "HSC1.1"], None),
    ("IFC A 2.2.2", ("IFC A 2.2.1", "HSC1.1"), 0, ["HSC1"], None),
    ("IFC A 2.2.2.1", ("IFC A 2.2.2", "HSC1"), 0, ["HSC1", "HSC1.1"], None),
    ("IFC A 3.0.1", ("IFC A 3.0", "HSC1"), 0, ["HSC1", "HSC1.1"], None),
    ("IFC A 3.1", ("IFC A 3.0", "HSC1.1"), 3, ["HSC1"], 2),
    ("IFC A 3.0.2", ("IFC A 3.0.1", "HSC1.1"), 0, ["HSC1"], 1),
    ("IFC B 1.0", None, 3, ["HSC1", "HSC1.1"], None),
    ("IFC B 1.1", ("IFC B 1.0", "HSC1"), 3, ["HSC1"], None),
    ("IFC B 1.0.1", ("IFC B 1.0", "HSC1.1"), 0, ["HSC1", "HSC1.1"], None),
    ("IFC B 1.1.1", ("IFC B 1.1", "HSC1"), 0, ["HSC1"], None),
    ("IFC B 2.0", ("IFC B 1.1", "HSC1"), 3, ["HSC1", "HSC1.1"], None),
    ("IFC B 2.0.1", ("IFC B 2.0", "HSC1"), 0, ["HSC1"], None),
    ("IFC B 2.1", ("IFC B 2.0", "HSC1.1"), 3, ["HSC1", "HSC1.1"], None),
    ("IFC B 2.1.1", ("IFC B 2.1", "HSC1"), 0, ["HSC1"], 1),
    ("IFC B 3.0", ("IFC B 2.1", "HSC1.1"), 3, ["HSC1"], 1),
]
STARTS = {"A": dt.date(2019, 3, 4), "B": dt.date(2021, 1, 11)}
MANAGED = [("ENGINE-SW", "Processing engine", dt.date(2022, 1, 1), "ENG"),
           ("PORTAL-SW", "User portal", dt.date(2023, 4, 1), "PRT"),
           ("LEDGER-SW", "Records service", dt.date(2024, 1, 1), "LDG")]
MANAGED_IN = {"A": ["ENGINE-SW", "PORTAL-SW"], "B": ["LEDGER-SW", "PORTAL-SW"]}   # which IFC lines field them


COMPONENTS = ["core", "io", "sched", "store", "api", "ui", "sync", "auth"]


def csci_steps(rng, today):
    """Dated API steps for the managed CSCIs' release history (see the module docstring)."""
    steps = []
    last_q = (today.year * 4 + (today.month - 1) // 3) + 3                # three quarters past today's
    for ci, description, start, project in MANAGED:
        at = lambda d: min(d, today).isoformat()
        steps.append({"at": at(start), "do": "ci", "ci": ci, "description": description,
                      "cscs": [[f"{ci.lower().removesuffix('-sw')}-{c}", f"{project}{c[:3].upper()}", ci, c.capitalize()]
                               for c in rng.sample(COMPONENTS, rng.randint(1, 5))]})
        fixes, head = [], None                                             # released fixes not yet merged
        for qi in range(start.year * 4 + (start.month - 1) // 3, last_q + 1):
            year, q = divmod(qi, 4)
            name, target = f"{year}.Q{q + 1}", dt.date(year, 3 * q + 3, 15)
            begin = max(start, target - dt.timedelta(days=100))            # builds start before the quarter does
            n = rng.randint(2, 4)
            builds = [f"{name}-b{i}" for i in range(1, n + 1)]
            steps.append({"at": at(begin), "do": "release", "ci": ci, "name": name, "kind": "planned",
                          "target_date": target.isoformat(), "builds": builds})
            reject = rng.randint(0, n - 2) if n >= 3 and rng.random() < 0.3 else None
            for i, b in enumerate(builds):
                built = begin + dt.timedelta(days=(target - begin).days * (i + 1) // (n + 1))
                if built > today:
                    break
                steps.append({"at": built.isoformat(), "do": "status", "ci": ci, "version": b, "status": "built"})
                if i == reject:
                    steps.append({"at": built.isoformat(), "do": "status", "ci": ci, "version": b, "status": "rejected"})
                if i == 0 and fixes and head and rng.random() < 0.75:          # fold the earlier fixes in
                    steps.append({"at": max([built] + [f[1] for f in fixes]).isoformat(), "do": "merge", "ci": ci,
                                  "version": b, "parents": [head] + [f[0] for f in fixes]})
                    fixes = []
            if target > today:
                continue
            steps.append({"at": target.isoformat(), "do": "status", "ci": ci, "version": builds[-1], "status": "tested"})
            steps.append({"at": target.isoformat(), "do": "release_version", "ci": ci, "version": builds[-1]})
            head = builds[-1]                    # fixes not merged yet carry over to the next quarter
            for kind, tag, chance, (lo, hi) in (("emergency", "ER", 0.4, (10, 30)), ("patch", "P", 0.25, (35, 60))):
                if rng.random() >= chance:
                    continue
                opened = target + dt.timedelta(days=rng.randint(lo, hi))
                fix = f"{name}.{tag}1"
                shipped = opened + dt.timedelta(days=rng.randint(7, 20))
                if opened > today:
                    continue
                steps.append({"at": opened.isoformat(), "do": "release", "ci": ci, "name": fix, "kind": kind,
                              "parent": name, "reason": f"CR-{rng.randint(1000, 1999)}"})
                steps.append({"at": (opened + dt.timedelta(days=3)).isoformat(), "do": "status", "ci": ci,
                              "version": fix, "status": "built"})
                if shipped <= today:
                    steps.append({"at": shipped.isoformat(), "do": "status", "ci": ci, "version": fix, "status": "tested"})
                    steps.append({"at": shipped.isoformat(), "do": "release_version", "ci": ci, "version": fix})
                    fixes.append((fix, shipped))
    return sorted(steps, key=lambda s: s["at"])


VERBS = ["Add", "Support", "Improve", "Rework", "Speed up", "Simplify"]
THINGS = ["data export", "audit trail", "session handling", "report filters", "bulk import", "retry logic",
          "config validation", "status dashboard", "search indexing", "access roles", "scheduling rules", "alerting"]
FAULTS = ["Crash in {}", "Wrong totals in {}", "Timeout during {}", "Memory growth in {}", "Stale data after {}"]


def csci_tickets(rng, steps, today):
    """Jira-style tickets for the managed CSCIs: top-level FEAT (feature) and DR (discrepancy) tickets, each
    split into CSC tickets in the CSCs' own projects, fixed in a build of the CSC's CSCI. A CSC ticket's state
    follows its build (released: done; built: in progress .. verification; planned: analysis .. ready, now
    and then blocked or cancelled). Parents carry no state: the ticket source rolls it up from their CSC tickets."""
    cscs, builds, status, fix_of, release_of, shipped = {}, {}, {}, {}, {}, set()
    for st in steps:
        ci = st["ci"]
        if st["do"] == "ci":
            cscs[ci] = [{"name": n, "project": p, "product": prod} for n, p, prod, _ in st["cscs"]]
        elif st["do"] == "release":
            names = st.get("builds") or [st["name"]]
            builds[(ci, st["name"])] = names
            status.update({(ci, b): ("planned", st.get("target_date") or st["at"]) for b in names})
            release_of.update({(ci, b): (ci, st["name"]) for b in names})
            if st["kind"] != "planned":
                fix_of[(ci, st["name"])] = st["kind"]
        elif st["do"] == "status":
            status[(ci, st["version"])] = (st["status"], status[(ci, st["version"])][1])
        elif st["do"] == "release_version":
            status[(ci, st["version"])] = ("released", status[(ci, st["version"])][1])
            shipped.add(release_of[(ci, st["version"])])

    counters = {}
    def key(project):
        counters[project] = counters.get(project, 0) + 1
        return f"{project}-{counters[project]}"

    def state(version):
        done, when = status[version]
        if release_of[version] in shipped:                     # its release shipped: the work is done
            return rng.choices(["done", "cancelled"], [30, 1])[0]
        if done in ("built", "tested"):
            return rng.choice(["in_progress", "peer_review", "verification", "done"])
        soon = (dt.date.fromisoformat(str(when)[:10]) - today).days < 120
        return rng.choice(["ready_for_work", "in_progress", "in_analysis", "blocked"] if soon
                          else ["analysis_required", "analysis_required", "in_analysis"])

    tickets = []
    def top(kind, summary, parts):
        """One FEAT/DR ticket and its CSC tickets: parts = [(ci, csc, fix version)]."""
        parent = key(kind)
        tickets.append({"key": parent, "summary": summary, "type": "Feature" if kind == "FEAT" else "Discrepancy",
                        "cis": sorted({ci for ci, _, _ in parts}), "url": f"https://jira.example.com/browse/{parent}"})
        kids = []
        for ci, csc, version in parts:
            k, st = key(csc["project"]), state((ci, version))
            kids.append(k)
            reason = f"Waiting on {rng.choice([x for x in kids if x != k] or [parent])}" if st == "blocked" else None
            tickets.append({"key": k, "summary": f"{summary} ({csc['name']})", "type": "Story" if kind == "FEAT" else "Bug",
                            "state": st, "state_reason": reason, "status": st.replace("_", " ").title(),
                            "parent_key": parent, "project": csc["project"], "affected_product": csc["product"],
                            "fix_versions": [version], "url": f"https://jira.example.com/browse/{k}"})

    for (ci, release), names in builds.items():
        usable = [b for b in names if status[(ci, b)][0] != "rejected"]
        if (ci, release) in fix_of:
            top("DR", rng.choice(FAULTS).format(rng.choice(THINGS)),
                [(ci, csc, usable[0]) for csc in rng.sample(cscs[ci], min(len(cscs[ci]), rng.randint(1, 2)))])
            continue
        for _ in range(rng.randint(2, 5)):
            kind = "FEAT" if rng.random() < 0.7 else "DR"
            summary = (f"{rng.choice(VERBS)} {rng.choice(THINGS)}" if kind == "FEAT"
                       else rng.choice(FAULTS).format(rng.choice(THINGS)))
            parts = [(ci, csc, rng.choice(usable)) for csc in rng.sample(cscs[ci], min(len(cscs[ci]), rng.randint(1, 3)))]
            other = [c for c in cscs if c != ci and (c, release) in builds]
            if other and rng.random() < 0.2:                      # sometimes another CSCI's CSC is in it too
                o = rng.choice(other)
                o_builds = [b for b in builds[(o, release)] if status[(o, b)][0] != "rejected"]
                parts.append((o, rng.choice(cscs[o]), rng.choice(o_builds)))
            top(kind, summary, parts)
    return tickets


def generate(seed=7, today=None):
    rng = random.Random(seed)
    today = today or dt.date.today()
    csci_rng = random.Random(seed + 1)                 # its own stream, so the IFC history doesn't shift with it
    steps = csci_steps(csci_rng, today)
    shipped = {}                                       # managed CSCI -> [(date, version)] as released
    for st in steps:
        if st["do"] == "release_version":
            shipped.setdefault(st["ci"], []).append((st["at"], st["version"]))
    counters, cis, ifcs, events = {}, {}, [], []
    dates, configs = {}, {}          # (ifc, hscm) -> date / {ci: version}

    def bump(ci, ctype, ifc):
        n = counters[ci] = counters.get(ci, 0) + 1
        if ctype == "HWCI":
            return "Rev " + (chr(64 + n) if n <= 26 else "A" + chr(64 + n - 26))
        major, minor = ifc.split()[2].split(".")[:2]
        return f"{major}.{minor}.{n}"

    for name, spawn, builds, hscs, got in TREE:
        letter = name.split()[1]
        if spawn and spawn not in dates:
            continue                                           # its spawn point would be in the future
        if spawn:
            config = dict(configs[spawn])
            start = dates[spawn] + dt.timedelta(days=rng.randint(21, 70))
        else:
            config = {}
            start = STARTS[letter]
        if start > today:
            continue
        for ci, ctype in CIS[letter] + LATE_CIS.get(name, []):
            cis[ci] = ctype
            if ci not in config:
                config[ci] = bump(ci, ctype, name)
        ifcs.append({"name": name, "spawned_from": {"ifc": spawn[0], "hscm": spawn[1]} if spawn else None,
                     "description": f"System Increment {letter} {name.split()[2]}"})
        events.append({"at": (start - dt.timedelta(days=1)).isoformat(), "type": "ifc", "ifc": name})

        names = [f"Build {i}" for i in range(1, builds + 1)] + hscs
        if got is not None:
            names = names[:got]
        when = start
        for i, hscm in enumerate(names):
            if i:
                when += dt.timedelta(days=rng.randint(50, 120) if hscm.startswith("Build") or "." not in hscm[3:]
                                     else rng.randint(80, 240))
            fix = hscm.startswith("HSC") and "." in hscm[3:]
            changes = 1 if fix else rng.randint(1, 2) if hscm.startswith("HSC") else rng.randint(2, 4)
            for ci in rng.sample(sorted(config), changes):
                if cis[ci] == "HWCI" and rng.random() > 0.15:
                    continue                                   # hardware changes rarely
                config[ci] = bump(ci, cis[ci], name)
            draft = got is not None and i == len(names) - 1
            if when > today:
                break
            dates[(name, hscm)], configs[(name, hscm)] = when, dict(config)
            fielded = {ci: [v for at, v in shipped.get(ci, []) if at <= when.isoformat()] for ci in MANAGED_IN[letter]}
            events.append({"at": when.isoformat(), "type": "hscm", "ifc": name, "name": hscm, "approve": not draft,
                           "source_ref": f"HSCM-{name.split()[1]}{name.split()[2]}-{hscm.replace(' ', '')}",
                           "entries": dict(sorted({**config, **{ci: vs[-1] for ci, vs in fielded.items() if vs}}.items()))})

    events.sort(key=lambda e: (e["at"], e["type"] != "ifc"))
    open_ifcs = {name for name, *_, got in TREE if got is not None}
    last = {}
    for e in events:
        if e["type"] == "hscm":
            last[e["ifc"]] = e["at"]
    final = [i["name"] for i in ifcs if i["name"] not in open_ifcs and i["name"] in last
             and dt.date.fromisoformat(last[i["name"]]) < today - dt.timedelta(days=120)]
    cis.update({ci: "CSCI" for ci, *_ in MANAGED})
    return {"description": __doc__.splitlines()[0], "seed": seed, "generated": today.isoformat(),
            "cis": [{"name": k, "type": v} for k, v in sorted(cis.items())],
            "ifcs": ifcs, "events": events, "final": final, "cscis": steps, "tickets": csci_tickets(csci_rng, steps, today)}


# ----------------------------------------------------------------------------- loading

class Http:
    def __init__(self, url):
        self.url = url.rstrip("/") + "/api"

    def __call__(self, method, path, body=None):
        req = urllib.request.Request(self.url + urllib.parse.quote(path), method=method.upper(),
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return json.loads(r.read() or "null")
        except urllib.error.HTTPError as e:
            raise SystemExit(f"{method.upper()} {path}: {e.code} {e.read().decode()[:500]}") from None


class InProcess:
    def __init__(self, db):
        from . import create_app
        self.client = create_app({"DATABASE": db}).test_client()

    def __call__(self, method, path, body=None):
        r = getattr(self.client, method)("/api" + urllib.parse.quote(path), json=body)
        if r.status_code >= 400:
            raise SystemExit(f"{method.upper()} {path}: {r.status_code} {r.get_data(as_text=True)[:500]}")
        return r.get_json()


def load(data, call, out=print):
    """Replay a generated history: the managed CSCIs first (the HSCMs list their versions), then the IFCs."""
    versions = {}                                    # (ci, version name) -> id
    for st in data.get("cscis", []):
        ci, when = st["ci"], st["at"] + "T12:00:00+00:00"
        if st["do"] == "ci":
            call("post", "/cis", {"name": ci, "description": st["description"]})
            for name, project, product, team in st["cscs"]:
                call("post", f"/cis/{ci}/cscs", {"name": name, "jira_project": project, "affected_product": product,
                                                 "team": team})
        elif st["do"] == "release":
            rel = call("post", f"/cis/{ci}/releases", {k: st[k] for k in ("name", "kind", "target_date", "parent",
                                                                          "reason", "builds") if k in st})
            versions.update({(ci, v["name"]): v["id"] for v in rel["versions"]})
        elif st["do"] == "status":
            call("patch", f"/versions/{versions[(ci, st['version'])]}",
                 {"status": st["status"], **({"built_at": when} if st["status"] == "built" else {})})
        elif st["do"] == "release_version":
            call("post", f"/versions/{versions[(ci, st['version'])]}/release", {"released_at": when})
        elif st["do"] == "merge":
            call("put", f"/versions/{versions[(ci, st['version'])]}/parents", {"parents": st["parents"]})
    types = {c["name"]: c["type"] for c in data["cis"]}
    descriptions = {i["name"]: i for i in data["ifcs"]}
    ids = {}                                         # (ifc, hscm) -> baseline id
    for e in data["events"]:
        if e["type"] == "ifc":
            spec = descriptions[e["ifc"]]
            src = spec["spawned_from"]
            call("post", "/ifcs", {"name": e["ifc"], "description": spec["description"],
                                   "spawned_from": ids[(src["ifc"], src["hscm"])] if src else None})
        else:
            rows = [{"ci": ci, "version": v, "type": types.get(ci, "CSCI")} for ci, v in e["entries"].items()]
            out_ = call("post", f"/ifcs/{e['ifc']}/hscm", {"name": e["name"], "rows": rows, "approve": e["approve"],
                                                           "source_ref": e["source_ref"],
                                                           "date": e["at"]})
            ids[(e["ifc"], e["name"])] = out_["baseline"]["id"]
    for name in data["final"]:
        call("post", f"/ifcs/{name}/final")
    hscms = sum(e["type"] == "hscm" for e in data["events"])
    cscis = sum(st["do"] == "ci" for st in data.get("cscis", []))
    out(f"loaded {len(data['ifcs'])} IFCs, {hscms} HSCMs, {len(data['final'])} final; {cscis} managed CSCIs")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m cmtrack.history", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate", help="write the history as JSON")
    g.add_argument("-o", "--out", default="ifc_history.json")
    g.add_argument("--seed", type=int, default=7)
    lo = sub.add_parser("load", help="load a history file into an instance")
    lo.add_argument("file", nargs="?", help="a generated history (default: generate one now)")
    lo.add_argument("--seed", type=int, default=7)
    where = lo.add_mutually_exclusive_group(required=True)
    where.add_argument("--url", help="a running instance, e.g. http://127.0.0.1:5000")
    where.add_argument("--db", help="a database file, loaded in-process")
    lo.add_argument("--reset", action="store_true", help="first delete the database file (needs --db)")
    lo.add_argument("--tickets", default="tickets.json", help="where to write the tickets (default tickets.json)")
    a = ap.parse_args(argv)

    if a.cmd == "generate":
        data = generate(a.seed)
        os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
        with open(a.out, "w") as f:
            json.dump(data, f, indent=1)
            f.write("\n")
        print(f"wrote {a.out}: {len(data['ifcs'])} IFCs, {sum(e['type'] == 'hscm' for e in data['events'])} HSCMs")
        return
    if a.reset and not a.db:
        ap.error("--reset needs --db")
    if a.file:
        with open(a.file) as f:
            data = json.load(f)
    else:
        data = generate(a.seed)
    if a.reset and os.path.exists(a.db):
        os.remove(a.db)
    load(data, InProcess(a.db) if a.db else Http(a.url))
    with open(a.tickets, "w") as f:
        json.dump(data.get("tickets", []), f, indent=1)
    print(f"wrote {len(data.get('tickets', []))} tickets to {a.tickets}; serve them with "
          f"CMTRACK_TICKET_SOURCES=jira=cmtrack.history:ticket_file")


def ticket_file():
    """A ticket source serving the tickets ``load`` wrote (CMTRACK_TICKETS_FILE, default tickets.json)."""
    with open(os.environ.get("CMTRACK_TICKETS_FILE", "tickets.json")) as f:
        return StaticSource(json.load(f), name="jira")


if __name__ == "__main__":
    sys.exit(main())

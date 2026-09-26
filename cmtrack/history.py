"""Years of IFC / HSCM history for trying the IFC views at scale.

    python -m cmtrack.history load --db cmtrack.db [--reset]       # generate and load
    python -m cmtrack.history load --url http://127.0.0.1:5000
    python -m cmtrack.history generate -o history.json [--seed 7]   # write it out instead (load history.json ...)

IFCs are a letter and a dotted version (IFC A 1.0, IFC A 1.0.2.1). A first child spawns from its parent
(1.0.2 -> 1.0.2.1) and each later sibling from the sibling before it (1.0.2.1 -> 1.0.2.2), from the source's
HSC at the time; the source can still get HSC fixes afterwards. Short versions (1.0, 2.1) have Build 1-3
before their HSCs; longer ones only have HSCs. HSC1 means complete; HSC1.1, HSC1.2 fix what was found after.

``generate`` writes plain data (CIs, IFCs, and dated HSCM events with their full CI -> version lists);
``load`` replays it through the HTTP API in date order, dating each HSCM by its document (``date``), then marks
the IFCs that are done final. ``--db`` runs the same calls in-process against a database file (a running
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


def generate(seed=7, today=None):
    rng = random.Random(seed)
    today = today or dt.date.today()
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
            events.append({"at": when.isoformat(), "type": "hscm", "ifc": name, "name": hscm, "approve": not draft,
                           "source_ref": f"HSCM-{name.split()[1]}{name.split()[2]}-{hscm.replace(' ', '')}",
                           "entries": dict(sorted(config.items()))})

    events.sort(key=lambda e: (e["at"], e["type"] != "ifc"))
    open_ifcs = {name for name, *_, got in TREE if got is not None}
    last = {}
    for e in events:
        if e["type"] == "hscm":
            last[e["ifc"]] = e["at"]
    final = [i["name"] for i in ifcs if i["name"] not in open_ifcs and i["name"] in last
             and dt.date.fromisoformat(last[i["name"]]) < today - dt.timedelta(days=120)]
    return {"description": __doc__.splitlines()[0], "seed": seed, "generated": today.isoformat(),
            "cis": [{"name": k, "type": v} for k, v in sorted(cis.items())],
            "ifcs": ifcs, "events": events, "final": final}


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
    out(f"loaded {len(data['ifcs'])} IFCs, {hscms} HSCMs, {len(data['final'])} final")


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


if __name__ == "__main__":
    sys.exit(main())

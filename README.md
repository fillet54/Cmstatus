# cmtrack

Configuration tracking for software and hardware configuration items (CSCIs / HWCIs): their releases and builds,
what each build was built from, which tickets went into it, and which versions each capability increment (IFC)
fields in its HSCMs.

- **Releases and builds** come from Jira (a *release source*) or are entered by hand. Patch and emergency releases
  branch off a quarter's release; merging a fix back in is recorded, so "what's new since X" is always right.
- **Tickets stay in Jira** (the *ticket source*); cmtrack asks for them live and groups them: feature / discrepancy
  ticket → each CSC's ticket → the builds it's fixed in.
- **IFCs** spawn from one another; each has a sequence of HSCM builds (Build 1, 2, … then HSC1, HSC1.1, …).
  Timelines show all of it by date.

Python 3.10+, Flask and SQLite. Nothing else to install or run: no database server, no build step.

## Try it

```bash
git clone git@github.com:fillet54/cmstatus.git && cd cmstatus
python -m venv .venv && . .venv/bin/activate
pip install -e .
```

Then load some data. Pick one:

**A. Years of sample history** (the fuller picture): 33 IFCs with ~85 HSCMs since 2019, three hand-managed CSCIs
with quarterly releases, fixes and merges, 1–5 CSCs each, and ~600 feature/discrepancy tickets.

```bash
python -m cmtrack.history load --db cmtrack.db --reset     # writes cmtrack.db and tickets.json
CMTRACK_TICKET_SOURCES=jira=cmtrack.history:ticket_file cmtrack
```

**B. The small demo**: a few CIs, one of them synced from a stand-in Jira project, a handful of tickets and a
shared backlog. Good for seeing sync, remap and backlogs.

```bash
python -m cmtrack.demo --db demo.db
CMTRACK_DB=demo.db CMTRACK_TICKET_SOURCES=jira=cmtrack.demo:demo_source \
  CMTRACK_RELEASE_SOURCES=jira=cmtrack.demo:demo_release_source cmtrack
```

Open <http://127.0.0.1:5000>. To start over, run the load again (`--reset` deletes the database first).

## A quick tour (with the sample history)

| Where | What to look at |
|---|---|
| **Overview** (`/`) | The IFC timeline: every IFC's HSCMs by date, new IFCs branching off the HSCM they spawned from. Zoom from years to weeks; it opens on today. |
| **Configuration items** → **ENGINE-SW** | Releases, latest first (click a column to sort; fixes stay under their release). Open **Lineage** for every build on a time axis, with fixes branching off and merging back. |
| A release, e.g. **2026.Q3** | Its builds, what it was built from, HSCMs still fielding an older version, and **Tickets and comparisons**: the tickets that went into it. Change **From** / **To** to compare any two versions, releases or HSCMs (type to search); filter by CSC, feature vs discrepancy, or state. The URL keeps all of it, so the link can be shared. |
| A ticket, e.g. **FEAT-108** | How each CSC implemented it and the builds it's fixed in. |
| **Capabilities** | The IFCs, what each spawned from, its builds and whether it's final. Open one to start a build, import an HSCM CSV, or mark it final. |
| **Backlogs** (demo) | A ranked backlog shared by several teams; drag to reorder. |
| **`/ui`** | The component library every page is built from. |

## Settings

Environment variables, all optional:

| Variable | Default | |
|---|---|---|
| `CMTRACK_DB` | `cmtrack.db` | SQLite file. `schema.sql` is the whole schema; after a schema change, delete and reload. |
| `CMTRACK_TICKET_SOURCES` | none | `name=module:factory[,…]`: where tickets come from (e.g. your Jira client). |
| `CMTRACK_RELEASE_SOURCES` | none | Same, for releases and builds. |
| `CMTRACK_TICKETS_FILE` | `tickets.json` | The file `cmtrack.history:ticket_file` serves. |
| `CMTRACK_MARKING`, `CMTRACK_PROGRAM` | unset | The marking banners and program name in the page header. |
| `CMTRACK_UI_FONTS_CSS`, `CMTRACK_UI_HTMX_JS` | CDN URLs | Point at self-hosted copies on a closed network. |

`cmtrack --port 8000 --host 0.0.0.0 --debug` changes where the dev server listens.

## Connecting your own Jira

Implement a `TicketSource` (and optionally a `ReleaseSource`) around your Jira client and name it in the
variables above. The docstrings at the top of [`cmtrack/tickets.py`](cmtrack/tickets.py) and
[`cmtrack/releases.py`](cmtrack/releases.py) are the templates; [docs/reference.md](docs/reference.md#work-items-tickets)
covers what cmtrack expects of them.

## Development

```bash
python -m unittest discover -s tests
```

- [docs/reference.md](docs/reference.md): the data model and its rules, lineage and ranges, tickets, release
  sources, the HTTP API (`/api`, also listed at `GET /api/`), the pages and the UI component library.
- [docs/ontology.md](docs/ontology.md): what the terms mean, lined up with EIA-649, OSLC and PROV-O.
- [plan.md](plan.md): the order to build it in by hand, phase by phase.

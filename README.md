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

Python 3.10+, Flask, requests (for Jira) and SQLite. Nothing else to install or run: no database server, no build step.

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

## Backups

cmtrack can copy its SQLite database to a Nexus **raw** repository (or a directory). Each backup is a consistent
snapshot, taken safely while the app is writing, checked with SQLite's integrity check, then gzipped and uploaded
as `{CMTRACK_BACKUP_URL}/{prefix}/cmtrack-<UTC time>.db.gz`. Every run shows in the audit log. To keep only the
last N backups, set a cleanup policy on the Nexus repository.

| Variable | |
|---|---|
| `CMTRACK_BACKUP_URL` | a Nexus raw repository, e.g. `https://nexus.example/repository/cmtrack-backups`, or a directory |
| `CMTRACK_BACKUP_USER` + `CMTRACK_BACKUP_PASSWORD`, or `CMTRACK_BACKUP_BEARER` | Nexus credentials (Basic, or a user token as Bearer) |
| `CMTRACK_BACKUP_PREFIX` | folder inside the repository (default `cmtrack`) |
| `CMTRACK_BACKUP_TOKEN` | turns on `POST` / `GET /api/admin/backup`, which need `Authorization: Bearer <token>` |
| `CMTRACK_BACKUP_DAILY_AT` | `HH:MM` (UTC): back up once a day from inside the app |

Pick one way to schedule it (both can be on):

- **A scheduled GitLab pipeline** calls the endpoint. The job only needs curl, since the database never leaves
  the server:
  ```yaml
  backup-cmtrack:
    rules: [{ if: $CI_PIPELINE_SOURCE == "schedule" }]
    script:
      - curl -fsS -X POST -H "Authorization: Bearer $CMTRACK_BACKUP_TOKEN" https://cmtrack.example/api/admin/backup
  ```
  The job fails if the upload does (the endpoint answers 502), so GitLab tells you.
- **Inside the app**: set `CMTRACK_BACKUP_DAILY_AT=02:00`. A background thread checks every five minutes and backs
  up once a day after that time, straight away if the day's run was missed because the server was down. With
  several worker processes each one runs the timer, but each backup is claimed in the database first, so only
  one of them makes it. A failed run is retried an hour later.

By hand on the server: `python -m cmtrack.backup` (uses `CMTRACK_DB` and the settings above). To restore, run
`python -m cmtrack.backup --restore <backup URL or file> new.db`. It downloads the backup, checks it and writes
`new.db` (never over an existing file); then point `CMTRACK_DB` at it.

## Connecting your own Jira

Start from [`cmtrack/jira_tickets.py`](cmtrack/jira_tickets.py), a working Jira ticket source. It has a REST
client (on `requests`), a registry that lets you name custom fields ("Parent Ticket") instead of
`customfield_12345`, and helpers that pull plain values out of the API's responses. It's set up for top-level
feature and discrepancy projects plus CSCI projects whose tickets point at their parent through a custom field.
Configure it in code or with `CMTRACK_TICKET_SOURCES=jira=cmtrack.jira_tickets:from_env` (settings in the module
docstring). A ticket's state comes from a *state rule* you register, a function that sees one ticket (status,
labels, any registered field); top-level tickets then go through a *rollup* over their CSC tickets (in a CI's work report, and in
`top_level_tickets_for_versions`, only the CSC tickets of the CSCs you're looking at). `analysis_rule` is an example, with an "Analysis State" field on features and an "Analysis" label on CSC tickets.

Or implement a `TicketSource` (and optionally a `ReleaseSource`) around your own Jira client and name it in the
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

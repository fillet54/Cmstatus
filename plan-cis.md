# cmtrack: typing plan for the CI pages (capabilities stubbed)

The shortest path from an empty directory to **finished CI pages**: the CI list, a CI's overview, its releases
and builds (synced from Jira or kept by hand), every build's lineage, and the tickets each CSC implemented, with
**capabilities (IFCs / HSCMs) present but empty**. About 6,700 lines to type, 690 of them the Jira modules.

[`plan.md`](plan.md) is the full rebuild in feature order. This one is narrower and stubs far less: almost every
file is typed exactly as it is on `main`, and whole line ranges are left out. Line numbers are those of `main` at
commit `58d77f1`.

This cut was built and run from `main`: every page under "Working when" below returned 200 with the dummy sources,
and the only tests that fail are the ones that assert on capabilities, the backlog, the overview page or the style
guide (listed in [Tests](#tests)).

## How capabilities are stubbed

Three decisions keep the stubbing to two small functions and one small template:

1. **Type `schema.sql` in full**, capability tables included (`ifc`, `baseline`, `baseline_entry`: 34 lines). They
   stay empty. Every query on the CI pages that joins them ("Fielded in", baselines behind, where used, the HSCM
   choices of the ticket range picker) then runs unchanged and returns nothing, so none of that code is stubbed,
   and there is no schema change or database reload when capabilities arrive.
2. **Type the CI templates as they are.** Their links to capability pages (`url_for('ui.ifc')`,
   `url_for('ui.baseline')`) sit inside loops over those empty results, so they are never evaluated.
3. **Stub the two pages other pages link to**: `/` (the error page's "Overview" button) and `/ifcs` (the
   "Capabilities" nav item). Code in [Stage 5](#stage-5-the-ci-pages-1466-lines).

Left out entirely, to type later: the IFC and baseline code, the overview dashboard, the audit log page, the style
guide, the backlog package, backups and the history generator. The nav skips items whose page doesn't exist, so
Backlogs and Audit log simply don't show.

## Before you start

- Python 3.10+, `pip install flask requests` (`requests` is only needed from Stage 7).
- The pages need htmx. On a closed network, self-host it and set `CMTRACK_UI_HTMX_JS` (and `CMTRACK_UI_FONTS_CSS`,
  or set it to `""` for system fonts).
- One commit per stage, after its check passes.

```bash
api() { m=$1; p=$2; shift 2; curl -s -X "$m" "localhost:5000/api$p" -H 'Content-Type: application/json' "$@"; echo; }
```

---

## Stage 1: leaf modules (863 lines)

Nothing here imports Flask routes or each other, apart from `db.py` reading `schema.sql`.

| File | Lines | Type |
|---|---|---|
| `cmtrack/schema.sql` | 175 | all of it |
| `cmtrack/db.py` | 31 | all of it |
| `cmtrack/graph.py` | 141 | all of it |
| `cmtrack/releases.py` | 272 | all of it |
| `cmtrack/tickets.py` | 244 | all of it |
| `cmtrack/__init__.py` | 0 | an empty file for now |
| `.gitignore` | | `__pycache__/`, `*.pyc`, `*.db` |

**Working when**

```bash
python -c "from cmtrack import db, graph, releases, tickets; c = db.connect(':memory:'); db.init_db(c); print('ok')"
```

## Stage 2: the domain logic (1,426 lines)

| File | Type | Leave out |
|---|---|---|
| `cmtrack/service.py` | lines 1–1247 and 1637–1817 | 1248–1636: the "IFCs" and "baselines (HSCM builds)" sections, through `import_hscm` |

Everything kept is typed as is, including the parts that read capability tables (`behind_effective`, `where_used`,
`_in_use`) and the composites section. The `csv` and `io` imports go unused; leave them.

**Working when**

```bash
python - <<'EOF'
from cmtrack import db, service as svc
conn = db.connect(":memory:"); db.init_db(conn)
svc.create_ci(conn, "NAV-SW")
svc.add_csc(conn, "NAV-SW", "nav-core", "NAVL", "core", "Nav")
svc.create_release(conn, "NAV-SW", name="2026.Q4", builds=["2026.Q4-b1", "2026.Q4-b2"])
print([r["name"] for r in svc.list_releases(conn, "NAV-SW")], [v["name"] for v in svc.ci_versions(conn, "NAV-SW")])
EOF
# ['2026.Q4'] ['2026.Q4-b1', '2026.Q4-b2']
```

## Stage 3: the API and the manual release source (563 lines)

| File | Lines | Type | Leave out |
|---|---|---|---|
| `cmtrack/manual_releases.py` | 145 | all of it | |
| `cmtrack/api.py` | 349 | lines 1–305 and 424–467 | 306–423: "IFCs & baselines" |
| `cmtrack/__init__.py` | 52 | all of it, less the lines on the right | `backlog` in the import on line 7; `backlog.init_db(conn)` (29); `backlog.init_app(app)` (38); the two `backup` lines (39–40) |
| `cmtrack/__main__.py` | 17 | all of it | |

Until Stage 5, also hold back the three UI lines of `create_app` (`ui.init_app(app)`, the `views` import and
`app.register_blueprint(ui_bp)`), the `ui` name in the line 7 import, and the `render_template("error.html")`
branch of the `CMError` handler.

**Working when** (`python -m cmtrack` in another terminal)

```bash
api POST /cis -d '{"name": "DISP-SW", "release_source": "manual", "source_params": {"patterns": {"planned": "(?P<line>\\d{4}\\.Q\\d)", "build": "(?P<line>\\d{4}\\.Q\\d)-b(?P<n>\\d+)"}}}'
api POST /cis/DISP-SW/cscs -d '{"name": "disp-render", "jira_project": "DSPL", "affected_product": "render", "team": "Display"}'
api POST /cis/DISP-SW/manual-versions -d '{"name": "2026.Q4", "date": "2026-12-15"}'
api POST /cis/DISP-SW/manual-versions -d '{"name": "2026.Q4-b1", "date": "2026-11-01"}'
api GET  /cis/DISP-SW/releases          # one planned release, 2026.Q4, with planned_versions: 1
```

Each manual-version change syncs the CI, which is what turns the names into a release and its build. A CI on a
Jira source gets its releases only when you sync it (`POST /cis/<ci>/sync`): the pages always read the stored
releases, never the source.

## Stage 4: the UI shell (1,513 lines)

Nothing to check until Stage 5; these are what every page is built from.

| File | Lines | Type | Leave out |
|---|---|---|---|
| `cmtrack/static/ui.css` | 525 | all of it | |
| `cmtrack/templates/ui/components.html` | 607 | all of it | |
| `cmtrack/templates/ui/layout.html` | 40 | all of it | |
| `cmtrack/ui.py` | 142 | lines 1–142 | 144–180: `styleguide_samples` |
| `cmtrack/templates/error.html` | 10 | all of it | |
| `cmtrack/static/timeline.js` | 30 | all of it | |
| `cmtrack/static/picker.js` | 159 | all of it | |

Leave `NAV` in `ui.py` as it is.

## Stage 5: the CI pages (1,466 lines)

`cmtrack/views.py` (590 lines): type it as is, apart from these.

| Lines on `main` | What | Do |
|---|---|---|
| 14 | `from .backlog import service as backlogs` | leave out |
| 58–65 | `with_staleness` | leave out |
| 128–160 | the dashboard section (`dashboard`, `recent_events`) | replace with the two stubs below |
| 214 | `backlogs=backlogs.list_backlogs(conn, detail["id"]),` in `render_ci` | leave out that argument |
| 369 | `BASELINE_DOTS` | leave out |
| 382–404 | `ifc_timeline`, `ifc_timeline_fragment` | leave out |
| 501–594 | `spawn_options` through `styleguide` (the IFC and baseline pages, events, style guide) | leave out |
| 746–819 | "IFC & baseline forms" | leave out |

Careful with the section headed "IFCs & baselines" at line 367: `VERSION_DOTS`, `zoomed_timeline`, `ci_timeline`,
`ci_lineage`, `range_timeline` and `work_lineage` live under it and **are needed**.

The stubs, in place of the dashboard section:

```python
@bp.get("/")
def dashboard():                                     # TODO capabilities: the real overview page
    return redirect(url_for("ui.cis"))


@bp.get("/ifcs")
def ifcs():                                          # TODO capabilities: the real IFC list
    return render_template("ifcs.html", ifcs=[])
```

and `cmtrack/templates/ifcs.html`, a stand-in for the real one:

```jinja
{% extends "ui/layout.html" %}
{% import "ui/components.html" as ui %}
{% set nav_current = "ifcs" %}
{% block title %}Capabilities{% endblock %}
{% block content %}
{{ ui.page_header("Capabilities (IFCs)", "Each IFC's HSCM builds, and the IFCs they spawned") }}
{% call ui.card() %}{{ ui.empty("No capabilities yet.") }}{% endcall %}
{% endblock %}
```

Templates, all typed as they are except one line of `ci.html`:

| File | Lines | |
|---|---|---|
| `cis.html`, `_ci_rows.html` | 22, 18 | the CI list and its filtered rows |
| `ci.html` | 164 | leave out line 158, `{% include "backlog/_ci_card.html" %}` |
| `_release_table.html` | 49 | |
| `_attention.html`, `_sync_summary.html` | 48, 34 | |
| `_manual_versions.html` | 55 | |
| `_release.html`, `release.html` | 156, 15 | the release panel and its full page |
| `version.html` | 115 | |
| `_timeline.html` | 17 | |
| `_work.html`, `_work_items.html` | 101, 10 | the tickets section of a release page |
| `ticket.html` | 64 | |

Then put back the UI lines of `create_app` held over from Stage 3.

**Working when** (still on the Stage 3 database)

- `/` lands on `/cis`, which lists DISP-SW and narrows as you type in the search box.
- `/cis/DISP-SW` shows 2026.Q4 in the releases table with its panel loaded on the right, the Versions card with
  both names, "Fielded in: Not in any approved baseline", and the CSCs card. Opening "Lineage" draws b1.
- Adding `2026.Q4-b2` under Versions adds a build to the release.
- The nav shows Capabilities, Configuration items and Overview; `/ifcs` says "No capabilities yet."
- `/cis/NOPE` is the HTML error page; `/api/cis/NOPE` is still JSON.
- The release page's tickets section says "no ticket source configured": that's Stage 6.

## Stage 6: dummy releases and tickets (165 lines)

`cmtrack/demo.py`, as it is on `main` less two blocks of `seed`:

| Lines on `main` | What | Do |
|---|---|---|
| 75–87 | the IFC-1 / IFC-2 block, from the `# IFC-1: Build 1 ...` comment to the `d1` entry | leave out |
| 94–101 | the backlog block at the end of `seed` | leave out |

That gives a stand-in Jira project of versions (`demo_release_source`), stand-in tickets for the CSCs
(`demo_source`), and a seed that creates NAV-SW (synced from the stand-in project, CSCs nav-core and nav-maps),
DISPLAY-SW and the composite SUITE (by hand), ships a few builds, and merges an emergency fix into the next quarter.

```bash
python -m cmtrack.demo --db demo.db
CMTRACK_DB=demo.db CMTRACK_TICKET_SOURCES=jira=cmtrack.demo:demo_source \
  CMTRACK_RELEASE_SOURCES=jira=cmtrack.demo:demo_release_source python -m cmtrack
```

For tickets that are *empty* rather than dummy, register `StaticSource([], name="jira")` as the ticket source: the
tickets section then renders with nothing in it, instead of the "no ticket source" alert.

**Working when**

- `/cis/NAV-SW`: quarters with their builds, 2026.Q4.ER1 under 2026.Q4, one item under Needs attention (a build
  the source can't place). "Preview sync" shows what would change without changing it; "Sync from jira" applies it.
- `/releases/<2027.Q1>`: feature tickets PRG-10 and PRG-18 with state bars, opening to CSC, then to tickets with
  their fix versions. Changing From / To changes the range; the CSC, type and state filters narrow it.
- `/tickets/PRG-10` shows every CSC that worked on it; `/versions/<id>` shows "Fixed in this version".
- `/cis/NAV-SW/work` redirects to the latest shipped release's page.

## Stage 7: real Jira for the CSCs (689 lines, plus the bootstrap)

| File | Lines | Type |
|---|---|---|
| `cmtrack/jira_tickets.py` | 603 | all of it (`JiraClient`, `Fields`, `JiraTicketSource`) |
| `cmtrack/jira_releases.py` | 86 | all of it (`JiraReleaseSource`) |
| `examples/bootstrap.py` | 148 | all of it, then change the settings at the top |

In the bootstrap, set `JIRA_URL`, the token file, `TICKETS` (top-level projects, field names, status map), the
version patterns, and `CIS`: each CI's `release_source` (`"jira"` or `"manual"`) and its CSCs' Jira project and
Affected Product pairs.

```bash
python examples/bootstrap.py           # creates the CIs and CSCs that are missing; safe to run again
python examples/bootstrap.py serve
```

**Working when**

- A Jira-backed CI's page shows "Never synced" and no releases; "Preview sync" lists the versions of its CSCs'
  Jira projects sorted into releases and builds; "Sync from jira" stores them.
- A release page lists the feature and discrepancy tickets fixed in its range, grouped by CSC.
- A wrong URL or token shows an alert on the page (and a 502 from the API), not a crash.

---

## Tests

Type these alongside the stage that makes them pass. All pass on this cut except where noted.

| File | With stage | Leave out |
|---|---|---|
| `tests/test_flow.py` | 3 | `ManualFlowTests.test_full_flow` from its first `/ifcs` call (line 190) on |
| `tests/test_manual_releases.py` | 5 | the "Open releases" assertion on the overview page (line 82) |
| `tests/test_work.py` | 6 | the HSCM `optgroup` assertion in `test_views` (line 264) |
| `tests/test_views.py` | 6 | `test_baseline_staleness_and_diff`, `test_events_paging`; the IFC, baseline, `/events` and `/` URLs in `test_pages_render`, `test_fragments` and `test_every_page_uses_the_ui_layout`; the last assertion of `test_edit_pin_correct` |
| `tests/test_ui.py` | 5 | `test_layout_markings_and_nav`, `test_styleguide_renders_everything`, `test_rank_item_keeps_drag_contract` |
| `tests/test_jira_tickets.py` | 7 | the two backlog lines at the end of `test_in_cmtrack` (250–251) |
| `tests/test_jira_releases.py`, `tests/test_bootstrap_example.py` | 7 | |

`python -m unittest discover -s tests`

## When capabilities come

Nothing typed so far changes shape; this only adds.

1. `service.py` lines 1248–1636, `api.py` lines 306–423.
2. `views.py`: `with_staleness`, the real `dashboard` and `recent_events` in place of the first stub, `BASELINE_DOTS`,
   `ifc_timeline` and its fragment, `spawn_options` through `baseline_diff` (the real `ifcs` replaces the second
   stub), `events`, and the "IFC & baseline forms" section.
3. Templates: the real `ifcs.html`, `ifc.html`, `baseline.html`, `_entries.html`, `_draft_entries.html`,
   `_version_options.html`, `_diff.html`, `dashboard.html`, `_recent_events.html`, `events.html`, `_event_rows.html`.
4. The two `seed` blocks left out of `demo.py`, the tests left out above, and `tests/test_baselines.py`.

The backlog (`cmtrack/backlog/`, the `ci.html` include, the `__init__.py` and `views.py` lines), `backup.py`,
`history.py` and the style guide are independent of capabilities and can follow in any order; see Phase 7 of
[`plan.md`](plan.md) for the backlog.

# cmtrack: implementation plan (rebuild order)

The order to type cmtrack in by hand. The code on `main` is the answer key; each phase names the files and
functions to copy across. Every phase ends with something that **runs and can be checked**, so stop at any
phase boundary and have a working tool.

**Priority scope (Phases 0–7):** one CI with CSCs whose quarterly releases and builds sync from Jira (or are
entered by hand) → a CI overview → drill into releases and versions → the tickets implemented since the
previous release → the shared backlog.
**Later (Phases 8–11):** patch/emergency releases and merges, dashboard and events, IFCs and their HSCM builds
(with the IFC timeline), composites and the rest.

---

## Ground rules

- **Type each table in its final shape.** Tables can arrive in later phases (`CREATE TABLE IF NOT EXISTS`
  makes that safe), but when you create a table, give it every column it ends up with (e.g. `release.parent_id`,
  `release.source_key`, `version.lineage`, `ci.last_sync`). There are no migrations: `schema.sql` is the whole
  schema, indexes included, so a database from an older shape is deleted and reloaded.
- **Stub, don't skip, cross-phase references.** A few functions return data from features that come later
  (`release_detail` returns `baselines_behind` and `unabsorbed`). Return `[]` for them until that phase,
  and leave a `# TODO phase N` comment so you know where to come back.
- **One commit per phase**, after its checks pass.
- **Keep a throwaway DB.** Delete `cmtrack.db` whenever a phase changes the schema of an existing table.
- **Environment:** Python 3.10+, `pip install flask`. Nothing else. For the browser: the UI component library
  (`static/ui.css`, `templates/ui/`) and htmx 2 (from jsdelivr, or self-hosted via `CMTRACK_UI_HTMX_JS`).
- **Test as you go.** Split `tests/test_flow.py` up so each phase adds the tests for what it built; the
  test files on `main` are the reference.

Handy for checks (define once in your shell):

```bash
api() { m=$1; p=$2; shift 2; curl -s -X "$m" "localhost:5000/api$p" -H 'Content-Type: application/json' "$@"; echo; }
# e.g. api POST /cis -d '{"name": "NAV-SW"}'
```

---

## Phase 0: skeleton

**Goal:** an app that starts, has a database, and turns domain errors into clean JSON.

| File | Type in |
|---|---|
| `cmtrack/schema.sql` | `ci` (in full: `release_source`, `source_params`, `require_tested`, `last_sync`), `csc`, `event` |
| `cmtrack/db.py` | `connect`, `init_db` (runs `schema.sql`), `get_db`, `close_db` |
| `cmtrack/service.py` | module docstring, `CMError` / `NotFound` / `Conflict`, helpers: `to_dict`, `to_dicts`, `now`, `log`, `_one`, `_by_ref`, `_date`, `_when` |
| `cmtrack/api.py` | blueprint, `body`, `pick`, `tx`, `created`, `index` (`GET /api/`) |
| `cmtrack/__init__.py` | `create_app` with `DATABASE` config, `CMError` and `IntegrityError` handlers (JSON only for now) |
| `cmtrack/__main__.py` | the dev server entry point |
| `.gitignore` | `__pycache__/`, `*.pyc`, `*.db` |

**Working when**
- `python -m cmtrack` starts; `GET /api/` lists the routes as JSON.
- `sqlite3 cmtrack.db .tables` shows the three tables.

---

## Phase 1: CI and CSC (API)

**Goal:** a CI and its CSCs with their Jira pairs.

| File | Type in |
|---|---|
| `service.py` | CI section (`get_ci`, `list_cis`, `_source_settings`, `create_ci`, `update_ci`, `ci_detail` without `releases` / `attention` for now); CSC section (`add_csc`, `find_csc`) |
| `api.py` | `GET/POST /cis`, `GET/PATCH /cis/<ci>`, `POST /cis/<ci>/cscs`, `GET /cscs/lookup` |

**Working when**
```bash
api POST /cis -d '{"name": "NAV-SW"}'
api POST /cis/NAV-SW/cscs -d '{"name": "nav-core", "jira_project": "NAVL", "affected_product": "core", "team": "Nav"}'
api POST /cis/NAV-SW/cscs -d '{"name": "other", "jira_project": "NAVL", "affected_product": "core"}'   # 409: pair taken
api GET "/cscs/lookup?project=NAVL&product=core"                                                      # -> NAV-SW
```

---

## Phase 2: releases and versions by hand (API)

**Goal:** enter a quarter and its builds, move builds through their lifecycle, add an ad hoc build, promote one
to be the release, and correct dates after the fact.

| File | Type in |
|---|---|
| `schema.sql` | `release` and `version` **in full** (including `parent_id`, `base_version_id`, `released_version_id`, `released_at`, `reason`, `source_key` / `source_state` / `pinned`, and `version.lineage`), plus `ix_version_release` and the two `ux_*_source_key` indexes |
| `service.py` | `RELEASED`, `VERSION_TRANSITIONS`; `get_release`, `get_version`, `_of_ci`, `find_version`, `find_release`, `list_releases`, `release_detail` (**stub** `baselines_behind` and `unabsorbed` as `[]`), `_next_seq`, `_insert_version`, `add_version`, `create_release` (**planned only**: patches are Phase 8), `SYNCED_*_FIELDS`, `PIN_ALIASES`, `_label`, `_pins`, `_note`, `update_release`, `update_version`, `release_version` (**skip** the composite/manifest block, Phase 11), `root_release`, `effective_version`. `ci_detail` now includes `releases`. |
| `api.py` | `POST /cis/<ci>/releases`, `GET /cis/<ci>/releases`, `GET/PATCH /releases/<id>`, `POST /releases/<id>/versions`, `GET/PATCH /versions/<id>`, `POST /versions/<id>/release` |
| tests | `ManualFlowTests`: the release / build / ad hoc b4 / promote part, and `test_date_corrections` |

**Working when**
- `POST /cis/NAV-SW/releases {"name": "2026.Q4", "target_date": "2026-12-15", "builds": ["2026.Q4-b1", "2026.Q4-b2", "2026.Q4-b3"]}`.
- b1–b3 → `built`; promoting b3 fails (`must be tested`); reject b3, add ad hoc b4, move it to `tested`, promote it.
- Correct b4's build date with a note; one in the future, or without a note, is refused.

---

## Phase 3: sync releases from Jira (API)

**Goal:** the CI's releases and builds come from its Jira project; anything that doesn't line up is flagged and
fixed by hand.

| File | Type in |
|---|---|
| `cmtrack/releases.py` | all of it (`ReleaseRecord`, `BuildRecord`, `Unplaced`, `ReleaseSource`, `SourceVersion`, `PatternSource`, `StaticVersionSource`), **then `PatternSourceTests` first** |
| `tickets.py` | `load_sources` (the `what` argument is for release sources; the rest of the file is Phase 6) |
| `service.py` | release sources section: `SourceError`, `_issue`, `_source_records`, `_adopt_or_find`, `_apply`, `sync_ci`, `_sync`, `_in_use`, `_drop_unused`, `_take_over`, `remap_release`, `remap_version`, `detach_release`, `detach_version`, `ci_attention`, `remap_candidates`; `ci_detail` gains `attention` |
| `__init__.py` | `RELEASE_SOURCES` config, falling back to `CMTRACK_RELEASE_SOURCES` |
| `api.py` | `release_source`, `GET /release-sources`, `POST /cis/<ci>/sync`, `GET /cis/<ci>/attention`, remap / detach for releases and versions |
| `demo.py` | `NAV_PARAMS`, `NAV_VERSIONS`, `demo_release_source` |
| tests | `SyncTests` |

**Working when**
- `PATCH /cis/NAV-SW {"release_source": "jira", "source_params": <NAV_PARAMS>}` with the demo source, then
  `POST /cis/NAV-SW/sync` creates the quarters and builds; again creates nothing; `{"dry_run": true}` changes nothing.
- Rename or re-date a version in the (static) source: the next sync updates it. Remove one: it's `missing`, and
  `remap` / `detach` clear it from `GET /cis/NAV-SW/attention`. Edit a synced date by hand: it's pinned and kept.
- **Then:** write your `JiraReleases(PatternSource)` (the docstring at the top of `releases.py` is the
  template), point `CMTRACK_RELEASE_SOURCES` at it, and sync your real project.

---

## Phase 4: web shell and CI overview

**Goal:** browse CIs and open a CI to see its releases, with a release panel loaded by htmx.

| File | Type in |
|---|---|
| `static/ui.css` | tokens + the shell, page structure, identifier, status, button, form and table styles (the rest arrives with the components that need it) |
| `templates/ui/components.html` | `icon`, `logo`, `marking_banner`, `app_header`, `breadcrumbs`, `page_header`, `card*`, `section_label`, `stats`/`stat`, `chip`, `ident`, `badge`, `timestamp`, `version_glyph`, `version_status`, `release_kind`, `alert`, `button`, `icon_button`, `field`, `input`, `select`, `search_box`, `table`, `empty_row`, `dl`, `empty`, `lineage` |
| `templates/ui/layout.html` + `cmtrack/ui.py` | the page shell; marking config, `utc`/`iso` filters, shell context. Point `UI_NAV` at the pages that exist so far. |
| `templates/error.html` | |
| `templates/cis.html`, `_ci_rows.html` | CI list; the search/filter form swaps `#ci-rows` |
| `templates/ci.html`, `_attention.html`, `_sync_summary.html` | releases table + `#release-panel` + Needs attention + release source card (sync, preview, last sync, add a release) + CSCs. **Leave out** "Fielded in" (Phase 10), the "Work items" button (Phase 6) and the Backlogs card (Phase 7). |
| `templates/_release.html`, `release.html`, `version.html` | release panel / full page with its edit, unpin, add-build, cancel and correct forms; version page with edit / correct. **Leave out** the work link, the unabsorbed alert, the baselines-behind alert, and the lineage / tickets / where-used cards. |
| `ui/components.html` | also `disclosure`, `source_state`, `pinned` (+ `.ui-pin`, `.ui-form-grid`, `.ui-plain-list` CSS) |
| `cmtrack/views.py` | `is_fragment`, `page`, `ci_overview`, `built_from`, `release_families`, `render_ci`, `edit_options`, and the views `cis`, `ci` (without the `fielded` query: it reads baseline tables, Phase 10), `release`, `version`, plus the form posts at the bottom (`_back`, `_run`, `_form`, sync … detach). Make `/` redirect to `/cis` until Phase 9. |
| `__init__.py` | `ui.init_app(app)`; register the `ui` blueprint; `CMError` renders `error.html` outside `/api` |
| `tests/test_views.py` | page renders + fragment-vs-page checks for the views above |

**Working when**
- `/cis` narrows as you type in the search box (the URL updates), and the type/managed filters work.
- `/cis/NAV-SW` lists the quarters with their status and source + ad hoc version counts; clicking one loads its
  panel on the right, and middle-clicking opens `/releases/<id>` as a full page.
- "Preview sync" shows what would change; "Sync from jira" applies it. A version the source lost shows under
  Needs attention with Remap / Detach / Cancel. Editing a target date in the panel pins it (× unpins).
- `/cis/NOPE` shows the HTML error page, while `/api/cis/NOPE` still returns JSON.

---

## Phase 5: version lineage and ranges

**Goal:** know what each build was built from, so a range like "since the last release" is a well-defined set
of versions.

| File | Type in |
|---|---|
| `schema.sql` | `version_parent` + `ix_vparent_parent` |
| `cmtrack/graph.py` | all of it: `newest_first`, and the timeline (`ZOOMS`, `short_label`, `timeline`) |
| `service.py` | lineage section: `release_head`, `rebuild_lineage`, `_closure`, `ancestor_ids`, `descendant_ids`, `version_parents`, `lineage`, `version_range`, `_topo_order` (on `graph.newest_first`), `ci_versions`, `release_range`. **Skip** `set_version_parents` and `unabsorbed_fixes` (Phase 8). Add the `rebuild_lineage(...)` calls in `create_release`, `add_version`, `update_release`, `release_version`, `sync_ci`, the remaps and `detach_release`. |
| `api.py` | `GET /versions/<id>/lineage`, `GET /cis/<ci>/versions?to=&from=` |
| `views.py` | `VERSION_DOTS`, `zoomed_timeline`, `ci_timeline`, `ci_lineage` (`/cis/<ci>/lineage`, a fragment) |
| templates | the lineage card in `version.html`; `_timeline.html`; the on-demand "Lineage" section in `ci.html`; `timeline` in `ui/components.html` (+ `.ui-graph__*` / `.ui-tl*` CSS) and `static/timeline.js`; the `attrs` argument of `disclosure` |
| tests | `test_auto_lineage` (planned releases only), `SyncTests.test_detach_and_phantoms`, `GraphLayoutTests` |

**Working when**
- `GET /api/cis/NAV-SW/versions?to=2026.Q4-b4` → b1, b2, b3, b4 (rejected b3 stays in the chain).
- `2027.Q1-b1`'s parent is `2026.Q4-b4` (the Q4 release); `?to=2027.Q1-b2&from=2026.Q4-b4` → Q1-b1, Q1-b2.
- `release_range(Q1)` is `{from: 2026.Q4-b4, to: <Q1 head>}`: the "what's new in this release" range.
- The version page shows "Built from" / "Built on by" links; opening "Lineage" on the CI page loads every build
  on a time axis: the quarters along one lane, fixes branching off and merging back, zoomable, centred on today.

---

## Phase 6: tickets implemented since a previous release

**Goal:** for any range of versions, show the parent tickets and, under each, how each CSC implemented them,
read live from the ticket source. Start against the in-memory `StaticSource`, then plug in your Jira code.

| File | Type in |
|---|---|
| `cmtrack/tickets.py` | `STATES`, `ALIASES`, `WORKFLOW`, `rollup`, `ERROR`, `normalize_state`, `TicketRecord` (leave `cis` out until Phase 7 if you like), `TicketSource` (`tickets_for_versions`, `get_tickets`, `get_children`), `StaticSource`, `pick_source` |
| `service.py` | tickets section: `_ask`, `_progress`, `_Resolver`, `_group_by_csc`, `work_report`, `ticket_detail` |
| `__init__.py` | `TICKET_SOURCES` config, falling back to `CMTRACK_TICKET_SOURCES` |
| `api.py` | `ticket_source`, `versions_arg`, `GET /ticket-states`, `GET /cis/<ci>/work`, `GET /tickets/<key>` |
| `views.py` | `ticket_source`, `live`, `work` (versions or `hscm:<id>` at either end; default: the latest shipped release), `ticket`; the version view gains `report` / `source_error` |
| `ui/components.html` | `state_glyph`, `state_pill`, `state_reason`, `state_bar`, `state_counts`, `ticket_ref`, `version_chip`, `ticket_line`, `group_label`, `work_group` (+ their CSS) |
| templates | `_work.html`, `_work_items.html`, `work.html`, `ticket.html`; the "Work items" button in `ci.html`; the work link in `_release.html`; "Fixed in this version" in `version.html` |
| `cmtrack/demo.py` | `seed` (the parts built so far), `DEMO_TICKETS`, `demo_source` |
| tests | `test_work.py`: report grouping, ticket detail, source is definitive, records that don't fit, source failures, no source configured |

**Working when**
- `CMTRACK_TICKET_SOURCES=jira=cmtrack.demo:demo_source python -m cmtrack`, then `/cis/NAV-SW/work`:
  choosing 2026.Q4-b4 .. 2027.Q1-b2 (type to search; `static/picker.js`, optgroups in `ui.select`) shows parent tickets
  with state bars, expanding to CSC → tickets with fix versions. An HSCM can stand for either end.
- A merge-blocked or error ticket shows its reason inline; `/tickets/PRG-10` shows every CSC that worked on it.
- If the source raises, the work page shows an alert and the API returns 502, not a crash.
- **Then:** write your `JiraSource(TicketSource)` (the docstring at the top of `tickets.py` is the template),
  point `CMTRACK_TICKET_SOURCES` at it, and check the same pages against real Jira.

---

## Phase 7: shared backlog

**Goal:** a ranked backlog of top-level tickets shared by several teams, reordered by drag and drop.

| File | Type in |
|---|---|
| `cmtrack/rank.py` | all of it, **then its tests first** (`RankTests` in `tests/test_backlog.py`) before anything uses it |
| `schema.sql` | `backlog`, `backlog_ci`, `backlog_item` |
| `tickets.py` | `TicketRecord.cis`; `TicketSource.top_level_tickets`; `StaticSource.top_level_tickets` |
| `service.py` | `"teams"` in `_JSON_COLS`; `_ask`'s `NotImplementedError` branch; backlogs section: `get_backlog` … `backlog_view` |
| `api.py` | `backlog_source` + the backlog endpoints (CRUD, items, `move`, `pull`, `rebalance`) |
| `views.py` | `backlogs`, `create_backlog`, `_backlog_fragment`, `backlog`, `_backlog_action` and the pull / add / remove / rebalance actions; the `ci` view gains `backlogs` |
| templates | `backlogs.html`, `backlog.html`, `_backlog_items.html`, `static/backlog.js` (drag and drop); `rank_item`, `checkbox`, `segmented` in `ui/components.html`; the Backlogs card in `ci.html`; Backlogs in `UI_NAV` |
| `demo.py` | the backlog part of `seed`, plus `cis` on the top-level demo tickets |
| tests | `BacklogTests` |

**Working when**
- `/backlogs` → create "Nav & Display" with its teams and CIs → "Pull from source" adds the top-level tickets
  for those CIs; pulling again adds nothing.
- Dragging an item and reloading keeps the new order. Only the moved item's rank changes
  (`GET /api/backlogs/<b>` shows the ranks).
- ⤒ ↑ ↓ work too; a stale move (e.g. two tabs) gets a 409, a toast, and a reloaded list.
- Adding a CSC ticket by key is refused and names its parent.

> **Milestone: the priority scope is done.** A CI with CSCs whose quarters sync from Jira, CI overview, release
> drill-down, tickets since the previous release from Jira, and the shared backlog.

---

## Phase 8: patch/emergency releases and merges

**Goal:** fixes released between quarters, and making sure the next quarter includes them.

| File | Type in |
|---|---|
| `service.py` | `_base_version` and the patch/emergency branch of `create_release`; `cancel_release`; the base-version fill at the end of `_sync`; the `no_base` / `no_reason` / `open_children` checks in `ci_attention`; `behind_effective` (**keep returning `[]`** until Phase 10: it reads baselines), `set_version_parents`, `unabsorbed_fixes` (un-stub it in `release_detail`). |
| `api.py` | `POST /releases/<id>/cancel`, `PUT` / `DELETE /versions/<id>/parents` |
| templates | the unabsorbed alert in `_release.html`; `release_families` already nests children in `ci.html` |
| tests | the patch / emergency / cancel parts of `ManualFlowTests`; `SyncTests.test_patches_come_from_the_source`; `test_merge_range_and_reset`, `test_parents_validation`, `test_lineage_survives_resync` |

**Working when**
- Add `2026.Q4.ER1` to the source (or by hand, with a reason) and sync: it hangs under 2026.Q4 and gets
  its base version once Q4 is released. Ship it. Two open emergencies on a line show under Needs attention.
- The Q1 panel warns "Not built on 1 earlier fix" until you `PUT /versions/<Q1-b1>/parents
  {"parents": ["2026.Q4-b4", "2026.Q4.ER1"]}`. After that, 2026.Q4-b4 .. 2027.Q1-b2 includes ER1's tickets.

---

## Phase 9: dashboard and events

| File | Type in |
|---|---|
| `service.py` | `list_events` (with `before_id`) |
| `api.py` | `GET /events` |
| `views.py` | `entity_url`, `dashboard` (without the stale-baseline card), `recent_events`, `events`. Drop the Phase 4 redirect of `/`. |
| templates | `dashboard.html`, `_recent_events.html`, `events.html`, `_event_rows.html`; Events in the nav |

**Working when:** `/` shows counts, open patch/emergency releases, upcoming releases and recent activity (refreshing
every 30s); `/events` filters by entity and loads more as you scroll. (The IFC timeline joins the dashboard in
Phase 10.)

---

## Phase 10: IFCs and their HSCM builds

**Goal:** IFCs that spawn from one another, each with a straight sequence of HSCM builds (Build 1, 2, … then HSC1,
HSC1.1, …) ending in a final one; all of it on a timeline.

| File | Type in |
|---|---|
| `schema.sql` | `ifc` (`spawned_from_id`, `final_id`), `baseline` (`seq`, `derived_from_id`, `supersedes_id`), `baseline_entry` + `ix_entry_version`, `ux_baseline_seq` |
| `service.py` | IFCs section (`get_ifc`, `_spawn_point`, `create_ifc`, `update_ifc`, `_spawn_ancestors`, `list_ifcs`, `ifc_detail`); HSCM builds section (`current_baseline`, `_latest_build`, `baseline_detail`, `baseline_lineage`, `_next_build`, `create_baseline`, the entry edits, `refresh_baseline`, `delete_baseline`, `_approve` / `_approval_time` / `approve_baseline`, `finalize_ifc`, `reopen_ifc`, `diff_baselines`), `_external_version`, `hscm_rows`, `import_hscm`; the real `behind_effective` |
| `api.py` | IFC and baseline endpoints, including final / reopen and the HSCM import (JSON or CSV, with `date`) |
| `views.py` | `with_staleness`, `BASELINE_DOTS`, `ifc_timeline`, `ifc_timeline_fragment`, `spawn_options`, `ifcs`, `ifc`, `baseline`, `version_options` (+ its fragment), `baseline_diff`, the IFC and baseline form posts; the stale card and the timeline in `dashboard`; "Fielded in" in `ci` |
| templates | `ifcs.html`, `ifc.html`, `baseline.html`, `_entries.html`, `_draft_entries.html`, `_version_options.html`, `_diff.html`; the baselines-behind alert in `_release.html`; IFCs in the nav |
| `cmtrack/history.py` | generate years of IFC history and load it (`--db` / `--url`) |
| tests | the IFC part of `test_full_flow`; `test_baselines.py` |

**Working when:** start IFC-1, import an HSCM CSV as Build 1 (unknown CIs become placeholders), start Build 2,
change an entry, approve it, diff the two, mark it final; spawn IFC-2 from it. Ship an emergency and the dashboard
and baseline page flag the entry as "behind". `python -m cmtrack.history load --db cmtrack.db --reset` fills the
timeline with years of IFCs: zoom from years to weeks, and it opens on today.

---

## Phase 11: composites, the rest

- `manifest_entry` table; `set_manifest`, `manifest`, `where_used`; the composite block in `release_version`;
  `PUT /versions/<id>/manifest`, `GET /versions/<id>/where-used`; the manifest / where-used cards in `version.html`.
- The full `demo.py` seed and the rest of `test_full_flow.py` and `test_views.py` (`FormTests`).

---

## After the rebuild (not on `main` yet)

- CSRF protection and login before exposing the UI beyond your own machine (the backlog, release and version
  forms change data).
- IFC ↔ CI membership (`ifc_ci`), so a build can be checked for a version of every CI its IFC comprises.
- A cross-CI "tickets in error" view: it needs a source query, since tickets aren't stored.

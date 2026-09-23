# cmtrack: implementation plan (rebuild order)

The order to type cmtrack in by hand. The code on `main` is the answer key; each phase names the files and
functions to copy across. Every phase ends with something that **runs and can be checked**, so stop at any
phase boundary and have a working tool.

**Priority scope (Phases 0–6):** one CI with CSCs on a quarterly release policy → a CI overview → drill into
releases and versions → the tickets implemented since the previous release → the shared backlog.
**Later (Phases 7–10):** patch/emergency releases and merges, dashboard and events, IFCs and HSCM baselines,
composites, and the manual policy.

---

## Ground rules

- **Type each table in its final shape.** Tables can arrive in later phases (`CREATE TABLE IF NOT EXISTS`
  makes that safe), but when you create a table, give it every column it ends up with (e.g. `release.parent_id`,
  `release.released_at`, `version.lineage`). Then `db.MIGRATIONS` stays empty and your early databases never
  need migrating. Only add a `MIGRATIONS` entry when you change a table that already holds data you care about.
- **Stub, don't skip, cross-phase references.** A few functions return data from features that come later
  (`release_detail` returns `baselines_behind` and `unabsorbed`). Return `[]` for them until that phase,
  and leave a `# TODO phase N` comment so you know where to come back.
- **One commit per phase**, after its checks pass.
- **Keep a throwaway DB.** Delete `cmtrack.db` whenever a phase changes the schema of an existing table.
- **Environment:** Python 3.10+, `pip install flask`. Nothing else. For the browser: Tailwind play CDN,
  daisyUI 4 and htmx 2 from jsdelivr (see `base.html`).
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
| `cmtrack/schema.sql` | `policy`, `ci`, `csc`, `event` tables only |
| `cmtrack/db.py` | `connect`, `init_db` (with the empty `MIGRATIONS` / `DROPPED` loops), `get_db`, `close_db` |
| `cmtrack/service.py` | module docstring, `CMError` / `NotFound` / `Conflict`, helpers: `to_dict`, `to_dicts`, `now`, `log`, `_one`, `_by_ref`, `_date` (needs `parse_date`: type a stub or jump ahead to `policies.parse_date`) |
| `cmtrack/api.py` | blueprint, `body`, `pick`, `tx`, `created`, `index` (`GET /api/`) |
| `cmtrack/__init__.py` | `create_app` with `DATABASE` and `POLICY_DIR` config, `CMError` and `IntegrityError` handlers (JSON only for now) |
| `cmtrack/__main__.py` | the dev server entry point |
| `.gitignore` | `__pycache__/`, `*.pyc`, `*.db` |

**Working when**
- `python -m cmtrack` starts; `GET /api/` lists the routes as JSON.
- `sqlite3 cmtrack.db .tables` shows the four tables.

---

## Phase 1: policy, CI and CSC (API)

**Goal:** define a quarterly cadence policy, a CI that uses it, and the CI's CSCs with their Jira pairs.

| File | Type in |
|---|---|
| `cmtrack/policies.py` | `PolicyError`, `PlannedVersion`, `PlannedRelease`, `add_months`, `parse_date`, `Policy` (all of it: defaults, `validate`, `plan`, `version_name`, `child_release_name`, `release_gate`, `_check_format`), `CadencePolicy`, `REGISTRY` / `register` / `build`. **Skip `ManualPolicy`** (Phase 10). |
| `service.py` | policies section (`create_policy`, `get_policy`, `list_policies`, `policy_for`); CI section (`get_ci`, `list_cis`, `create_ci`, `update_ci`, `ci_detail` without `releases` for now); CSC section (`add_csc`, `find_csc`) |
| `api.py` | `GET/POST /policies`, `GET/POST /cis`, `GET/PATCH /cis/<ci>`, `POST /cis/<ci>/cscs`, `GET /cscs/lookup` |
| `tests/test_flow.py` | `PolicyUnitTests` (quarterly/monthly naming, fiscal-year tokens, bad params) |

**Working when**
```bash
api POST /policies -d '{"name": "quarterly", "type": "cadence"}'
api POST /cis -d '{"name": "NAV-SW", "policy": "quarterly"}'
api POST /cis/NAV-SW/cscs -d '{"name": "nav-core", "jira_project": "NAVL", "affected_product": "core", "team": "Nav"}'
api POST /cis/NAV-SW/cscs -d '{"name": "other", "jira_project": "NAVL", "affected_product": "core"}'   # 409: pair taken
api GET "/cscs/lookup?project=NAVL&product=core"                                                      # -> NAV-SW
```
`CadencePolicy({}).plan(2026-10-01, 2027-04-01)` gives `2026.Q4` (b1–b3) and `2027.Q1`.

---

## Phase 2: releases and versions for the quarterly policy (API)

**Goal:** plan a CI's quarters, move builds through their lifecycle, add an ad hoc build, and promote one to be
the release.

| File | Type in |
|---|---|
| `schema.sql` | `release` and `version` **in full** (including `parent_id`, `base_version_id`, `released_version_id`, `released_at`, `reason`, and `version.lineage`), plus the partial unique index `ux_release_child_reason` and `ix_version_release` |
| `service.py` | `RELEASED`, `VERSION_TRANSITIONS`; `plan_ci`; `get_release`, `get_version`, `find_version`, `list_releases`, `release_detail` (**stub** `baselines_behind` and `unabsorbed` as `[]`), `_insert_version`, `add_version`, `update_version`, `release_version` (**skip** the composite/manifest block, Phase 10), `root_release`, `effective_version`. `ci_detail` now includes `releases`. |
| `api.py` | `POST /cis/<ci>/plan`, `GET /cis/<ci>/releases`, `GET /releases/<id>`, `POST /releases/<id>/versions`, `GET/PATCH /versions/<id>`, `POST /versions/<id>/release` |
| tests | the planning / build / ad hoc b4 / promote part of `test_full_flow` |

**Working when**
- `POST /cis/NAV-SW/plan {"start": "2026-10-01", "end": "2027-07-01"}` creates 3 quarters; running it again creates nothing.
- b1–b3 → `built`; promoting b3 fails (`must be tested`); reject b3, add ad hoc b4, move it to `tested`, promote it.
- `GET /cis/NAV-SW/releases` shows Q4 `released` with 3 planned + 1 unplanned versions.

---

## Phase 3: web shell and CI overview

**Goal:** browse CIs and open a CI to see its releases, with a release panel loaded by htmx.

| File | Type in |
|---|---|
| `templates/base.html` | the full layout (CDN links, navbar, theme toggle). Leave Backlogs / IFCs / Events out of `nav` for now. |
| `templates/macros.html` | `badge`, `page_header`, `empty`, `detail` (ticket macros come in Phase 5) |
| `templates/error.html` | |
| `templates/cis.html`, `_ci_rows.html` | CI list; the search/filter form swaps `#ci-rows` |
| `templates/ci.html` | releases table + `#release-panel` + policy + CSCs. **Leave out** "Fielded in" (Phase 9), the "Work items" button (Phase 5) and the Backlogs card (Phase 6). |
| `templates/_release.html`, `release.html` | release panel / full page. **Leave out** the work link, the unabsorbed alert and the baselines-behind alert. |
| `cmtrack/views.py` | `is_fragment`, `page`, `ts` filter, `release_families`, and the views `cis`, `ci` (without the `fielded` query: it reads baseline tables, Phase 9), `release`. Make `/` redirect to `/cis` until Phase 8. |
| `__init__.py` | register the `ui` blueprint; `CMError` renders `error.html` outside `/api` |
| `tests/test_views.py` | page renders + fragment-vs-page checks for the views above |

**Working when**
- `/cis` narrows as you type in the search box (the URL updates), and the type/managed filters work.
- `/cis/NAV-SW` lists the quarters with their status and planned + ad hoc version counts; clicking one loads its
  panel on the right, and middle-clicking opens `/releases/<id>` as a full page.
- `/cis/NOPE` shows the HTML error page, while `/api/cis/NOPE` still returns JSON.

---

## Phase 4: version lineage and ranges

**Goal:** know what each build was built from, so a range like "since the last release" is a well-defined set
of versions.

| File | Type in |
|---|---|
| `schema.sql` | `version_parent` + `ix_vparent_parent` |
| `service.py` | lineage section: `release_head`, `rebuild_lineage`, `backfill_lineage`, `_closure`, `ancestor_ids`, `descendant_ids`, `version_parents`, `lineage`, `version_range`, `_topo_order`, `ci_versions`, `release_range`. **Skip** `set_version_parents` and `unabsorbed_fixes` (Phase 7). Add `rebuild_lineage(...)` calls at the end of `plan_ci`, `add_version` and `release_version`. |
| `__init__.py` | call `backfill_lineage` at startup |
| `api.py` | `GET /versions/<id>/lineage`, `GET /cis/<ci>/versions?to=&from=` |
| `templates/version.html` + `views.version` | version page: stats, lineage card, history (no manifest / where-used / tickets yet) |
| tests | `test_auto_lineage` (planned releases only) |

**Working when**
- `GET /api/cis/NAV-SW/versions?to=2026.Q4-b4` → b1, b2, b3, b4 (rejected b3 stays in the chain).
- `2027.Q1-b1`'s parent is `2026.Q4-b4` (the Q4 release); `?to=2027.Q1-b2&from=2026.Q4-b4` → Q1-b1, Q1-b2.
- `release_range(Q1)` is `{from: 2026.Q4-b4, to: <Q1 head>}`: the "what's new in this release" range.
- The version page shows "Built from" / "Built on by" links.

---

## Phase 5: tickets implemented since a previous release

**Goal:** for any range of versions, show the parent tickets and, under each, how each CSC implemented them,
read live from the ticket source. Start against the in-memory `StaticSource`, then plug in your Jira code.

| File | Type in |
|---|---|
| `cmtrack/tickets.py` | `STATES`, `DONE` / `ERROR`, `normalize_state`, `TicketRecord` (leave `cis` out until Phase 6 if you like), `TicketSource` (`tickets_for_versions`, `get_tickets`, `get_children`), `StaticSource`, `pick_source`, `load_sources` |
| `service.py` | tickets section: `SourceError`, `_ask`, `_progress`, `_Resolver`, `_group_by_csc`, `work_report`, `ticket_detail` |
| `__init__.py` | `TICKET_SOURCES` config, falling back to `CMTRACK_TICKET_SOURCES` |
| `api.py` | `ticket_source`, `versions_arg`, `GET /ticket-states`, `GET /cis/<ci>/work`, `GET /tickets/<key>` |
| `views.py` | `ticket_source`, `live`, `work` (with the "What's new in" presets), `ticket`; the version view gains `report` / `source_error` |
| `macros.html` | `STATE_STYLE`, `tstate`, `reason`, `progress`, `state_counts`, `ticket_link` |
| templates | `_work.html`, `_work_items.html`, `work.html`, `ticket.html`; the "Work items" button in `ci.html`; the work link in `_release.html`; "Fixed in this version" in `version.html` |
| `cmtrack/demo.py` | `seed` (the parts built so far), `DEMO_TICKETS`, `demo_source` |
| tests | `test_work.py`: report grouping, ticket detail, source is definitive, records that don't fit, source failures, no source configured |

**Working when**
- `CMTRACK_TICKET_SOURCES=jira=cmtrack.demo:demo_source python -m cmtrack`, then `/cis/NAV-SW/work`:
  clicking "What's new in: 2027.Q1" shows parent tickets with state bars, expanding to CSC → tickets with fix versions.
- A merge-blocked or error ticket shows its reason inline; `/tickets/PRG-10` shows every CSC that worked on it.
- If the source raises, the work page shows an alert and the API returns 502, not a crash.
- **Then:** write your `JiraSource(TicketSource)` (the docstring at the top of `tickets.py` is the template),
  point `CMTRACK_TICKET_SOURCES` at it, and check the same pages against real Jira.

---

## Phase 6: shared backlog

**Goal:** a ranked backlog of top-level tickets shared by several teams, reordered by drag and drop.

| File | Type in |
|---|---|
| `cmtrack/rank.py` | all of it, **then its tests first** (`RankTests` in `tests/test_backlog.py`) before anything uses it |
| `schema.sql` | `backlog`, `backlog_ci`, `backlog_item` |
| `tickets.py` | `TicketRecord.cis`; `TicketSource.top_level_tickets`; `StaticSource.top_level_tickets` |
| `service.py` | `"teams"` in `_JSON_COLS`; `_ask`'s `NotImplementedError` branch; backlogs section: `get_backlog` … `backlog_view` |
| `api.py` | `backlog_source` + the backlog endpoints (CRUD, items, `move`, `pull`, `rebalance`) |
| `views.py` | `backlogs`, `create_backlog`, `_backlog_fragment`, `backlog`, `_backlog_action` and the pull / add / remove / rebalance actions; the `ci` view gains `backlogs` |
| templates | `backlogs.html`, `backlog.html` (including the inline drag-and-drop script), `_backlog_items.html`; the Backlogs card in `ci.html`; Backlogs in the `base.html` nav |
| `demo.py` | the backlog part of `seed`, plus `cis` on the top-level demo tickets |
| tests | `BacklogTests` |

**Working when**
- `/backlogs` → create "Nav & Display" with its teams and CIs → "Pull from source" adds the top-level tickets
  for those CIs; pulling again adds nothing.
- Dragging an item and reloading keeps the new order. Only the moved item's rank changes
  (`GET /api/backlogs/<b>` shows the ranks).
- ⤒ ↑ ↓ work too; a stale move (e.g. two tabs) gets a 409, a toast, and a reloaded list.
- Adding a CSC ticket by key is refused and names its parent.

> **Milestone: the priority scope is done.** A quarterly CI with CSCs, CI overview, release drill-down, tickets
> since the previous release from Jira, and the shared backlog.

---

## Phase 7: patch/emergency releases and merges

**Goal:** fixes released between quarters, and making sure the next quarter includes them.

| File | Type in |
|---|---|
| `service.py` | `spawn_release`, `cancel_release`, `behind_effective` (**keep returning `[]`** until Phase 9: it reads baselines), `set_version_parents`, `unabsorbed_fixes` (un-stub it in `release_detail`). Add `rebuild_lineage` calls to `spawn_release` and `cancel_release`. |
| `api.py` | `POST /releases/<id>/spawn`, `POST /releases/<id>/cancel`, `PUT` / `DELETE /versions/<id>/parents` |
| templates | the unabsorbed alert in `_release.html`; `release_families` already nests children in `ci.html` |
| tests | the patch / emergency / duplicate-guard / cancel parts of `test_full_flow`; `test_merge_range_and_reset`, `test_parents_validation`, `test_lineage_survives_replanning` |

**Working when**
- Spawn `2026.Q4.ER1` with a reason and ship it; the same reason again returns the existing release (200);
  a second open emergency is a 409.
- The Q1 panel warns "Not built on 1 earlier fix" until you `PUT /versions/<Q1-b1>/parents
  {"parents": ["2026.Q4-b4", "2026.Q4.ER1"]}`. After that, "What's new in 2027.Q1" includes ER1's tickets.

---

## Phase 8: dashboard and events

| File | Type in |
|---|---|
| `service.py` | `list_events` (with `before_id`) |
| `api.py` | `GET /events` |
| `views.py` | `entity_url`, `dashboard` (without the stale-baseline card), `recent_events`, `events`. Drop the Phase 3 redirect of `/`. |
| templates | `dashboard.html`, `_recent_events.html`, `events.html`, `_event_rows.html`; Events in the nav |

**Working when:** `/` shows counts, open patch/emergency releases, upcoming releases and recent activity (refreshing
every 30s); `/events` filters by entity and loads more as you scroll.

---

## Phase 9: IFC capabilities and HSCM baselines

| File | Type in |
|---|---|
| `schema.sql` | `ifc`, `baseline`, `baseline_entry` + `ix_entry_version` |
| `service.py` | IFCs section, baselines section, `_external_version`, `import_hscm`; the real `behind_effective` |
| `api.py` | IFC and baseline endpoints, including the HSCM CSV import |
| `views.py` | `with_staleness`, `ifcs`, `ifc`, `baseline`, `baseline_diff`; the stale card in `dashboard`; "Fielded in" in `ci` |
| templates | `ifcs.html`, `ifc.html`, `baseline.html`, `_entries.html`, `_diff.html`; the baselines-behind alert in `_release.html`; IFCs in the nav |
| tests | the IFC / HSCM / clone / approve / diff parts of `test_full_flow` |

**Working when:** import an HSCM CSV (unknown CIs become placeholders), clone it, change an entry, approve it, and
diff the two. Ship an emergency and the dashboard and baseline page flag the entry as "behind".

---

## Phase 10: composites, manual policy, the rest

- `manifest_entry` table; `set_manifest`, `manifest`, `where_used`; the composite block in `release_version`;
  `PUT /versions/<id>/manifest`, `GET /versions/<id>/where-used`; the manifest / where-used cards in `version.html`.
- `ManualPolicy` + `policies/display-sw.txt`.
- The full `demo.py` seed and the rest of `test_full_flow`.

---

## After the rebuild (not on `main` yet)

- CSRF protection and login before exposing the UI beyond your own machine (the backlog forms change data).
- IFC ↔ CI membership (`ifc_ci`) and parent-IFC rollups, discussed earlier but not built.
- A cross-CI "tickets in error" view: it needs a source query, since tickets aren't stored.

# cmtrack

Configuration tracking for CSCIs, their releases and versions, and the HSCM builds of IFCs.
Python 3.10+, Flask, SQLite. No other dependencies.

```
python -m cmtrack                      # dev server on :5000, DB in ./cmtrack.db
python -m unittest discover -s tests   # end-to-end scenario + views
python -m cmtrack.history load --db cmtrack.db [--reset]   # years of IFCs/HSCMs, managed CSCIs, CSCs; tickets.json
CMTRACK_TICKET_SOURCES=jira=cmtrack.history:ticket_file python -m cmtrack   # ...serving those tickets
python -m cmtrack.demo --db demo.db    # load a demo scenario, then:
CMTRACK_DB=demo.db CMTRACK_TICKET_SOURCES=jira=cmtrack.demo:demo_source \
  CMTRACK_RELEASE_SOURCES=jira=cmtrack.demo:demo_release_source python -m cmtrack
```
Env: `CMTRACK_DB` (SQLite path), `CMTRACK_TICKET_SOURCES` and `CMTRACK_RELEASE_SOURCES`
(`name=module:factory[,...]`, see Work items and Release sources).

## Model

| Concept | Table | Notes |
|---|---|---|
| CI | `ci` | CSCI or HWCI. `kind` simple/composite. `managed=0` = placeholder known only from HSCMs. `release_source` (+ `source_params`) = where its releases come from; NULL = entered by hand. `require_tested` = the release gate. |
| CSC | `csc` | Part of a CSCI. Unique Jira `(project, affected product)` pair; many CSCs → one CSCI. |
| Release | `release` | `planned`, `patch`/`emergency` (on a planned release: `parent_id` + `base_version_id`), `external` (scraped). Synced ones carry `source_key` / `source_state` / `pinned`. |
| Version | `version` | A build within a release. `planned=1` if it came from the release source, `0` if added by hand (variance on a synced CI). One is promoted to be the release. |
| Lineage | `version_parent` | DAG of what each build was built from. Derived from the releases (`lineage=auto`) or set by hand for merges (`manual`). |
| Manifest | `manifest_entry` | Composite CI version → pinned child versions. |
| IFC | `ifc` | An increment of capability. `spawned_from_id` = the approved HSCM (of an earlier IFC) it started from; `final_id` = the build marked final (the IFC is then closed). |
| Baseline | `baseline` + `baseline_entry` | An HSCM build of an IFC (`seq`: Build 1, 2, …): one version per CI. draft → approved → superseded. `derived_from_id` = the build before it (Build 1: the IFC's spawn point); `supersedes_id` = the approved one it replaced (set on approval). |
| Backlog | `backlog` + `backlog_item` + `backlog_ci` | A ranked list of top-level ticket keys shared by a set of teams, related to CIs. Stores only key + lexorank; tickets are read live. |
| Event | `event` | Append-only log of every change (status accounting). |

**Rules enforced**
- Version lifecycle: `planned → built → tested → released`; `rejected` from any pre-release state. `released` only via promotion.
- Promotion runs the CI's gate (`require_tested`, default on: the version must be `tested`, else `built` is
  enough) and can be backdated (`released_at`, never before the build). A composite needs a non-empty manifest
  of released children.
- Patch/emergency: hang off a planned release (`parent_id`, always the root of the line) and build on
  `base_version`: by default the line's effective version, filled in once something on the line is released.
  Entered by hand, an emergency needs a `reason` and names default to `<release>.P<n>` / `.ER<n>`; from a
  release source they come as the source lists them. Cancel an abandoned one with `POST /releases/<id>/cancel`.
- Not enforced (a release source can list anything) but reported (`GET /cis/<ci>/attention`): emergencies without a
  reason, patches with no base version, more than one open patch or emergency on a line.
- Dates: planned dates (`target_date`, `planned_date`) can be edited any time; what happened (`built_at`,
  `released_at`) can be corrected with a `note`, is never in the future, and a release is never before its build.
  Corrections are logged as `corrected` events with old → new.
- A release's `released_version` never changes. `effective_version` = latest released version in its family;
  `baselines_behind` = approved HSCMs still fielding an older one.
- Approving a baseline requires every entry to be `released` or `external`; approved baselines are frozen.
- An IFC's HSCMs are a straight sequence of builds: each new build (by hand or imported) starts from the one before
  (Build 1: from the HSCM the IFC was spawned from), and only one can be a draft at a time; a draft can be discarded.
  The latest approved build is the IFC's current HSCM. Marking it final closes the IFC to new builds (reopen to add more).
- IFCs spawn from an approved HSCM of an earlier IFC, never from a draft and never from their own descendants.
- Scraped HSCMs create placeholder CIs and `external` versions as needed and are approved as-is, returning warnings.

## Version lineage and ranges

Every version has parents in `version_parent`, maintained automatically as releases are synced, added, built
and cancelled:

- a build's parent is the previous build of its release (rejected ones included, since the next build was made from them);
- the first build of a patch/emergency builds on its base version;
- the first build of a planned release builds on the previous planned release's head (its released version, else
  its latest non-rejected build). Planned releases are ordered by target date; cancelled ones, and ones gone from
  the release source that never got a real build, are skipped.

When a fix is folded into a later release, record the merge: `PUT /api/versions/<id>/parents
{"parents": ["2026.Q4-b4", "2026.Q4.ER1"]}` (that version is then `manual`; `DELETE` reverts it).
Ranges use git semantics: `from..to` = `to` and everything it was built from, minus `from` and everything
*it* was built from. So `2026.Q4-b4..2027.Q1-b2` includes `2026.Q4.ER1` once it has been merged into Q1.
A planned release whose lineage is missing an earlier release line's released patch/emergency lists it
under `unabsorbed` (and the UI flags it).

## Work items (tickets)

The ticket source (Jira) is the system of record. **cmtrack stores no tickets**: every work report and ticket
page asks the source, so it always shows what the source says now. Caching, if any, is the source's business.
cmtrack contributes what the source doesn't know: the version set a range covers (lineage), which CSC and
CSCI a (Jira project, affected product) pair belongs to, and the grouping. Parent tickets are what reports
show; the CSC tickets under them are how each CSC team did the work, so each team can split or implement
it differently.

Implement `cmtrack.tickets.TicketSource` around your Jira client, returning `TicketRecord`s:

- `tickets_for_versions(ci, cscs, versions)`: CSC tickets of those CSCs whose fix versions include any of `versions`;
- `get_tickets(keys)`: tickets by key (ticket pages, and the parents of a report's CSC tickets);
- `get_children(key)`: the CSC tickets under a parent, across CIs.

Register it with `create_app({"TICKET_SOURCES": {"jira": JiraSource()}})` or
`CMTRACK_TICKET_SOURCES=jira=mypkg.jira:JiraSource`; with several, pick one per request with `?source=`.
If the source raises, the API answers 502 and pages show the error in place of the tickets.

`TicketRecord`: `key`, `summary`, `type`, `state`, `state_reason`, `status`, `parent_key`, `project` +
`affected_product` (resolved to the CSC), `fix_versions` (cmtrack version names of that CSC's CSCI), `url`,
`assignee`, `updated`, `attributes`. Tickets that can't be placed (unmapped Jira pair, fix versions outside
the request, another CI's ticket) are left out of the report and listed under `warnings`; a parent the
source can't find shows as an `error` placeholder.

**States** (`GET /api/ticket-states`), in workflow order: `analysis_required`, `in_analysis`, `ready_for_work`,
`in_progress`, `peer_review`, `verification`, `done`; plus `blocked` (waiting on something: `state_reason` says what),
`cancelled` and `error` (the source data doesn't add up, e.g. closed with open sub-tasks). The source decides a CSC
ticket's state, usually with domain logic over the ticket and everything linked to it rather than one Jira status.
A parent (DR/FEAT) ticket's state is consistent with its CSC tickets: `tickets.rollup` ignores cancelled ones,
lets blocked or error win, and otherwise takes the least advanced (in progress once any CSC has started);
`StaticSource` fills it in for parents that come without one. A missing or unrecognized state is stored as `error`
with a reason; blocked and error reasons show next to the ticket so people know what to fix in Jira. `status`
keeps the raw Jira status for display.

## Shared backlogs

Jira can't hold one ordering across several teams' projects, so cmtrack keeps it: a backlog has a name,
the teams sharing it, and related CIs (for navigation and for choosing what to pull), and each item is just a
top-level ticket key plus a **lexorank** (`cmtrack/rank.py`). Ticket data (summary, state, affected CIs) is
read live from the ticket source like everywhere else.

- **Ranks** are base-36 strings compared as plain strings. `rank.between(a, b)` returns a rank strictly
  between two others (`None` = open end), so a move rewrites only the moved item. Appends and prepends grow
  ranks by about one character per 35 inserts; repeatedly dropping into the same gap grows them faster, which
  `POST /api/backlogs/<b>/rebalance` fixes by re-spacing every rank (order unchanged). The UI offers it once
  ranks pass 12 characters.
- **Move callback**: `POST /api/backlogs/<b>/items/<key>/move {"after": <key above>, "before": <key below>}`
  returns `{key, rank}`. Send both neighbours after a drag and drop, or one to move next to an item (`before`
  the first item = top, `after` the last = bottom). If the neighbours are no longer in that order (someone
  else reordered), it answers 409 and the page reloads the list.
- **Pull**: `POST /api/backlogs/<b>/pull` asks the source's optional `top_level_tickets(backlog, cis)` for
  candidates and appends the ones not already there, in the source's order. Records may set `cis` (affected
  CI names), which the backlog shows. Nothing is removed by a pull. Items can also be added by key (top or
  bottom); CSC tickets are refused, since the backlog holds their parents.
- **Drag and drop** in `/backlogs/<b>` uses the browser's native drag events and ~60 lines of inline JS,
  with no libraries. ⤒ ↑ ↓ buttons use the same callback for keyboard and touch users.

## Release sources

A CI either syncs its releases and builds from a **release source** (Jira, typically) or has them entered by
hand. Sync is "always everything": `POST /api/cis/<ci>/sync` asks the source for every release it knows for the
CI and reconciles by the source's key (a Jira version id):

- new → created; known → updated (name, kind, dates, reason, parent; a build can move between releases while
  it is still `planned`);
- a row entered by hand with the same name is **adopted** (so a manual CI can switch to a source); a same-named
  row whose old key the source no longer lists at all is **re-keyed** (deleted and re-created in Jira);
- no longer listed → marked `missing` (never deleted); listed again → restored;
- what the source can't place (a build with no release, a name matching two patterns, a duplicate) is reported
  as an issue, not guessed at;
- a patch/emergency gets its base version once its line has released something.

`{"dry_run": true}` works it all out and rolls it back. The summary (created, updated with old → new,
adopted, re-keyed, restored, missing, pinned, bases, issues) is returned and kept on the CI as `last_sync`.

**Fixing by hand** (API or the CI page's "Needs attention" card):

- **Remap** a missing release/build onto what the source now calls it: `POST /releases/<old>/remap {"to": <new>}`.
  The old row takes the new one's key, name and dates (and its builds by position), and the new one, which must
  be fresh and unused, is deleted, so builds, patches, baselines and lineage stay attached. Same for
  `/versions/<id>/remap`.
- **Detach**: `POST /releases/<id>/detach` (or `/versions/<id>/detach`) keeps it as if entered by hand.
- **Cancel** it if it isn't happening.
- **Pin**: editing a synced field by hand (`PATCH /releases/<id>` / `PATCH /versions/<id>`) pins it; syncs keep
  your value and report the source's. `{"unpin": ["target_date"]}` hands it back.

**Writing a source.** The easy way is a `PatternSource` (`cmtrack/releases.py`): return the flat list of
versions in a Jira project and let name patterns in the CI's `source_params` sort them:

```python
class JiraReleases(PatternSource):
    def versions(self, ci, params):
        for v in my_jira.project_versions(params["project"]):
            yield SourceVersion(key=v.id, name=v.name, date=v.releaseDate, description=v.description,
                                released=v.released, archived=v.archived)
```
```json
{"project": "NAV",
 "patterns": {"planned":   "(?P<line>\\d{4}\\.Q\\d)",
              "build":     "(?P<line>\\d{4}\\.Q\\d)-b(?P<n>\\d+)",
              "patch":     "(?P<line>\\d{4}\\.Q\\d)\\.P(?P<n>\\d+)",
              "emergency": "(?P<line>\\d{4}\\.Q\\d)\\.ER(?P<n>\\d+)"},
 "self_build": ["patch", "emergency"], "include_archived": true}
```
Patterns match whole names; names matching none are ignored. Named groups tie things together: a build, patch or
emergency belongs to the planned release whose groups (all but `n`) have the same values, and `n` orders
builds (numerically). Kinds in `self_build` are their own build (add `"planned"` when a release's last build
carries the release's name, e.g. `3.2.0-rc1`, `3.2.0`). The version description becomes the reason / CR; a
version Jira marks released that cmtrack hasn't released is reported. Anything else implements
`ReleaseSource.releases(ci, params)` and returns `ReleaseRecord`s (key, name, kind, target_date, parent_key,
reason, released, builds) and `Unplaced` items. Register with
`create_app({"RELEASE_SOURCES": {"jira": JiraReleases()}})` or `CMTRACK_RELEASE_SOURCES=jira=mypkg.jira:JiraReleases`,
then `PATCH /api/cis/NAV-SW {"release_source": "jira", "source_params": {...}}`. A failing source answers 502.

## Endpoints (`/api`, CI/IFC refs accept id or name; `GET /api/` lists them all)

```
POST /cis                          {name, type?, kind?, managed?, release_source?, source_params?, require_tested?}
GET  /cis/<ci>          PATCH /cis/<ci>   {managed, kind, release_source, source_params, require_tested, description, attributes}
POST /cis/<ci>/cscs                {name, jira_project, affected_product, team?}
GET  /cscs/lookup?project=&product=
GET  /release-sources              configured release sources
POST /cis/<ci>/sync                {dry_run?}   reconcile with the release source → summary
GET  /cis/<ci>/attention           unplaced, missing, no base, no reason, several open patches on a line
POST /cis/<ci>/releases            {name?, kind, target_date?, parent?, base_version?, reason?, builds?}  by hand
GET  /cis/<ci>/releases            (includes source vs hand-added version counts)
GET  /releases/<id>                released_version, effective_version, baselines_behind, children
PATCH /releases/<id>               {name, target_date, reason, parent, base_version} (pins if synced),
                                   {released_at, note} (correction), {unpin: [...]}
POST /releases/<id>/versions       {name?, planned_date?}          build added by hand
POST /releases/<id>/remap {to}     POST /releases/<id>/detach      POST /releases/<id>/cancel {note?}
PATCH /versions/<id>               {status, built_at?} · {built_at, note} (correction) · {name, planned_date} · {unpin}
POST /versions/<id>/release        {released_at?}   promote (gate enforced)
POST /versions/<id>/remap {to}     POST /versions/<id>/detach
PUT  /versions/<id>/manifest       {children: [{ci, version}]}   composites only
GET  /versions/<id>/where-used     composites + baselines, transitively
POST /ifcs                         {name, spawned_from?: baseline id, description?}
GET  /ifcs/<ifc>                   spawned_from, ancestors, spawned, builds, final, draft, current HSCM
POST /ifcs/<ifc>/final             mark the latest (approved) build final    DELETE /ifcs/<ifc>/final  reopen
POST /ifcs/<ifc>/hscm              JSON {name?, rows:[{ci, version, type?}], source_ref?, approve?, date?}
                                   or text/csv (ci,version[,type]) with ?name=&source_ref=&date=
POST /ifcs/<ifc>/baselines         {name?, entries?}  → the next build, a draft ("Build N"; starts from the previous build)
PUT  /baselines/<id>/entries   POST /baselines/<id>/approve {approved_at?: backdate}
PUT  /baselines/<id>/entries/<ci> {version}   DELETE /baselines/<id>/entries/<ci>   (drafts)
POST /baselines/<id>/refresh      move entries behind their effective version up to it (drafts)
DELETE /baselines/<id>            discard a draft build
PATCH /ifcs/<ifc>                 {spawned_from?, description?}
GET  /baselines/<a>/diff/<b>
GET  /versions/<id>/lineage        PUT /versions/<id>/parents {parents}   DELETE /versions/<id>/parents
GET  /cis/<ci>/versions?to=&from=  range over the lineage DAG, oldest first
GET  /cis/<ci>/work?to=&from=      or ?versions=a,b   parent tickets -> CSC -> CSC tickets (live from the source)
GET  /tickets/<key>                a ticket, its parent, and CSC tickets under it by CI/CSC (live)
GET  /ticket-states                the workflow states a source may report
GET  /backlogs[?ci=]               POST /backlogs {name, description?, teams?, cis?, source?}
GET  /backlogs/<b>                 items in rank order, tickets read live      PATCH /backlogs/<b>
POST /backlogs/<b>/items {key, position?: bottom|top}   DELETE /backlogs/<b>/items/<key>
POST /backlogs/<b>/items/<key>/move {after?, before?} -> {key, rank}
POST /backlogs/<b>/pull            POST /backlogs/<b>/rebalance
GET  /events?entity=&entity_id=
```

`schema.sql` is the whole schema; there are no migrations. After a schema change, delete the database and reload it.

## Web UI (`/`)

Flask + Jinja pages in `cmtrack/views.py` / `cmtrack/templates/`, htmx for interactivity, and the cmtrack UI
component library below for everything visual (no CSS framework, no build step). Each view returns its `_fragment.html` template to htmx
requests and the full page otherwise (boosted navigation and history restores also get the full page), so
every URL works as a plain link.

```
/                     dashboard: counts, IFC timeline (HSCMs by date, a lane per active IFC, spawns branching off;
                      zoom years/quarters/months/weeks, opens on today, always at least full width),
                      open patch/emergency releases, upcoming releases,
                      HSCM entries behind their effective version, recent activity (polls every 30s)
/cis                  CI list; search + type/managed filters re-render the rows via htmx
/cis/<ci>             releases grouped by family (click one to load its panel), "Needs attention" (remap, detach,
                      cancel), sync / preview sync, last sync summary, add a release by hand, where fielded, CSCs
/cis/<ci>/work        work items for a from..to range (fuzzy pickers: a version, or an HSCM = the version it lists;
                      default: what's new in the latest shipped release); parent tickets
                      expand to each CSC's tickets
/releases/<id>        versions, released vs effective version, baselines behind, unabsorbed fixes, work link;
                      edit (pins synced fields), unpin, add a build, cancel, correct the release date
/versions/<id>        tickets fixed in it, lineage (built from / built on by), manifest, where-used, history;
                      edit name / planned date, correct the build date
/tickets/<key>        a parent ticket and how each CSC implemented it, across CIs
/backlogs             shared backlogs, and a form to create one
/backlogs/<b>         the ranked backlog: drag and drop (or ⤒ ↑ ↓) to reorder, pull from the source, add by key,
                      remove, hide done
/cis/<ci>/lineage     fragment for the CI page's "Lineage" section (loaded when opened): every build on a time
                      axis, planned releases along one lane, patch/emergency branches, merges; zoomable
/ifcs                 IFCs (spawned from, builds, current, final) and the IFC timeline; add an IFC
/ifcs/<ifc>           its builds, current HSCM entries (stale ones flagged); start the next build, import an HSCM
                      CSV as the next build (file or paste), mark final / reopen, edit spawn point/description
/baselines/<id>       entries, compare with another HSCM (diff loaded via htmx), lineage;
                      drafts: pick versions, add/remove CIs, bring stale entries up to date, approve, discard
/events               audit log, entity filter, infinite scroll
```

## UI component library (`/ui`)

The design system every page is built from: plain CSS tokens and components in `static/ui.css`, Jinja macros in `templates/ui/components.html`, and a
page shell in `templates/ui/layout.html`. **`/ui` is the living reference**: every macro rendered with a usage
snippet, plus a CI overview page built only from macros.

```jinja
{% extends "ui/layout.html" %}
{% import "ui/components.html" as ui %}
{% set nav_current = "cis" %}
{% block content %}
  {% call ui.page_header("NAV-SW", "Navigation software", crumbs=[("Configuration items", url_for("ui.cis")), ("NAV-SW", None)]) %}
    {{ ui.button("Work items", variant="primary", href=url_for("ui.work", ref="NAV-SW")) }}
  {% endcall %}
  {% call ui.card(flush=True) %}{% call ui.table(["Release", "Status"]) %}...{% endcall %}{% endcall %}
{% endblock %}
```

Macros: shell (`marking_banner`, `app_header`), structure (`page_header`, `breadcrumbs`, `card`, `card_header`,
`card_section`, `card_footer`, `stats`/`stat`, `section_label`), identifiers (`ident`, `chip`, `badge`, `kbd`,
`timestamp`), status (`version_status`, `version_glyph`, `state_pill`, `state_glyph`, `state_reason`, `state_bar`,
`state_counts`), feedback (`alert`, `empty`), actions (`button`, `icon_button`, `button_group`, `icon`), forms
(`field`, `input`, `select`, `checkbox`, `search_box`, `segmented`, `tabs`), data (`table`, `empty_row`, `dl`,
`audit_list`, `stamp`, `disclosure`), release sources (`source_state`, `pinned`), tickets and backlogs (`ticket_ref`, `ticket_line`, `group_label`, `rank_item`, `drop_line`,
`lineage`, `timeline`). Extra HTML attributes (hx-*, data-*, aria-*) go in `attrs={...}`.

Every status is a glyph and a word as well as a colour. Identifiers are monospace, times always UTC (`utc` filter:
`2026-09-23 14:24Z`), focus is always visible. Config in `cmtrack/ui.py`: `CMTRACK_MARKING` (+
`CMTRACK_MARKING_COLORS`) for the top/bottom banners, which read "[Marking not configured]" until set;
`CMTRACK_PROGRAM`; `CMTRACK_UI_FONTS_CSS` / `CMTRACK_UI_HTMX_JS` to self-host fonts and htmx on a closed network.
Forms post plain HTML (boosted by htmx, `hx-push-url="false"`) and redirect back; the layout's `htmx-config`
swaps 4xx/5xx responses too, so a failed post shows the error page instead of silently doing nothing.

Pages extend `ui/layout.html`, import the macros and add no page-specific CSS. The only page scripts are
`static/backlog.js` (backlog drag and drop), `static/picker.js` (fuzzy search on a `select[data-picker]`) and
`static/timeline.js` (centres a timeline on today, and re-fetches
one that is narrower than its box at the box's width).

## Not yet built (next iterations)
- Jira: the `TicketSource` for your Jira client; discrepancy/feature trace; a cross-CI "tickets in error" view
  (needs a source query for it, since nothing is stored).
- Verification events as a first-class record behind the `tested` gate.
- HW revisions beyond "a version of an HWCI"; per-CSC versions (only if a product needs them).

# cmtrack

Configuration tracking for CSCIs, their releases and versions, and IFC HSCM baselines.
Python 3.10+, Flask, SQLite. No other dependencies.

```
python -m cmtrack                      # dev server on :5000, DB in ./cmtrack.db
python -m unittest discover -s tests   # end-to-end scenario + views
python -m cmtrack.demo --db demo.db    # load a demo scenario, then:
CMTRACK_DB=demo.db CMTRACK_TICKET_SOURCES=jira=cmtrack.demo:demo_source python -m cmtrack
```
Env: `CMTRACK_DB` (SQLite path), `CMTRACK_POLICY_DIR` (where manual plan files live, default `./policies`),
`CMTRACK_TICKET_SOURCES` (`name=module:factory`, see Work items).

## Model

| Concept | Table | Notes |
|---|---|---|
| Policy | `policy` | Named, shared by many CIs. `type` picks a class in `policies.py`; `params` is JSON. |
| CI | `ci` | CSCI or HWCI. `kind` simple/composite. `managed=0` = placeholder known only from HSCMs. |
| CSC | `csc` | Part of a CSCI. Unique Jira `(project, affected product)` pair; many CSCs → one CSCI. |
| Release | `release` | `planned` (from policy), `patch`/`emergency` (spawned onto the root release: `parent_id` + `base_version_id`), `external` (scraped). |
| Version | `version` | A build within a release. `planned=1` if the policy created it, `0` if ad hoc (variance). One is promoted to be the release. |
| Lineage | `version_parent` | DAG of what each build was built from. Derived from the plan (`lineage=auto`) or set by hand for merges (`manual`). |
| Manifest | `manifest_entry` | Composite CI version → pinned child versions. |
| IFC | `ifc` | Capability, with `parent_id` hierarchy. |
| Baseline | `baseline` + `baseline_entry` | The HSCM list: one version per CI. draft → approved → superseded. |
| Event | `event` | Append-only log of every change (status accounting). |

**Rules enforced**
- Version lifecycle: `planned → built → tested → released`; `rejected` from any pre-release state. `released` only via promotion.
- Promotion runs the policy gate (cadence default: must be `tested`). A composite needs a non-empty manifest of released children.
- Patch/emergency: only on a release that's been promoted; emergency requires a `reason`. Children always hang
  off the root release, so they number per release line (`2026.Q4.ER1`, `.ER2`) and by default build on the
  family's effective version.
- No duplicate spawns: the same `reason` (change request) on the same release line returns the existing
  release (200, `spawned: false`); policy `max_open` (default 1 per kind) blocks another while one is open (409).
  Cancel an abandoned one with `POST /releases/<id>/cancel`; numbers are never reused. A partial unique index
  and an immediate write lock make this hold under concurrent requests.
- A release's `released_version` never changes. `effective_version` = latest released version in its family;
  `baselines_behind` = approved HSCMs still fielding an older one.
- Approving a baseline requires every entry to be `released` or `external`; approved baselines are frozen (clone to change).
- Scraped HSCMs create placeholder CIs and `external` versions as needed and are approved as-is, returning warnings.

## Version lineage and ranges

Every version has parents in `version_parent`, maintained automatically as releases are planned, built,
spawned and cancelled:

- a build's parent is the previous build of its release (rejected ones included, since the next build was made from them);
- the first build of a patch/emergency builds on its base version;
- the first build of a planned release builds on the previous planned release's head (its released version, else its latest non-rejected build).

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

**States** (`GET /api/ticket-states`), in workflow order: `analysis_required`, `analysis_in_progress`,
`ready_for_work`, `in_progress`, `peer_review`, `merge_blocked` (code ready, something is holding up the merge),
`verification`, `done`, `error`. The source decides the state, usually with domain logic over the ticket and
everything linked to it rather than one Jira status, and says why in `state_reason` where that helps. `error`
means the source data doesn't add up (e.g. closed with open sub-tasks). A missing or unrecognized state is stored
as `error` with a reason. Errors (and merge-blocked reasons) show next to the ticket so people know what to fix
in Jira. `status` keeps the raw Jira status for display.

## Policies

- **`none`**: plans nothing (default for CIs without a policy).
- **`cadence`**: `release_months`, `build_months`, `anchor_month` (10 = US FY), `build_day`, `release_format`
  (`{year} {fy} {quarter} {month}`), `version_format` (`{release} {seq}`), `require_tested`.
  Default is quarterly release, monthly builds: `2026.Q4-b1..b3`.
- **`manual`**: `path` to a hand-maintained file:
  ```
  release 3.2.0 2026-11-30
      version 3.2.0-rc1 2026-10-31
      version 3.2.0     2026-11-30
  ```
- All policies: `patch_format` (`{release}.P{n}`), `emergency_format` (`{release}.ER{n}`) — tokens `{release}` `{base}` `{n}` —, `child_version_format`, `spawn_kinds`, `max_open` (`{"patch": 1, "emergency": 1}`, null = unlimited).
- New behaviour = subclass `Policy`, override `plan()` / `release_gate()` / naming, then `register()` it.

Planning (`POST /cis/<ci>/plan`) is idempotent. It creates missing releases and versions and moves dates on items still `planned`, and it never deletes anything.

## Endpoints (`/api`, CI/IFC refs accept id or name; `GET /api/` lists them all)

```
POST /policies                     {name, type, params}
POST /cis                          {name, type?, kind?, managed?, policy?}
GET  /cis/<ci>          PATCH /cis/<ci>   {managed, kind, policy, description, attributes}
POST /cis/<ci>/cscs                {name, jira_project, affected_product, team?}
GET  /cscs/lookup?project=&product=
POST /cis/<ci>/plan                {start?, end?}
GET  /cis/<ci>/releases            (includes planned vs unplanned version counts)
GET  /releases/<id>                released_version, effective_version, baselines_behind, children
POST /releases/<id>/versions       {name?, planned_date?}          ad hoc build
POST /releases/<id>/spawn          {kind: patch|emergency, reason?, base_version?, target_date?}  201 new / 200 existing
POST /releases/<id>/cancel         {note?}
PATCH /versions/<id>               {status, artifact_ref?}
POST /versions/<id>/release        promote (gate enforced)
PUT  /versions/<id>/manifest       {children: [{ci, version}]}   composites only
GET  /versions/<id>/where-used     composites + baselines, transitively
POST /ifcs                         {name, parent?}     PATCH /ifcs/<ifc> {parent}
GET  /ifcs/<ifc>                   ancestors, children, baselines, current HSCM
POST /ifcs/<ifc>/baselines         {name, entries: [{ci, version}]}  → draft
POST /ifcs/<ifc>/hscm              JSON {name, rows:[{ci, version, type?}], source_ref?, approve?}
                                   or text/csv (ci,version[,type]) with ?name=&source_ref=
PUT  /baselines/<id>/entries   POST /baselines/<id>/clone   POST /baselines/<id>/approve
GET  /baselines/<a>/diff/<b>
GET  /versions/<id>/lineage        PUT /versions/<id>/parents {parents}   DELETE /versions/<id>/parents
GET  /cis/<ci>/versions?to=&from=  range over the lineage DAG, oldest first
GET  /cis/<ci>/work?to=&from=      or ?versions=a,b   parent tickets -> CSC -> CSC tickets (live from the source)
GET  /tickets/<key>                a ticket, its parent, and CSC tickets under it by CI/CSC (live)
GET  /ticket-states                the workflow states a source may report
GET  /events?entity=&entity_id=
```

Schema additions to existing DBs are applied by `db.MIGRATIONS` at startup.

## Web UI (read-only, `/`)

Flask + Jinja pages in `cmtrack/views.py` / `cmtrack/templates/`, htmx for interactivity, Tailwind + daisyUI 4
(loaded from CDN in `base.html`, no build step). Each view returns its `_fragment.html` template to htmx
requests and the full page otherwise (boosted navigation and history restores also get the full page), so
every URL works as a plain link.

```
/                     dashboard: counts, open patch/emergency releases, upcoming releases,
                      HSCM entries behind their effective version, recent activity (polls every 30s)
/cis                  CI list; search + type/managed filters re-render the rows via htmx
/cis/<ci>             releases grouped by family (click one to load its panel), where fielded, policy, CSCs
/cis/<ci>/work        work items for a from..to range ("what's new in <release>" presets); parent tickets
                      expand to each CSC's tickets
/releases/<id>        versions, released vs effective version, baselines behind, unabsorbed fixes, work link
/versions/<id>        tickets fixed in it, lineage (built from / built on by), manifest, where-used, history
/tickets/<key>        a parent ticket and how each CSC implemented it, across CIs
/ifcs                 IFC tree with each IFC's current HSCM
/ifcs/<ifc>           current HSCM entries (stale ones flagged), baseline history
/baselines/<id>       entries, compare with another baseline of the IFC (diff loaded via htmx)
/events               audit log, entity filter, infinite scroll
```

## Not yet built (next iterations)
- Jira: the `TicketSource` for your Jira client; discrepancy/feature trace; a cross-CI "tickets in error" view
  (needs a source query for it, since nothing is stored).
- Verification events as a first-class record behind the `tested` gate.
- HW revisions beyond "a version of an HWCI"; per-CSC versions (only if a product needs them).

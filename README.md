# cmtrack

Configuration tracking for CSCIs, their releases and versions, and IFC HSCM baselines.
Python 3.10+, Flask, SQLite. No other dependencies.

```
python -m cmtrack                      # dev server on :5000, DB in ./cmtrack.db
python -m unittest discover -s tests   # end-to-end scenario
```
Env: `CMTRACK_DB` (SQLite path), `CMTRACK_POLICY_DIR` (where manual plan files live, default `./policies`).

## Model

| Concept | Table | Notes |
|---|---|---|
| Policy | `policy` | Named, shared by many CIs. `type` picks a class in `policies.py`; `params` is JSON. |
| CI | `ci` | CSCI or HWCI. `kind` simple/composite. `managed=0` = placeholder known only from HSCMs. |
| CSC | `csc` | Part of a CSCI. Unique Jira `(project, affected product)` pair; many CSCs → one CSCI. |
| Release | `release` | `planned` (from policy), `patch`/`emergency` (spawned onto the root release: `parent_id` + `base_version_id`), `external` (scraped). |
| Version | `version` | A build within a release. `planned=1` if the policy created it, `0` if ad hoc (variance). One is promoted to be the release. |
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
GET  /events?entity=&entity_id=
```

Schema additions to existing DBs are applied by `db.MIGRATIONS` at startup.

## Not yet built (next iterations)
- Jira sync: product tickets via the CSC pair mapping, discrepancy/feature trace, missing-ticket findings.
- Verification events as a first-class record behind the `tested` gate.
- Emergency reconciliation: flag when a later planned release (e.g. 2027.Q1) hasn't absorbed a Q4 emergency fix.
- HW revisions beyond "a version of an HWCI"; per-CSC versions (only if a product needs them).

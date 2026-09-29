# Shared backlogs (`cmtrack/backlog/`)

Everything about backlogs lives in this folder: about 1,250 lines across 13 files. To type it in on another
system, read the files in the order below. Each step runs on its own.

A backlog is a ranked list of **top-level ticket keys** shared by a set of teams and related to CIs. Jira can't
hold one ordering across several teams' projects, so the order lives here: each item is just a key plus a
**lexorank** string, and a move rewrites only the moved item's rank. Ticket data (summary, state, affected
CIs) is read live from the ticket source on every view.

Where the (key, rank) pairs are kept is up to each backlog's **rank store**:

- **`sqlite`** (the default): the `backlog_item` table in cmtrack's database. Transactional, unique ranks, and
  it remembers when each item was added.
- **`jira`**: a plain **text custom field** on each Jira issue, one field per backlog (no Jira app needed). An
  issue is on the backlog when that field holds a rank; removing it clears the field. Backlog settings:
  `{"store": "jira", "store_params": {"rank_field": "customfield_12345", "scope": "<optional JQL>"}}`.

The backlog's own row (name, teams, CIs, ticket source, store) always stays in cmtrack's database. Changing a
backlog's store (`PATCH /api/backlogs/<b> {"store": ..., "store_params": ...}`) copies every item with its rank
and then clears the old store, so you can start in SQLite and move to Jira later, or back.

## Files, in typing order

| # | File | Lines | What it is |
|---|---|---|---|
| 1 | `rank.py` | 68 | Lexorank: `between(lo, hi)` gives a string that sorts strictly between two others (`None` = open end); `spread(n)` gives n evenly spaced ranks; `validate`. No dependencies. Its tests are `RankTests` in `tests/test_backlog.py`, so type those next. |
| 2 | `schema.sql` | 34 | Tables `backlog` (name, description, teams JSON, source, store, store_params), `backlog_ci` (backlog ↔ ci) and `backlog_item` (the sqlite store's items: backlog, ticket_key, rank, added_at; `UNIQUE(backlog_id, rank)`). |
| 3 | `stores.py` | 253 | The rank stores. `RankStore` is the interface (items, add, remove, set_rank, set_ranks, check_params). `SqliteStore` keeps items in `backlog_item`. `JiraRankStore` keeps them in a custom field through a `JiraFieldClient`, which needs only two calls, `find(field, scope)` and `set(key, field, value)`. `JiraRestClient` implements them over Jira's REST API v2 using only the standard library; `MemoryJira` is an in-memory stand-in for tests and demos. |
| 4 | `service.py` | 353 | Domain logic, the same for every store: `list_backlogs`, `create_backlog`, `update_backlog` (including moving to another store), `backlog_summary`, `add_backlog_item`, `remove_backlog_item`, `move_backlog_item` (the drag-and-drop callback), `rebalance_backlog`, `pull_backlog`, `backlog_view`. Items are sorted by (rank, key); invalid ranks sort last and are reported. |
| 5 | `routes.py` | 203 | One blueprint: the JSON API under `/api/backlogs…` and the pages under `/backlogs…` (the route list is in its docstring). |
| 6 | `__init__.py` | 45 | `init_db(conn)` runs `schema.sql` (and adds the store columns to older databases); `init_app(app)` loads the stores and registers the blueprint. |
| 7 | `templates/backlog/_macros.html` | 36 | `rank_item` (one draggable row) and `backlog_css()`. |
| 8 | `templates/backlog/list.html` | 53 | `/backlogs`: the table of backlogs (with where each keeps its order) and the "New backlog" form (store choice, Jira rank field). |
| 9 | `templates/backlog/page.html` | 38 | `/backlogs/<b>`: the header, the add-by-key form and `#backlog-items` (which carries the move URL). |
| 10 | `templates/backlog/_items.html` | 31 | The ranked list; htmx swaps it back in after pull, add, remove and rebalance. |
| 11 | `static/backlog.js` | 94 | Native HTML5 drag and drop plus ⤒ ↑ ↓ buttons. Each calls `POST …/move {after, before}`; on a 409 it shows a toast and reloads the list. |
| 12 | `static/backlog.css` | 16 | Styles for `.ui-rank-list` and `.ui-rank-item`. |
| – | `templates/backlog/_ci_card.html` | 16 | Optional: the "Backlogs" card that `templates/ci.html` includes. |

## What it needs from the rest of cmtrack

Configuration:

| Setting (app config / environment) | Meaning |
|---|---|
| `BACKLOG_STORES` / `CMTRACK_BACKLOG_STORES` | stores next to the built-in `sqlite`, as `name=module:factory`, e.g. `jira=cmtrack.backlog.stores:jira_store_from_env` |
| `BACKLOG_DEFAULT_STORE` / `CMTRACK_BACKLOG_DEFAULT_STORE` | the store for new backlogs that don't name one (default `sqlite`) |
| `CMTRACK_JIRA_URL` + `CMTRACK_JIRA_TOKEN` (or `_USER` + `_PASSWORD`) | what `jira_store_from_env` connects with |

To try the Jira store without Jira, use `CMTRACK_BACKLOG_STORES=jira=cmtrack.backlog.stores:memory_jira_store`
(it forgets everything on restart). To use your own Jira code, give `JiraRankStore` any object with `find` and
`set`.

Wiring (three lines in `cmtrack/__init__.py`):

```python
from . import backlog
backlog.init_db(conn)        # after db.init_db(conn): backlog_ci references ci
backlog.init_app(app)        # registers the blueprint
```

Core pieces it imports. On another system, provide these or stub them:

| From | Used for |
|---|---|
| `service.CMError` / `NotFound` / `Conflict` | errors, which the app turns into 400 / 404 / 409 (JSON under `/api`, the error page elsewhere) |
| `service._by_ref`, `log`, `to_dict(s)` | row lookup by id or name, the event log, row → dict |
| `service.SourceError` | the 502 raised when a store (Jira) fails, shown in place of the list on the page |
| `tickets.load_sources` | parsing `CMTRACK_BACKLOG_STORES` (the same `name=module:factory` format as the ticket sources) |
| `service.get_ci`, `list_cis` and the `ci` table | a backlog relates to CIs (the form lists managed CIs) |
| `service._ask`, `_Resolver`, `_progress` | calling the ticket source safely, placing tickets on CIs/CSCs, counts per state |
| `tickets.TicketRecord`, `tickets.ERROR` | a placeholder row for keys the source no longer knows |
| `api.tx`, `body`, `pick`, `created`, `ticket_source` | transaction per request, JSON body helpers, choosing the ticket source |
| `views.page`, `live`, `ticket_source` | fragment-or-page rendering, showing a source failure in place of the list |
| `ui/layout.html`, `ui/components.html`, `static/ui.css` | the page shell, component macros and design tokens |
| `ui.NAV` | the "Backlogs" nav entry (endpoint `backlog.backlogs`) |

The ticket source needs `get_tickets(keys)`. For "Pull from source" it also needs the optional
`top_level_tickets(backlog, cis)` (`tickets.TicketSource`). Records may set `cis` (affected CI names), which the
list shows.

## Behaviour worth knowing before retyping

- **Moves**: send the new neighbours, `after` (the key now above) and/or `before` (the key now below). Only the
  moved item's rank changes. If they're no longer in that order (someone else reordered), the answer is 409.
- **Rank growth**: dropping into the same gap over and over makes ranks longer. `rebalance` re-spaces them all
  in two UPDATE passes (because of the `UNIQUE(backlog_id, rank)`), keeping the order and `added_at`. The page
  offers it once ranks pass 12 characters.
- **Pull** only appends, in the source's order, and never removes. Adding by key refuses CSC tickets (they have
  a `parent_key`), since the backlog holds their parents.
- **Missing tickets**: a key the source no longer knows shows as an `error` item, "not found in the ticket
  source", instead of disappearing.
- **Names**: backlog names can't be all digits, because refs accept an id or a name.

## The Jira store in practice

- **Set up**: create one **text field (single line)** per backlog in Jira and add it to the screens of the issue
  types you rank. The service account needs permission to edit those issues. There's no need to show the field
  to people, but anyone who can see it can edit it.
- **Calls per action**:
  - view, add, move, pull: one search (paged at 100 issues);
  - add, move, remove: one issue update each, as well;
  - rebalance: one update per item;
  - moving a backlog into Jira: one update per item.
- **No transactions**: two people moving items at the same moment can leave two items with the same rank, and
  anyone can type into the field. The list stays usable: ties are broken by key, invalid values sort last, and
  both show as warnings with a "Rebalance ranks" button that rewrites every rank. Moving next to an item with a
  shared or invalid rank asks you to rebalance first.
- **A rebalance that fails partway** (Jira down) leaves some items with new ranks and some with old ones, so the
  order can look mixed. Run it again once Jira is back: it re-spaces the list as it then sorts.
- **The item count** isn't shown for Jira backlogs in the backlog list or on the CI page, because counting would
  mean a Jira search per backlog. The backlog's own page shows it.
- **Scope**: `scope` JQL (e.g. `project in (PRG, NAV)`) limits which issues are read, which is useful if the
  field is also set somewhere you don't want counted.

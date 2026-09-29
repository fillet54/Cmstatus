# Shared backlogs (`cmtrack/backlog/`)

Everything about backlogs lives in this folder: about 830 lines across 11 files. To type it in on another
system, read the files in the order below. Each step runs on its own.

A backlog is a ranked list of **top-level ticket keys** shared by a set of teams and related to CIs. Jira can't
hold one ordering across several teams' projects, so the order lives here: each item is just a key plus a
**lexorank** string, and a move rewrites only the moved item's rank. Ticket data (summary, state, affected
CIs) is read live from the ticket source on every view.

## Files, in typing order

| # | File | Lines | What it is |
|---|---|---|---|
| 1 | `rank.py` | 68 | Lexorank: `between(lo, hi)` gives a string that sorts strictly between two others (`None` = open end); `spread(n)` gives n evenly spaced ranks; `validate`. No dependencies. Its tests are `RankTests` in `tests/test_backlog.py`, so type those next. |
| 2 | `schema.sql` | 30 | Tables `backlog` (name, description, teams JSON, source), `backlog_ci` (backlog ↔ ci) and `backlog_item` (backlog, ticket_key, rank, added_at; `UNIQUE(backlog_id, rank)`). |
| 3 | `service.py` | 247 | Domain logic: `list_backlogs`, `create_backlog`, `update_backlog`, `backlog_summary`, `add_backlog_item`, `remove_backlog_item`, `move_backlog_item` (the drag-and-drop callback), `rebalance_backlog`, `pull_backlog`, `backlog_view`. |
| 4 | `routes.py` | 186 | One blueprint: the JSON API under `/api/backlogs…` and the pages under `/backlogs…` (the route list is in its docstring). |
| 5 | `__init__.py` | 26 | `init_db(conn)` runs `schema.sql`; `init_app(app)` registers the blueprint. |
| 6 | `templates/backlog/_macros.html` | 36 | `rank_item` (one draggable row) and `backlog_css()`. |
| 7 | `templates/backlog/list.html` | 45 | `/backlogs`: the table of backlogs and the "New backlog" form. |
| 8 | `templates/backlog/page.html` | 37 | `/backlogs/<b>`: the header, the add-by-key form and `#backlog-items` (which carries the move URL). |
| 9 | `templates/backlog/_items.html` | 31 | The ranked list; htmx swaps it back in after pull, add, remove and rebalance. |
| 10 | `static/backlog.js` | 94 | Native HTML5 drag and drop plus ⤒ ↑ ↓ buttons. Each calls `POST …/move {after, before}`; on a 409 it shows a toast and reloads the list. |
| 11 | `static/backlog.css` | 16 | Styles for `.ui-rank-list` and `.ui-rank-item`. |
| – | `templates/backlog/_ci_card.html` | 16 | Optional: the "Backlogs" card that `templates/ci.html` includes. |

## What it needs from the rest of cmtrack

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
| `service._by_ref`, `_one`, `log`, `to_dict(s)` | row lookup by id or name, the event log, row → dict |
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

-- Backlog tables. Run after cmtrack/schema.sql (backlog_ci references ci).
--
--   backlog ──< backlog_item                     (a shared, ranked list of top-level ticket keys; ticket data is live)
--      └──< backlog_ci >── ci                     (CIs the backlog is about, for navigation and pulling candidates)
--
-- A backlog shared by a set of teams. Jira can't order across projects/teams, so cmtrack keeps the order:
-- each item is just a ticket key and a lexorank string (see rank.py); everything else is read live.
-- The items live in the backlog's store (stores.py): backlog_item below ("sqlite"), or a Jira field ("jira").
CREATE TABLE IF NOT EXISTS backlog (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    teams       TEXT NOT NULL DEFAULT '[]',     -- JSON list of the team names sharing it
    source      TEXT,                           -- ticket source name; NULL = the default source
    store       TEXT NOT NULL DEFAULT 'sqlite', -- rank store holding the items: sqlite | jira | ...
    store_params TEXT NOT NULL DEFAULT '{}',    -- JSON for the store, e.g. {"rank_field": "customfield_12345"}
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS backlog_ci (
    backlog_id INTEGER NOT NULL REFERENCES backlog(id),
    ci_id      INTEGER NOT NULL REFERENCES ci(id),
    PRIMARY KEY (backlog_id, ci_id)
);

-- Items of "sqlite" backlogs.
CREATE TABLE IF NOT EXISTS backlog_item (
    backlog_id INTEGER NOT NULL REFERENCES backlog(id),
    ticket_key TEXT NOT NULL,                   -- a top-level ticket in the source
    rank       TEXT NOT NULL,                   -- lexorank: items sort by plain string comparison
    added_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (backlog_id, ticket_key),
    UNIQUE (backlog_id, rank)
);

-- cmtrack data model
--
--   ci ──< csc                                   (a CSCI is built from CSCs; each CSC is one Jira project/affected-product pair)
--    │
--    └──< release ──< version                    (releases and their builds, synced from the CI's release source or
--           │   ▲          │                      entered by hand; one version gets promoted to "released")
--           └───┘ parent    ├──< manifest_entry >── version   (composite CIs pin child versions)
--                           └──< version_parent >── version   (lineage DAG: what each build was built from)
--
--   Synced rows carry the source's key; fields a user corrected by hand are listed in ``pinned`` and left
--   alone by later syncs. Rows the source stops listing are marked source_state = 'missing', never deleted.
--
--   Tickets are not stored: the ticket source (Jira) is the system of record and is queried live.
--   cmtrack supplies the version set (lineage) and resolves tickets to CSCs via csc's Jira pair.
--
--   backlog ──< backlog_item                     (a shared, ranked list of top-level ticket keys; ticket data is live)
--      └──< backlog_ci >── ci                     (CIs the backlog is about, for navigation and pulling candidates)
--
--   ifc ──< ifc (parent/child)
--    └──< baseline ──< baseline_entry >── ci, version     (the HSCM list: one version per CI)
--
--   event                                        (append-only status accounting log)

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS ci (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    type        TEXT NOT NULL DEFAULT 'CSCI' CHECK (type IN ('CSCI', 'HWCI')),
    kind        TEXT NOT NULL DEFAULT 'simple' CHECK (kind IN ('simple', 'composite')),
    managed     INTEGER NOT NULL DEFAULT 1,     -- 0 = placeholder, known only from scraped HSCMs
    release_source TEXT,                        -- name of a configured release source; NULL = managed by hand
    source_params  TEXT NOT NULL DEFAULT '{}',  -- JSON handed to the source (e.g. Jira project + name patterns)
    require_tested INTEGER NOT NULL DEFAULT 1,  -- release gate: 1 = a version must be 'tested' to be released
    last_sync      TEXT,                        -- JSON: when, counts, and what the source couldn't place
    description TEXT,
    attributes  TEXT NOT NULL DEFAULT '{}',     -- JSON, free-form per-type attributes
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS csc (
    id               INTEGER PRIMARY KEY,
    ci_id            INTEGER NOT NULL REFERENCES ci(id),
    name             TEXT NOT NULL,
    jira_project     TEXT,
    affected_product TEXT,
    team             TEXT,
    UNIQUE (ci_id, name),
    UNIQUE (jira_project, affected_product)     -- the Jira pair identifies exactly one CSC
);

CREATE TABLE IF NOT EXISTS release (
    id                  INTEGER PRIMARY KEY,
    ci_id               INTEGER NOT NULL REFERENCES ci(id),
    name                TEXT NOT NULL,
    kind                TEXT NOT NULL CHECK (kind IN ('planned', 'patch', 'emergency', 'external')),
    status              TEXT NOT NULL DEFAULT 'planned'
                        CHECK (status IN ('planned', 'active', 'released', 'cancelled')),
    parent_id           INTEGER REFERENCES release(id),   -- planned release a patch/emergency patches
    base_version_id     INTEGER REFERENCES version(id),   -- released version a patch/emergency builds on
    released_version_id INTEGER REFERENCES version(id),   -- the version promoted to be this release
    target_date         TEXT,
    released_at         TEXT,
    reason              TEXT,                              -- justification / change request
    source              TEXT NOT NULL DEFAULT 'manual',    -- sync | manual | scraped
    source_key          TEXT,                              -- the release source's id for it (NULL = not synced)
    source_state        TEXT CHECK (source_state IN ('synced', 'missing')),
    pinned              TEXT NOT NULL DEFAULT '[]',        -- JSON list of fields set by hand; syncs leave them
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (ci_id, name)
);

CREATE TABLE IF NOT EXISTS version (
    id           INTEGER PRIMARY KEY,
    release_id   INTEGER NOT NULL REFERENCES release(id),
    ci_id        INTEGER NOT NULL REFERENCES ci(id),       -- denormalized so names are unique per CI
    seq          INTEGER NOT NULL,
    name         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'planned'
                 CHECK (status IN ('planned', 'built', 'tested', 'released', 'rejected', 'external')),
    planned      INTEGER NOT NULL DEFAULT 0,               -- 1 = from the release source; 0 = added by hand (variance)
    planned_date TEXT,
    built_at     TEXT,
    artifact_ref TEXT,
    lineage      TEXT NOT NULL DEFAULT 'auto',             -- auto = parents derived from the releases; manual = set by hand
    source_key   TEXT,
    source_state TEXT CHECK (source_state IN ('synced', 'missing')),
    pinned       TEXT NOT NULL DEFAULT '[]',
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (ci_id, name),
    UNIQUE (release_id, seq)
);

CREATE TABLE IF NOT EXISTS manifest_entry (
    parent_version_id INTEGER NOT NULL REFERENCES version(id),
    child_version_id  INTEGER NOT NULL REFERENCES version(id),
    PRIMARY KEY (parent_version_id, child_version_id)
);

-- Lineage DAG over one CI's versions. Usually a chain; a merge (e.g. an emergency fix folded into
-- the next quarter) gives a version two parents. Range x..y = ancestors(y) - ancestors(x), as in git.
CREATE TABLE IF NOT EXISTS version_parent (
    version_id INTEGER NOT NULL REFERENCES version(id),
    parent_id  INTEGER NOT NULL REFERENCES version(id),
    PRIMARY KEY (version_id, parent_id)
);

CREATE TABLE IF NOT EXISTS ifc (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    parent_id   INTEGER REFERENCES ifc(id),
    description TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS baseline (
    id            INTEGER PRIMARY KEY,
    ifc_id        INTEGER NOT NULL REFERENCES ifc(id),
    name          TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'approved', 'superseded')),
    supersedes_id INTEGER REFERENCES baseline(id),
    source        TEXT NOT NULL DEFAULT 'manual',     -- manual | scraped
    source_ref    TEXT,                               -- e.g. HSCM document number / URL
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    approved_at   TEXT,
    UNIQUE (ifc_id, name)
);

CREATE TABLE IF NOT EXISTS baseline_entry (
    baseline_id INTEGER NOT NULL REFERENCES baseline(id),
    ci_id       INTEGER NOT NULL REFERENCES ci(id),
    version_id  INTEGER NOT NULL REFERENCES version(id),
    PRIMARY KEY (baseline_id, ci_id)
);

-- A backlog shared by a set of teams. Jira can't order across projects/teams, so the order lives here:
-- each item is just a ticket key and a lexorank string (see rank.py); everything else is read live.
CREATE TABLE IF NOT EXISTS backlog (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    teams       TEXT NOT NULL DEFAULT '[]',     -- JSON list of the team names sharing it
    source      TEXT,                           -- ticket source name; NULL = the default source
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS backlog_ci (
    backlog_id INTEGER NOT NULL REFERENCES backlog(id),
    ci_id      INTEGER NOT NULL REFERENCES ci(id),
    PRIMARY KEY (backlog_id, ci_id)
);

CREATE TABLE IF NOT EXISTS backlog_item (
    backlog_id INTEGER NOT NULL REFERENCES backlog(id),
    ticket_key TEXT NOT NULL,                   -- a top-level ticket in the source
    rank       TEXT NOT NULL,                   -- lexorank: items sort by plain string comparison
    added_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (backlog_id, ticket_key),
    UNIQUE (backlog_id, rank)
);

CREATE TABLE IF NOT EXISTS event (
    id        INTEGER PRIMARY KEY,
    at        TEXT NOT NULL DEFAULT (datetime('now')),
    entity    TEXT NOT NULL,
    entity_id INTEGER,
    action    TEXT NOT NULL,
    detail    TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS ix_version_release  ON version(release_id);
CREATE INDEX IF NOT EXISTS ix_release_parent   ON release(parent_id);
CREATE INDEX IF NOT EXISTS ix_entry_version    ON baseline_entry(version_id);
CREATE INDEX IF NOT EXISTS ix_manifest_child   ON manifest_entry(child_version_id);
CREATE INDEX IF NOT EXISTS ix_event_entity     ON event(entity, entity_id);
CREATE INDEX IF NOT EXISTS ix_vparent_parent   ON version_parent(parent_id);

-- Indexes on columns that were added later live in db.py (INDEXES), after the migrations add the columns.

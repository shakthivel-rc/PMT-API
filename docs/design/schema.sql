-- ================================================================================================
-- PMT target schema — PostgreSQL 16 + pgvector 0.8
--
-- The schema the platform grows into (docs/design/02-data-model.md explains every choice).
-- Phase 1 ships the subset marked [P1] as a portable Alembic revision
-- (alembic/versions/20260929a001_project_management_core.py) that also runs on SQLite and MySQL;
-- this file is the PostgreSQL-native form: uuid keys, enums, JSONB, generated tsvectors,
-- partitioned event tables, pgvector and row-level security.
--
-- It is a design artifact, not a migration: it is applied to an empty database to review and
-- test the design (`psql -v ON_ERROR_STOP=1 -f docs/design/schema.sql`), and each part lands in
-- production through its own Alembic revision.
--
-- Conventions
--   * Every tenant-owned table carries workspace_id, and RLS (bottom of file) enforces it.
--   * Primary keys are uuid. The application generates UUIDv7 (time-ordered) so inserts append
--     to the right edge of the B-tree; gen_random_uuid() is only the fallback default.
--   * Soft delete is deleted_at IS NOT NULL; hot indexes are partial on deleted_at IS NULL.
--   * Timestamps are timestamptz, stored UTC.
-- ================================================================================================

CREATE EXTENSION IF NOT EXISTS vector;     -- embeddings (pgvector)
CREATE EXTENSION IF NOT EXISTS pg_trgm;    -- fuzzy title search, `~` in PQL
CREATE EXTENSION IF NOT EXISTS citext;     -- case-insensitive slugs, emails, repo names

-- The tenant for this transaction. The API runs `SET LOCAL app.workspace_id = '<uuid>'` after
-- resolving membership; workers do the same per job. Unset means NULL, and NULL matches no row.
CREATE FUNCTION app_workspace_id() RETURNS uuid LANGUAGE sql STABLE AS
$$ SELECT nullif(current_setting('app.workspace_id', true), '')::uuid $$;

CREATE FUNCTION app_user_id() RETURNS uuid LANGUAGE sql STABLE AS
$$ SELECT nullif(current_setting('app.user_id', true), '')::uuid $$;

-- ------------------------------------------------------------------------------------------------
-- Identity (owned by the base's identity module; shown in PostgreSQL form for the foreign keys).
-- roles, permissions, user_roles, role_permissions, user_sessions and activity_logs keep the
-- shapes of alembic/versions/20260821a001_baseline_schema.py with uuid keys.
-- ------------------------------------------------------------------------------------------------
CREATE TABLE users (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email           citext NOT NULL UNIQUE,
    username        citext NOT NULL UNIQUE,
    first_name      text NOT NULL,
    last_name       text NOT NULL,
    timezone        text NOT NULL DEFAULT 'UTC',
    created_at      timestamptz NOT NULL DEFAULT now(),
    deleted_at      timestamptz
);

-- ------------------------------------------------------------------------------------------------
-- Tenancy  [P1]
-- ------------------------------------------------------------------------------------------------
CREATE TYPE workspace_role AS ENUM ('guest', 'member', 'admin', 'owner');

CREATE TABLE workspaces (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name            text NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
    slug            citext NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]*[a-z0-9]$'),
    cell            text NOT NULL DEFAULT 'cell-1',        -- which stack serves it (01 §1.5)
    plan            text NOT NULL DEFAULT 'free',
    -- Feature switches and limits: {"ai": {"enabled": true, "monthly_token_budget": 5e6},
    -- "auto_assign": "suggest" | "apply" | "off", ...}
    settings        jsonb NOT NULL DEFAULT '{}',
    created_by      uuid NOT NULL REFERENCES users(id),
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    deleted_at      timestamptz
);

CREATE TABLE workspace_members (
    workspace_id    uuid NOT NULL REFERENCES workspaces(id),
    user_id         uuid NOT NULL REFERENCES users(id),
    role            workspace_role NOT NULL DEFAULT 'member',
    -- Default weekly capacity for workload planning; overridden per week in `capacity`.
    weekly_hours    numeric(4,1) NOT NULL DEFAULT 40,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, user_id)
);
CREATE INDEX ix_workspace_members_user ON workspace_members (user_id);

CREATE TABLE teams (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    uuid NOT NULL REFERENCES workspaces(id),
    name            text NOT NULL,
    UNIQUE (workspace_id, name)
);

CREATE TABLE team_members (
    workspace_id    uuid NOT NULL REFERENCES workspaces(id),
    team_id         uuid NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    user_id         uuid NOT NULL REFERENCES users(id),
    PRIMARY KEY (team_id, user_id)
);

-- ------------------------------------------------------------------------------------------------
-- Hierarchy: Workspace > Space > Folder > List  [P1]
-- ------------------------------------------------------------------------------------------------
CREATE TABLE spaces (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    uuid NOT NULL REFERENCES workspaces(id),
    team_id         uuid REFERENCES teams(id),              -- owning team, for velocity
    name            text NOT NULL,
    key             text NOT NULL CHECK (key ~ '^[A-Z][A-Z0-9]{1,9}$'),
    issue_counter   bigint NOT NULL DEFAULT 0,
    settings        jsonb NOT NULL DEFAULT '{}',            -- estimation unit, WIP rules, ...
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    deleted_at      timestamptz,
    UNIQUE (workspace_id, key)
);

CREATE TABLE folders (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    uuid NOT NULL REFERENCES workspaces(id),
    space_id        uuid NOT NULL REFERENCES spaces(id),
    name            text NOT NULL,
    position        integer NOT NULL DEFAULT 0,
    created_at      timestamptz NOT NULL DEFAULT now(),
    deleted_at      timestamptz
);
CREATE INDEX ix_folders_space ON folders (space_id) WHERE deleted_at IS NULL;

CREATE TABLE lists (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    uuid NOT NULL REFERENCES workspaces(id),
    space_id        uuid NOT NULL REFERENCES spaces(id),
    folder_id       uuid REFERENCES folders(id),
    name            text NOT NULL,
    position        integer NOT NULL DEFAULT 0,
    created_at      timestamptz NOT NULL DEFAULT now(),
    deleted_at      timestamptz
);
CREATE INDEX ix_lists_space ON lists (space_id) WHERE deleted_at IS NULL;

CREATE TYPE status_category AS ENUM ('todo', 'in_progress', 'done');

CREATE TABLE workflow_statuses (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    uuid NOT NULL REFERENCES workspaces(id),
    space_id        uuid NOT NULL REFERENCES spaces(id),
    name            text NOT NULL,
    category        status_category NOT NULL,
    position        integer NOT NULL DEFAULT 0,
    color           text,
    wip_limit       integer CHECK (wip_limit > 0),          -- Kanban WIP limit, advisory
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (space_id, name)
);
CREATE INDEX ix_workflow_statuses_space_position ON workflow_statuses (space_id, position);

-- Optional transition rules. A space with no rows here allows any move (Phase 1 behaviour).
CREATE TABLE workflow_transitions (
    workspace_id    uuid NOT NULL REFERENCES workspaces(id),
    space_id        uuid NOT NULL REFERENCES spaces(id),
    from_status_id  uuid NOT NULL REFERENCES workflow_statuses(id),
    to_status_id    uuid NOT NULL REFERENCES workflow_statuses(id),
    PRIMARY KEY (space_id, from_status_id, to_status_id)
);

-- ------------------------------------------------------------------------------------------------
-- Agile planning: sprints and releases
-- ------------------------------------------------------------------------------------------------
CREATE TYPE sprint_state AS ENUM ('planned', 'active', 'closed');

CREATE TABLE sprints (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    uuid NOT NULL REFERENCES workspaces(id),
    space_id        uuid NOT NULL REFERENCES spaces(id),
    name            text NOT NULL,
    goal            text,
    state           sprint_state NOT NULL DEFAULT 'planned',
    starts_on       date,
    ends_on         date,
    -- Frozen at start and at close, so velocity is what was true then, not what the issues
    -- say today after re-estimation.
    committed_points numeric(7,1),
    completed_points numeric(7,1),
    started_at      timestamptz,
    closed_at       timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CHECK (ends_on IS NULL OR starts_on IS NULL OR ends_on >= starts_on)
);
-- At most one active sprint per space.
CREATE UNIQUE INDEX uq_sprints_one_active ON sprints (space_id) WHERE state = 'active';

CREATE TYPE release_status AS ENUM ('unreleased', 'released', 'archived');

CREATE TABLE releases (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    uuid NOT NULL REFERENCES workspaces(id),
    space_id        uuid NOT NULL REFERENCES spaces(id),
    name            text NOT NULL,                          -- "2026.10", "v3.2.0"
    description     text,
    status          release_status NOT NULL DEFAULT 'unreleased',
    release_date    date,
    released_at     timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (space_id, name)
);

-- ------------------------------------------------------------------------------------------------
-- Issues  [P1 core]
-- ------------------------------------------------------------------------------------------------
CREATE TYPE issue_type AS ENUM ('epic', 'story', 'task', 'subtask', 'bug');
CREATE TYPE issue_priority AS ENUM ('urgent', 'high', 'medium', 'low', 'none');

CREATE TABLE issues (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    uuid NOT NULL REFERENCES workspaces(id),
    space_id        uuid NOT NULL REFERENCES spaces(id),
    list_id         uuid NOT NULL REFERENCES lists(id),
    number          integer NOT NULL,                       -- key = spaces.key || '-' || number
    type            issue_type NOT NULL DEFAULT 'task',
    title           text NOT NULL CHECK (length(title) BETWEEN 1 AND 500),
    -- Markdown snapshot of the collaborative document (documents.issue_id), refreshed on
    -- each save; what search, AI and exports read.
    description     text,
    status_id       uuid NOT NULL REFERENCES workflow_statuses(id),
    parent_id       uuid REFERENCES issues(id),
    assignee_id     uuid REFERENCES users(id),
    reporter_id     uuid NOT NULL REFERENCES users(id),
    priority        issue_priority NOT NULL DEFAULT 'none',
    story_points    numeric(5,1) CHECK (story_points >= 0),
    original_estimate_minutes  integer CHECK (original_estimate_minutes >= 0),
    remaining_estimate_minutes integer CHECK (remaining_estimate_minutes >= 0),
    -- Fractional index (utils/rank.py). COLLATE "C" makes ORDER BY byte order, so the database
    -- and the application compare ranks identically whatever the cluster's locale.
    rank            text COLLATE "C" NOT NULL,
    sprint_id       uuid REFERENCES sprints(id),            -- current sprint; history in sprint_issues
    start_date      date,
    due_date        date,
    resolved_at     timestamptz,                            -- set on entering a `done` status
    -- Custom field values keyed by definition id: {"<uuid>": 5, "<uuid>": ["ios","web"]}.
    custom_fields   jsonb NOT NULL DEFAULT '{}',
    version         integer NOT NULL DEFAULT 1,             -- optimistic concurrency
    search          tsvector GENERATED ALWAYS AS (
                        setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
                        setweight(to_tsvector('english', coalesce(description, '')), 'B')
                    ) STORED,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    deleted_at      timestamptz,
    UNIQUE (space_id, number),
    CHECK (parent_id IS NULL OR parent_id <> id)
);
-- Board by list, and by space: cards of a column in rank order. Covering the card fields the
-- board reads would make these index-only; measure before widening them.
CREATE INDEX ix_issues_board_list  ON issues (workspace_id, list_id, status_id, rank)
    WHERE deleted_at IS NULL;
CREATE INDEX ix_issues_board_space ON issues (space_id, status_id, rank)
    WHERE deleted_at IS NULL;
CREATE INDEX ix_issues_assignee_open ON issues (workspace_id, assignee_id)
    WHERE deleted_at IS NULL AND resolved_at IS NULL;       -- "my work", workload
CREATE INDEX ix_issues_parent   ON issues (parent_id) WHERE parent_id IS NOT NULL;
CREATE INDEX ix_issues_sprint   ON issues (sprint_id) WHERE sprint_id IS NOT NULL;
CREATE INDEX ix_issues_due      ON issues (workspace_id, due_date) WHERE due_date IS NOT NULL;
CREATE INDEX ix_issues_updated  ON issues (workspace_id, updated_at DESC);
CREATE INDEX ix_issues_custom   ON issues USING gin (custom_fields jsonb_path_ops);
CREATE INDEX ix_issues_search   ON issues USING gin (search);
CREATE INDEX ix_issues_title_trgm ON issues USING gin (title gin_trgm_ops);

CREATE TYPE issue_link_kind AS ENUM ('blocks', 'relates', 'duplicates', 'clones');

-- Dependencies for Gantt/timeline and "blocked" badges. Stored once, read both ways.
CREATE TABLE issue_links (
    workspace_id    uuid NOT NULL REFERENCES workspaces(id),
    source_issue_id uuid NOT NULL REFERENCES issues(id),
    target_issue_id uuid NOT NULL REFERENCES issues(id),
    kind            issue_link_kind NOT NULL,
    created_by      uuid REFERENCES users(id),
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_issue_id, target_issue_id, kind),
    CHECK (source_issue_id <> target_issue_id)
);
CREATE INDEX ix_issue_links_target ON issue_links (target_issue_id);

CREATE TABLE labels (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    uuid NOT NULL REFERENCES workspaces(id),
    name            citext NOT NULL,
    color           text,
    UNIQUE (workspace_id, name)
);

CREATE TABLE issue_labels (
    workspace_id    uuid NOT NULL REFERENCES workspaces(id),
    issue_id        uuid NOT NULL REFERENCES issues(id),
    label_id        uuid NOT NULL REFERENCES labels(id),
    PRIMARY KEY (issue_id, label_id)
);
CREATE INDEX ix_issue_labels_label ON issue_labels (label_id);

CREATE TABLE issue_watchers (
    workspace_id    uuid NOT NULL REFERENCES workspaces(id),
    issue_id        uuid NOT NULL REFERENCES issues(id),
    user_id         uuid NOT NULL REFERENCES users(id),
    PRIMARY KEY (issue_id, user_id)
);

CREATE TABLE sprint_issues (                                -- sprint scope history (burndown)
    workspace_id    uuid NOT NULL REFERENCES workspaces(id),
    sprint_id       uuid NOT NULL REFERENCES sprints(id),
    issue_id        uuid NOT NULL REFERENCES issues(id),
    points_at_add   numeric(5,1),
    added_at        timestamptz NOT NULL DEFAULT now(),
    removed_at      timestamptz,
    PRIMARY KEY (sprint_id, issue_id, added_at)
);

CREATE TABLE issue_fix_versions (
    workspace_id    uuid NOT NULL REFERENCES workspaces(id),
    issue_id        uuid NOT NULL REFERENCES issues(id),
    release_id      uuid NOT NULL REFERENCES releases(id),
    PRIMARY KEY (issue_id, release_id)
);
CREATE INDEX ix_issue_fix_versions_release ON issue_fix_versions (release_id);

CREATE TABLE issue_comments (                               -- [P1]
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    uuid NOT NULL REFERENCES workspaces(id),
    issue_id        uuid NOT NULL REFERENCES issues(id),
    author_id       uuid NOT NULL REFERENCES users(id),
    body            text NOT NULL,
    search          tsvector GENERATED ALWAYS AS (to_tsvector('english', body)) STORED,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    deleted_at      timestamptz
);
CREATE INDEX ix_issue_comments_issue ON issue_comments (issue_id, created_at)
    WHERE deleted_at IS NULL;
CREATE INDEX ix_issue_comments_search ON issue_comments USING gin (search);

-- Append-only change log: the source of every Agile report (burndown, velocity, CFD, cycle
-- time) and of PQL's WAS / CHANGED operators. Partitioned by month so a year of history is
-- twelve small tables and retention is DROP TABLE, not DELETE.  [P1, unpartitioned]
CREATE TYPE actor_kind AS ENUM ('user', 'integration', 'automation', 'ai');

CREATE TABLE issue_events (
    id              uuid NOT NULL DEFAULT gen_random_uuid(),
    workspace_id    uuid NOT NULL,
    issue_id        uuid NOT NULL,
    actor_id        uuid,
    actor_kind      actor_kind NOT NULL DEFAULT 'user',
    event           text NOT NULL,                          -- created, status_changed, assigned, …
    field           text,
    from_value      text,
    to_value        text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);
CREATE TABLE issue_events_2026_09 PARTITION OF issue_events
    FOR VALUES FROM ('2026-09-01') TO ('2026-10-01');
CREATE TABLE issue_events_2026_10 PARTITION OF issue_events
    FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');
CREATE TABLE issue_events_default PARTITION OF issue_events DEFAULT;
CREATE INDEX ix_issue_events_issue ON issue_events (issue_id, created_at);
CREATE INDEX ix_issue_events_workspace ON issue_events (workspace_id, created_at);

-- ------------------------------------------------------------------------------------------------
-- Custom fields: typed definitions + JSONB values on the issue
-- ------------------------------------------------------------------------------------------------
CREATE TYPE custom_field_type AS ENUM
    ('text', 'number', 'date', 'select', 'multi_select', 'user', 'url', 'checkbox');

CREATE TABLE custom_field_definitions (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    uuid NOT NULL REFERENCES workspaces(id),
    space_id        uuid REFERENCES spaces(id),             -- NULL = every space
    name            text NOT NULL,
    key             text NOT NULL CHECK (key ~ '^[a-z][a-z0-9_]{0,39}$'),  -- PQL: cf.key
    type            custom_field_type NOT NULL,
    options         jsonb NOT NULL DEFAULT '[]',            -- select choices [{id,label,color}]
    required        boolean NOT NULL DEFAULT false,
    position        integer NOT NULL DEFAULT 0,
    created_at      timestamptz NOT NULL DEFAULT now(),
    archived_at     timestamptz,
    UNIQUE (workspace_id, key)
);

-- ------------------------------------------------------------------------------------------------
-- Time tracking and capacity
-- ------------------------------------------------------------------------------------------------
CREATE TYPE time_source AS ENUM ('manual', 'timer', 'automation');

CREATE TABLE time_entries (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    uuid NOT NULL REFERENCES workspaces(id),
    issue_id        uuid NOT NULL REFERENCES issues(id),
    user_id         uuid NOT NULL REFERENCES users(id),
    started_at      timestamptz NOT NULL,
    minutes         integer NOT NULL CHECK (minutes > 0 AND minutes <= 24 * 60),
    note            text,
    source          time_source NOT NULL DEFAULT 'manual',
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_time_entries_issue ON time_entries (issue_id);
CREATE INDEX ix_time_entries_user_week ON time_entries (workspace_id, user_id, started_at);

CREATE TABLE capacity (                                     -- per-week override of weekly_hours
    workspace_id    uuid NOT NULL REFERENCES workspaces(id),
    user_id         uuid NOT NULL REFERENCES users(id),
    week_start      date NOT NULL,
    hours           numeric(4,1) NOT NULL CHECK (hours >= 0),
    PRIMARY KEY (workspace_id, user_id, week_start)
);

CREATE TABLE time_off (                                     -- PTO, for workload and auto-assign
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    uuid NOT NULL REFERENCES workspaces(id),
    user_id         uuid NOT NULL REFERENCES users(id),
    starts_on       date NOT NULL,
    ends_on         date NOT NULL,
    kind            text NOT NULL DEFAULT 'pto',
    CHECK (ends_on >= starts_on)
);
CREATE INDEX ix_time_off_user ON time_off (workspace_id, user_id, starts_on);

-- ------------------------------------------------------------------------------------------------
-- Developer toolchain: VCS and CI/CD
-- ------------------------------------------------------------------------------------------------
CREATE TYPE vcs_provider AS ENUM ('github', 'gitlab', 'bitbucket');

CREATE TABLE vcs_installations (                            -- a GitHub App install, GitLab group token…
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    uuid NOT NULL REFERENCES workspaces(id),
    provider        vcs_provider NOT NULL,
    external_id     text NOT NULL,
    account_login   text NOT NULL,
    -- Envelope-encrypted with INTEGRATIONS_KEY, never SECRET_KEY (CLAUDE.md §7 rule 3).
    token_ciphertext bytea,
    token_key_id    text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (provider, external_id)
);

CREATE TABLE vcs_repositories (                             -- [P1]
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    uuid NOT NULL REFERENCES workspaces(id),
    installation_id uuid REFERENCES vcs_installations(id),
    provider        vcs_provider NOT NULL,
    full_name       citext NOT NULL,
    default_branch  text,
    created_by      uuid REFERENCES users(id),
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (provider, full_name)
);

CREATE TYPE vcs_link_kind AS ENUM ('pull_request', 'branch', 'commit');

CREATE TABLE vcs_links (                                    -- [P1]
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    uuid NOT NULL REFERENCES workspaces(id),
    issue_id        uuid NOT NULL REFERENCES issues(id),
    repository_id   uuid NOT NULL REFERENCES vcs_repositories(id),
    kind            vcs_link_kind NOT NULL,
    external_id     text NOT NULL,                          -- owner/name#123, branch, sha
    url             text NOT NULL,
    title           text,
    state           text,                                   -- open | closed | merged
    head_sha        text,                                   -- joins CI runs to the PR
    author_login    text,
    merged_at       timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (issue_id, kind, external_id)
);
CREATE INDEX ix_vcs_links_issue ON vcs_links (issue_id);
CREATE INDEX ix_vcs_links_sha ON vcs_links (repository_id, head_sha) WHERE head_sha IS NOT NULL;

-- Who changes which paths: aggregated from push events. Feeds auto-assignment (03 §3.4).
CREATE TABLE file_touches (
    workspace_id    uuid NOT NULL REFERENCES workspaces(id),
    repository_id   uuid NOT NULL REFERENCES vcs_repositories(id),
    user_id         uuid NOT NULL REFERENCES users(id),
    path_prefix     text NOT NULL,                          -- directory, two levels deep
    commits         integer NOT NULL DEFAULT 0,
    last_touched_at timestamptz NOT NULL,
    PRIMARY KEY (repository_id, user_id, path_prefix)
);

CREATE TYPE ci_status AS ENUM ('queued', 'running', 'success', 'failure', 'cancelled', 'skipped');

CREATE TABLE ci_pipeline_runs (                             -- CI/CD status on the issue
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    uuid NOT NULL REFERENCES workspaces(id),
    repository_id   uuid NOT NULL REFERENCES vcs_repositories(id),
    provider        text NOT NULL,                          -- github_actions, gitlab_ci, jenkins…
    external_id     text NOT NULL,
    name            text NOT NULL,
    head_sha        text NOT NULL,
    branch          text,
    status          ci_status NOT NULL,
    url             text,
    started_at      timestamptz,
    finished_at     timestamptz,
    UNIQUE (provider, external_id)
);
CREATE INDEX ix_ci_runs_sha ON ci_pipeline_runs (repository_id, head_sha);

CREATE TABLE webhook_deliveries (                           -- [P1] idempotency
    id              text PRIMARY KEY,                       -- provider delivery GUID
    provider        text NOT NULL,
    event           text NOT NULL,
    received_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_webhook_deliveries_received ON webhook_deliveries (received_at);  -- TTL sweep

-- ------------------------------------------------------------------------------------------------
-- Views, automation, documents, notifications, outbox
-- ------------------------------------------------------------------------------------------------
CREATE TYPE view_type AS ENUM ('board', 'list', 'gantt', 'calendar', 'timeline');

CREATE TABLE saved_views (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    uuid NOT NULL REFERENCES workspaces(id),
    owner_id        uuid REFERENCES users(id),              -- NULL = shared with the workspace
    scope_type      text NOT NULL CHECK (scope_type IN ('workspace', 'space', 'folder', 'list')),
    scope_id        uuid NOT NULL,
    name            text NOT NULL,
    view_type       view_type NOT NULL,
    pql             text NOT NULL DEFAULT '',               -- the filter; 04 §4.4
    -- group_by, sort, visible fields, swimlanes, date fields (gantt/calendar), zoom…
    config          jsonb NOT NULL DEFAULT '{}',
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_saved_views_scope ON saved_views (workspace_id, scope_type, scope_id);

CREATE TABLE automation_rules (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    uuid NOT NULL REFERENCES workspaces(id),
    space_id        uuid REFERENCES spaces(id),             -- NULL = workspace-wide
    name            text NOT NULL,
    enabled         boolean NOT NULL DEFAULT true,
    trigger         jsonb NOT NULL,     -- {"event": "vcs.pull_request.merged"}
    conditions      jsonb NOT NULL DEFAULT '[]',  -- [{"pql": "type = bug"}]
    actions         jsonb NOT NULL,     -- [{"type": "set_status_category", "category": "done"},
                                        --  {"type": "log_time", "from": "pr_open_duration"}]
    version         integer NOT NULL DEFAULT 1,
    created_by      uuid REFERENCES users(id),
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_automation_rules_trigger ON automation_rules (workspace_id, (trigger->>'event'))
    WHERE enabled;

CREATE TABLE automation_runs (
    id              uuid NOT NULL DEFAULT gen_random_uuid(),
    workspace_id    uuid NOT NULL,
    rule_id         uuid NOT NULL,
    event_id        uuid NOT NULL,                          -- idempotency: one run per (rule, event)
    depth           smallint NOT NULL DEFAULT 0,            -- loop protection (max 3)
    status          text NOT NULL,                          -- succeeded | failed | skipped
    error           text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (id, created_at),
    UNIQUE (rule_id, event_id, created_at)
) PARTITION BY RANGE (created_at);
CREATE TABLE automation_runs_default PARTITION OF automation_runs DEFAULT;

-- Wiki pages and issue descriptions, edited collaboratively (Yjs). The binary CRDT state is
-- the source of truth for the editor; content_text is the plain snapshot everything else reads.
CREATE TABLE documents (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    uuid NOT NULL REFERENCES workspaces(id),
    space_id        uuid REFERENCES spaces(id),
    parent_id       uuid REFERENCES documents(id),          -- wiki tree
    issue_id        uuid UNIQUE REFERENCES issues(id),      -- set for an issue description
    title           text NOT NULL DEFAULT '',
    ydoc_state      bytea,
    content_text    text NOT NULL DEFAULT '',
    version         bigint NOT NULL DEFAULT 0,
    updated_by      uuid REFERENCES users(id),
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    deleted_at      timestamptz
);
CREATE INDEX ix_documents_space ON documents (workspace_id, space_id, parent_id)
    WHERE deleted_at IS NULL;

CREATE TYPE notification_kind AS ENUM
    ('mentioned', 'assigned', 'commented', 'status_changed', 'due_soon', 'ai_ready');

CREATE TABLE notifications (
    id              uuid NOT NULL DEFAULT gen_random_uuid(),
    workspace_id    uuid NOT NULL,
    user_id         uuid NOT NULL,                          -- recipient
    kind            notification_kind NOT NULL,
    issue_id        uuid,
    actor_id        uuid,
    payload         jsonb NOT NULL DEFAULT '{}',
    read_at         timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);
CREATE TABLE notifications_default PARTITION OF notifications DEFAULT;
CREATE INDEX ix_notifications_inbox ON notifications (user_id, created_at DESC)
    WHERE read_at IS NULL;

-- Transactional outbox: written in the same transaction as the change, relayed to Redis
-- Streams / Kafka for realtime, search indexing, automation and embeddings.
CREATE TABLE outbox (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workspace_id    uuid NOT NULL,
    aggregate_type  text NOT NULL,                          -- issue, comment, document, …
    aggregate_id    uuid NOT NULL,
    event_type      text NOT NULL,                          -- issue.moved, comment.created, …
    payload         jsonb NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    published_at    timestamptz
);
CREATE INDEX ix_outbox_unpublished ON outbox (id) WHERE published_at IS NULL;

-- ------------------------------------------------------------------------------------------------
-- AI
-- ------------------------------------------------------------------------------------------------
CREATE TYPE ai_job_status AS ENUM ('queued', 'running', 'succeeded', 'failed');

CREATE TABLE ai_jobs (                                      -- [P1]
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    uuid NOT NULL REFERENCES workspaces(id),
    kind            text NOT NULL,          -- summarize_issue_thread, estimate_points, suggest_assignee, draft_tickets
    subject_type    text NOT NULL,
    subject_id      uuid NOT NULL,
    status          ai_job_status NOT NULL DEFAULT 'queued',
    priority        smallint NOT NULL DEFAULT 0,            -- interactive 0, background 10
    input_hash      text,
    prompt_version  text,
    model           text,
    result          jsonb,
    error           text,
    attempts        smallint NOT NULL DEFAULT 0,
    input_tokens    integer,
    output_tokens   integer,
    cost_usd        numeric(10,6),
    requested_by    uuid REFERENCES users(id),
    available_at    timestamptz NOT NULL DEFAULT now(),
    lease_expires_at timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    started_at      timestamptz,
    finished_at     timestamptz
);
CREATE INDEX ix_ai_jobs_claim ON ai_jobs (priority, available_at)
    WHERE status IN ('queued', 'running');
CREATE INDEX ix_ai_jobs_subject ON ai_jobs (workspace_id, subject_id, kind, input_hash);

CREATE TABLE ai_usage (                                     -- budgets and the cost dashboard
    workspace_id    uuid NOT NULL REFERENCES workspaces(id),
    month           date NOT NULL,
    feature         text NOT NULL,
    requests        integer NOT NULL DEFAULT 0,
    input_tokens    bigint NOT NULL DEFAULT 0,
    output_tokens   bigint NOT NULL DEFAULT 0,
    cost_usd        numeric(12,4) NOT NULL DEFAULT 0,
    PRIMARY KEY (workspace_id, month, feature)
);

-- Every AI suggestion a person can accept or reject: estimates, assignees, drafted tickets.
-- The accept/reject decision is the label the evaluation harness learns from (03 §3.6).
CREATE TABLE ai_suggestions (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    uuid NOT NULL REFERENCES workspaces(id),
    issue_id        uuid REFERENCES issues(id),
    kind            text NOT NULL,                          -- story_points | assignee | ticket_draft
    suggestion      jsonb NOT NULL,
    rationale       text,
    evidence        jsonb NOT NULL DEFAULT '[]',            -- cited issue ids, PRs, paths
    model           text,
    prompt_version  text,
    accepted        boolean,
    decided_by      uuid REFERENCES users(id),
    decided_at      timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_ai_suggestions_issue ON ai_suggestions (issue_id, kind);

-- Vector store for retrieval (RAG). One row per chunk per embedding model, so a model change
-- is a backfill beside the old vectors and a switch, not a rewrite in place.
CREATE TABLE embeddings (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    uuid NOT NULL REFERENCES workspaces(id),
    source_type     text NOT NULL,          -- issue | comment | document | pull_request
    source_id       uuid NOT NULL,
    chunk_index     smallint NOT NULL DEFAULT 0,
    model           text NOT NULL,          -- e.g. voyage-3.5 (1024 dims)
    content_hash    text NOT NULL,          -- skip re-embedding unchanged text
    content         text NOT NULL,
    embedding       vector(1024) NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_type, source_id, chunk_index, model)
);
-- HNSW with cosine distance. Filtered by workspace_id (and RLS); pgvector 0.8's iterative
-- index scans (SET hnsw.iterative_scan = relaxed_order) keep recall up under that filter.
-- A tenant past ~5M chunks gets its own partial index (WHERE workspace_id = '…').
CREATE INDEX ix_embeddings_hnsw ON embeddings USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
CREATE INDEX ix_embeddings_source ON embeddings (workspace_id, source_type, source_id);

-- ------------------------------------------------------------------------------------------------
-- Reporting read models (refreshed by the scheduler, or incrementally from the outbox)
-- ------------------------------------------------------------------------------------------------
-- Cumulative flow: issues per status category at the end of each day, per space.
CREATE MATERIALIZED VIEW cfd_daily AS
SELECT i.workspace_id,
       i.space_id,
       d::date                                    AS day,
       ws.category,
       count(*)                                   AS issues
FROM issues i
CROSS JOIN LATERAL generate_series(date_trunc('day', i.created_at),
                                   date_trunc('day', coalesce(i.deleted_at, now())),
                                   interval '1 day') AS d
CROSS JOIN LATERAL (
    SELECT e.to_value::uuid AS status_id
    FROM issue_events e
    WHERE e.issue_id = i.id AND e.event IN ('created', 'status_changed')
      AND e.created_at < d + interval '1 day'
    ORDER BY e.created_at DESC
    LIMIT 1
) AS s
JOIN workflow_statuses ws ON ws.id = s.status_id
GROUP BY 1, 2, 3, 4
WITH NO DATA;

-- ------------------------------------------------------------------------------------------------
-- Row-level security: the database half of tenant isolation (01 §1.5)
-- ------------------------------------------------------------------------------------------------
-- The application connects as pmt_app, which does NOT have BYPASSRLS. Migrations run as the
-- table owner. FORCE makes the policy apply even to the owner, so a migration that reads data
-- must set app.workspace_id like anything else — or run as a dedicated maintenance role.
DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'teams', 'team_members', 'spaces', 'folders', 'lists', 'workflow_statuses',
        'workflow_transitions', 'sprints', 'releases', 'issues', 'issue_links', 'labels',
        'issue_labels', 'issue_watchers', 'sprint_issues', 'issue_fix_versions',
        'issue_comments', 'issue_events', 'custom_field_definitions', 'time_entries',
        'capacity', 'time_off', 'vcs_installations', 'vcs_repositories', 'vcs_links',
        'file_touches', 'ci_pipeline_runs', 'saved_views', 'automation_rules',
        'automation_runs', 'documents', 'notifications', 'outbox', 'ai_jobs', 'ai_usage',
        'ai_suggestions', 'embeddings'
    ] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I '
            'USING (workspace_id = app_workspace_id()) '
            'WITH CHECK (workspace_id = app_workspace_id())', t);
    END LOOP;
END $$;

-- The two tables read *before* a tenant is chosen: "which workspaces am I in" and the
-- workspace row itself. Visible to the member, through the membership.
ALTER TABLE workspace_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace_members FORCE ROW LEVEL SECURITY;
CREATE POLICY member_visibility ON workspace_members
    USING (workspace_id = app_workspace_id() OR user_id = app_user_id())
    WITH CHECK (workspace_id = app_workspace_id());

ALTER TABLE workspaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspaces FORCE ROW LEVEL SECURITY;
CREATE POLICY workspace_visibility ON workspaces
    USING (id = app_workspace_id()
           OR id IN (SELECT workspace_id FROM workspace_members WHERE user_id = app_user_id()))
    WITH CHECK (id = app_workspace_id() OR created_by = app_user_id());

-- Paths that must find the tenant before they can set it go through SECURITY DEFINER functions
-- that answer exactly one question and nothing else.
--   * A webhook knows a repository, not a workspace.
CREATE FUNCTION resolve_repository_workspace(p_provider vcs_provider, p_full_name citext)
RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS
$$ SELECT workspace_id FROM vcs_repositories WHERE provider = p_provider AND full_name = p_full_name $$;

--   * A worker claims the next job across all tenants, then sets app.workspace_id to the job's.
CREATE FUNCTION claim_ai_job(p_lease interval)
RETURNS TABLE (job_id uuid, job_workspace_id uuid, job_kind text, job_attempts smallint)
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = public AS
$$
    UPDATE ai_jobs j
       SET status = 'running', attempts = j.attempts + 1, started_at = now(),
           lease_expires_at = now() + p_lease
     WHERE j.id = (
            SELECT id FROM ai_jobs
             WHERE (status = 'queued' AND available_at <= now())
                OR (status = 'running' AND lease_expires_at < now())
             ORDER BY priority, available_at
             LIMIT 1
             FOR UPDATE SKIP LOCKED)
    RETURNING j.id, j.workspace_id, j.kind, j.attempts
$$;

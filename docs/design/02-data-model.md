# 2. Data model

This document explains the choices behind the schema. The complete PostgreSQL DDL is in
[schema.sql](schema.sql). It was applied to PostgreSQL 16 with pgvector 0.8.6 on 2026-09-29:
46 tables including partitions, and a row-level-security smoke test passed (§2.8). The
Phase 1 subset ships as a portable Alembic revision,
`alembic/versions/20260929a001_project_management_core.py`, whose test suite passes on SQLite
and on MySQL 8.

## 2.1 Entity overview

```
users ─┬─< workspace_members >─ workspaces ─┬─< teams ─< team_members
       │                                    ├─< spaces ─┬─< folders ─< lists
       │                                    │           ├─< workflow_statuses (+ transitions)
       │                                    │           ├─< sprints ─< sprint_issues
       │                                    │           └─< releases ─< issue_fix_versions
       │                                    ├─< custom_field_definitions
       │                                    ├─< labels, saved_views, automation_rules
       │                                    ├─< vcs_installations ─< vcs_repositories
       │                                    └─< documents (wiki tree, issue descriptions)
       │
       └──────── issues ──┬── parent_id ─▶ issues          (epic > story > task > sub-task; bug anywhere)
          (list, status,  ├─< issue_comments
           sprint, space) ├─< issue_events  (append-only, monthly partitions)
                          ├─< issue_links   (blocks / relates / duplicates / clones)
                          ├─< vcs_links ── ci_pipeline_runs (joined on head_sha)
                          ├─< time_entries
                          ├─< issue_labels, issue_watchers
                          ├─< ai_suggestions
                          └── custom_fields  (JSONB column, typed by the definitions)

   ai_jobs · ai_usage · embeddings (pgvector) · notifications · outbox · webhook_deliveries
```

## 2.2 Principles

1. **The tenant is on every row.** `workspace_id` is on every tenant-owned table, including
   leaf tables that could reach it through a join. A redundant column is the price of a
   row-level-security (RLS) predicate that is a plain indexed equality check, and of never
   having to ask "whose row is this?" with a join.
2. **Keys.** Primary keys are UUIDs, and the application generates them as UUIDv7. Opaque IDs
   do not leak record counts. A time-ordered value keeps B-tree inserts at the right-hand edge
   of the index, which matters on `issues`, `issue_events` and `embeddings`. Phase 1 uses
   `CHAR(36)`, as the base project does; the PostgreSQL cut-over changes those columns to
   `uuid`.
3. **Every change is an event.** `issue_events` is appended to in the same transaction as each
   change. Burndown, velocity, cumulative flow, cycle time, and PQL's `WAS` and `CHANGED`
   operators all read from it. A current-state row cannot answer "what was true on Tuesday?".
4. **Derived data is labelled as derived.** The `search` tsvectors, `cfd_daily`, `ai_usage`,
   `embeddings` and the OpenSearch documents can all be rebuilt from the source tables, so
   losing one of them costs time but no data.
5. **Soft delete where history matters.** Issues, comments, lists and documents carry
   `deleted_at`, because reports and audits still need them. Hot indexes are partial on
   `deleted_at IS NULL`, so deleted rows cost nothing on the read path.

## 2.3 Hierarchy

The hierarchy is `Workspace > Space > Folder > List`, following ClickUp.

- **A space owns two things.** It owns the issue key: `PAY` in `PAY-142`, with the number
  taken from `spaces.issue_counter` by one `UPDATE` in the same transaction that inserts the
  issue. It also owns the workflow: `workflow_statuses`, each with a *category* of `todo`,
  `in_progress` or `done`. Reports and automations work with categories, never with status
  names.
- **Folders are optional.** A list can sit directly in a space.
- **Issues belong to exactly one list.** Moving an issue between lists keeps its key. Moving it
  to another space gives it a new key. The old key must keep resolving, because commits and
  links already quote it, so that move is a Phase 2 feature with an `issue_key_aliases` table.

### Issue hierarchy

There is one `issues` table with a `type` column rather than a table per type, because every
type stores the same fields. A table per type would turn "my work" into a five-way `UNION`,
and changing an issue's type into a move between tables. The parent rules are data
(`models/issue.ALLOWED_PARENT_TYPES`):

| Type | Can sit under | Parent required? |
|---|---|---|
| epic | (top level only) | no |
| story | epic | no |
| task | epic, story | no |
| sub-task | story, task | **yes** |
| bug | epic, story, task, sub-task | no |

The hierarchy is an adjacency list (`parent_id`), with a cycle check when an issue is
re-parented. The type rules cap the depth at 5, so a recursive CTE over it is cheap. If
epic roll-ups ("points remaining under this epic") measure slow, the Phase 2 option is a
closure table maintained by a trigger. It is additive and needs no rewrite.

## 2.4 Board ordering: fractional ranks

`issues.rank` is a base-36 string key (`utils/rank.py`), compared in byte order (`COLLATE "C"`
on PostgreSQL). Moving a card writes one new key, strictly between its neighbours' keys, to
one row. An integer position would instead rewrite every card below the drop point, and two
people dragging at the same moment would overwrite each other's order.

- **The server picks the second neighbour.** The client's view of the column may be stale, so
  the server anchors on the card the client named *above* the drop and reads the real card
  below it from the database. The integration test for this is
  `test_a_stale_neighbour_does_not_break_the_drop`.
- **Keys are rebalanced when they grow.** Keys lengthen when many drops land in the same gap.
  Once a key passes 48 characters, the column is given fresh, evenly spaced keys in the same
  transaction. A 3,000-drop fuzz test checks that the database order always equals the order
  people made.
- **Only characters every database sorts the same way.** The alphabet is lower-case
  alphanumerics, so MySQL's case-insensitive collations and PostgreSQL's locale collations
  agree with Python. This was confirmed by running the board tests on MySQL 8.

## 2.5 Custom fields: typed definitions plus JSONB values

- `custom_field_definitions` holds each field's name, a PQL key (`cf.severity`), a type from
  the `custom_field_type` enum, options for select fields, and whether the field is required.
- Values live in `issues.custom_fields` as a JSONB object keyed by definition ID.
- **Why not entity-attribute-value (EAV)?** With EAV, every filter or sort on a custom field
  becomes a self-join per field, and a board showing five custom fields needs five joins per
  card. JSONB keeps the issue in one row. The `jsonb_path_ops` GIN index serves containment
  filters (`cf.platform = "ios"`) across every field at once.
- **Hot fields get their own index.** When a workspace sorts or range-filters on one field
  constantly (`cf.severity > 2`), an admin action creates an expression index such as
  `((custom_fields->>'<id>')::numeric) WHERE workspace_id = …`. The PQL compiler uses it
  automatically.
- **Validation happens on write**, against the definition's type and options, in the API.
  JSONB does not constrain its contents, so the API has to.

## 2.6 Agile: sprints, releases, reports

- **`sprints`** allow at most one active sprint per space, enforced by a partial unique index.
  `committed_points` is frozen when the sprint starts and `completed_points` when it closes,
  so velocity reflects what was true at the time, not today's re-estimates.
- **`sprint_issues`** records scope history: which issues were added or removed, when, and
  their points at that moment. The burndown's "scope change" line comes from here.
- **`releases`** with **`issue_fix_versions`** provide version management. Release notes are
  generated from the issues resolved in a release.
- **Reports are computed from `issue_events`:**
  - *Burndown*: remaining points per day, from the time each issue entered a `done` category
    within the sprint window.
  - *Velocity*: `completed_points` per closed sprint.
  - *Cumulative flow*: the count of issues per status category at each day's end. The
    `cfd_daily` materialized view in the schema reconstructs the status of each issue at each
    day from the event log.
  - *Cycle and lead time*: the time from the first `in_progress` event to `done`, and from
    creation to `done`.

## 2.7 Time tracking and capacity

> **Built on 2026-10-05, with changes.** Time is stored in seconds, not minutes. Remaining time
> is derived, not stored. A running timer has its own table. Entries carry a `work_date`.
> 07-time-tracking.md records what was built and why. Capacity, below, is still to come.

- **`time_entries`** records minutes against an issue, with a `source` of manual, timer or
  automation (for example, a rule that logs time when a PR merges). Estimated against actual
  time is `issues.original_estimate_minutes` compared with `SUM(time_entries.minutes)`.
- **Capacity.** Each member has `workspace_members.weekly_hours`, which `capacity` rows can
  override week by week, minus `time_off`. Workload for a person in a sprint is the sum of
  their open story points, or estimated hours, compared with that capacity. The same
  calculation feeds the workload view and the availability term of auto-assignment
  ([03-ai-strategy.md](03-ai-strategy.md) §3.4).

## 2.8 Tenant isolation in the database: row-level security

Every tenant table has `ENABLE` and `FORCE ROW LEVEL SECURITY` with the policy:

```sql
USING (workspace_id = app_workspace_id()) WITH CHECK (workspace_id = app_workspace_id())
-- app_workspace_id() = current_setting('app.workspace_id', true)::uuid
```

- **Setting the tenant.** The API connects as `pmt_app`, which does **not** have `BYPASSRLS`.
  Once `workspace_access` has resolved membership, the API runs
  `SET LOCAL app.workspace_id = …` inside the request's transaction. `SET LOCAL` expires at
  `COMMIT`, which is what makes this safe behind a transaction-mode PgBouncer, where
  connections are shared between requests.
- **Two tables are read before a tenant is chosen.** These are `workspaces` and
  `workspace_members`. Their policies let a user see rows reached through their own
  membership (`app.user_id`).
- **Some paths must find the tenant before they can set it.** Each goes through a
  `SECURITY DEFINER` function that answers exactly one question:
  - `resolve_repository_workspace()`, for webhooks.
  - `claim_ai_job()`, for workers. It claims across tenants with `FOR UPDATE SKIP LOCKED`,
    and the worker then sets the job's workspace for everything else it reads.

Smoke test on 2026-09-29, run as `pmt_app`:

| Test | Result |
|---|---|
| No tenant set | 0 rows visible |
| Tenant A set | only A's rows visible |
| Insert a row into tenant B while tenant A is set | refused: `new row violates row-level security policy` |
| "My workspaces" with only `app.user_id` set | returns exactly the caller's workspaces |

## 2.9 Scale notes

| Table | Growth | Plan |
|---|---|---|
| `issue_events`, `notifications`, `automation_runs` | Largest, and append-only | Monthly range partitions. Old partitions are detached and archived to object storage. |
| `issues` | About 10⁷ per large cell | The partial and covering indexes above. Past about 50M rows in a cell, split into another cell rather than sharding the table. |
| `embeddings` | Several chunks per issue, comment and document | HNSW index filtered by `workspace_id`, with iterative scans for recall. A partial index per very large tenant. A dedicated vector store only past about 50–100M vectors. |
| `webhook_deliveries` | One row per delivery | A 30-day TTL sweep on `received_at`. |
| `outbox` | Transient | Rows are deleted, or their partitions dropped, once published and acknowledged. |

## 2.10 Phase 1 compared with the target

| Area | Phase 1 (shipped, portable) | Target (PostgreSQL) |
|---|---|---|
| Keys | `CHAR(36)` UUID strings | `uuid` (UUIDv7) |
| Enums | `String` columns with application-side tuples | PostgreSQL `ENUM` types |
| Tenant isolation | Application layer (`workspace_access` plus a filter on every query) | The same, plus RLS |
| Events | `issue_events`, unpartitioned | Partitioned monthly |
| Time tracking (entries, timers, estimates) | **built** 2026-10-05 | Revision `20261005a001`; 07-time-tracking.md says how it departs from §2.7 |
| Custom fields, sprints, releases, capacity, automation, documents, embeddings | not yet | Tables in `schema.sql` |
| Queue | `ai_jobs` table with `SKIP LOCKED` | The same table, plus a `priority` lane and the `claim_ai_job()` function |

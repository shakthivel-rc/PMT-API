# 7. Time tracking

Time tracking was planned for Phase 2 (06 §6.4) and was brought forward on 2026-10-05. This
document records what was built, the decisions behind it, and what was left for later. It
also shows how the design in 02 §2.7 and `schema.sql`, which targets PostgreSQL, was adapted
to the MySQL 8 schema that actually runs.

The research behind it is in `../../../time-tracking-research.md`. It compares how Jira,
ClickUp, YouTrack, GitLab, Asana, Toggl, Harvest and Clockify handle time, and maps this
codebase.

## 7.1 What it does

- **Time on a ticket.** Members log time against an issue. Each issue shows its time spent,
  its estimate, the time remaining, and a progress bar. It also shows its *roll-up* (the
  issue plus all its sub-issues, at any depth), how much each person logged, and the full
  work log. Board cards and list rows show logged time against the estimate.
- **Time by person**, which covers the "vice versa" requirement. The Timesheet screen shows
  one person's week, or everyone's, as a grid of issues by days with totals both ways, plus
  the list of entries. Native Jira Cloud has no such screen; this gap is the main reason
  teams buy Tempo.
- **One running timer per person.** Start a timer from an issue; the header shows it on every
  screen. Stopping it saves an entry. Starting a timer on another issue stops and saves the
  first one.
- **Workspace settings.** Admins can switch tracking on or off and set the length of a day
  and a week (used when someone types `1d` or `1w`). They also set who sees whose time and
  which day a week starts on.

## 7.2 Decisions

| # | Question | Decision | Why |
|---|---|---|---|
| 1 | Unit | **Whole seconds**, everywhere in the API | A timer measures seconds. Rounding each entry to a minute loses up to 59 s per entry, permanently. Clockify's lesson is to store full precision and round only for display. Display is `3h 20m`. |
| 2 | Running timer | **`active_timers` table, primary key `user_id`** | "One timer per person" is then the primary key, which SQLite, MySQL and PostgreSQL all enforce. An `ended_at IS NULL` design would need a partial unique index, which MySQL lacks. |
| 3 | Days and time zones | **`work_date` (the logger's calendar day) plus an optional `started_at` (an instant, stored as naive UTC)** | Timesheets bucket by `work_date`, so a week never depends on the server's zone or on a `users.timezone` column that does not exist. `started_at` is optional because inventing "now" for "3h yesterday" is the most common worklog fault in Jira. |
| 4 | Estimates | **One `issues.time_estimate_seconds`. Remaining is derived:** estimate minus spent, floored at 0 | Jira's stored remaining estimate stops matching the original once work is logged, which is its most-reported confusion. ClickUp and GitLab use a single estimate. |
| 5 | Optimistic locking | **Logging time never writes the `issues` row** | Writing it would bump `version` and give a 409 to anyone editing the issue at that moment. Changing the estimate *is* an issue edit and goes through `PATCH` with `version`. |
| 6 | Totals | **Summed on read**, as a fourth batched query in `cards()` | No cached counter can drift (GitLab's resets when an issue moves). The board becomes 6 queries, flat in the number of cards. A test (`test_time_totals_do_not_cost_a_query_per_card_or_per_entry`) pins that. |
| 7 | Roll-ups | **Full subtree, *own* and *roll-up* kept apart** | Jira rolls up one level and never to epics; that limit is its JRACLOUD-70150, with 379 votes. The walk is bounded at five levels by the type rules, one tenant-filtered query per level. |
| 8 | Permissions | Guest reads. Member logs and edits their own time. Admin also logs for others and edits anyone's. | Workspace roles, not global RBAC slugs, which apply platform-wide. "Log for someone else" is its own admin capability, as in YouTrack and Tempo. The author of an entry (`user_id`) and who typed it in (`created_by`) never change. |
| 9 | Who sees whose time | **Workspace setting `visibility`: `everyone` (default) or `own`** | Under `own`, members see issue *totals* but only their own entries; admins see all. Jira's most persistent complaint is that everyone sees all worklogs. Germany's works councils have a say over tools that monitor colleagues (BetrVG §87(1)6). |
| 10 | Deletion and audit | **Soft delete**, plus `time_logged`, `time_updated` and `time_deleted` rows in `issue_events` (durations only, never notes) | Same rules as issues. When an admin acts on someone else's time, or settings change, the act is also written to `activity_logs`. |
| 11 | Per-person endpoint | `GET /workspaces/{w}/time-entries` and `/time-report`, workspace-scoped | Tenancy stays in the URL. Only the timer is person-scoped (`/timer`), because the header is not inside any workspace. |
| 12 | Settings | **`workspaces.settings` JSON**, key `time_tracking`, validated by `TimeTrackingSettings` | This is the document `schema.sql` designs. It is nullable with the default applied in code, because MySQL cannot give a JSON column a literal DEFAULT. |
| 13 | Idempotency | Timer start, stop and discard are naturally idempotent. Logging time is not. | There is no `Idempotency-Key` yet. On a timed-out POST, the SPA's copy already says to reload before sending again. |

## 7.3 Data model

Revision `20261005a001` is written by hand and portable:

```
time_entries   id, workspace_id, issue_id, user_id (worked), created_by (typed it in),
               work_date DATE, started_at DATETIME NULL, duration_seconds INT (1..86400),
               note TEXT, source ('manual'|'timer'|'automation'), created_at, updated_at,
               deleted, deleted_at
               ix (issue_id, work_date) · (workspace_id, user_id, work_date) · (workspace_id, work_date)
active_timers  user_id PK, workspace_id, issue_id, started_at, work_date, note
issues         + time_estimate_seconds INT NULL (0..1,000 h)
workspaces     + settings JSON NULL
```

Ranges are enforced by pydantic, not CHECK constraints, following the Phase 1 convention. An
entry is capped at one day, which is the same rule as `schema.sql`'s `minutes <= 24*60`.

`alembic/env.py` now loads every model module. Until this change, `make revision` drafted a
`drop_table` for all 13 project-management tables. Against a MySQL 8.0 database migrated to
head, autogenerate now reports zero differences, which also confirms that model and revision
index names match.

## 7.4 API

| Method and path | Role | What it does |
|---|---|---|
| `GET …/issues/{i}/time-tracking` | guest | Summary (own and roll-up), per-person totals, newest 200 entries, settings, `can_log` |
| `POST …/issues/{i}/time-entries` | member | Log time. `user_id` is for admins logging on someone's behalf |
| `PATCH …/time-entries/{e}` | member | Change own entry (admins: any) |
| `DELETE …/time-entries/{e}` | member | Soft delete own entry (admins: any) |
| `GET …/time-entries?user_id=&issue_id=&date_from=&date_to=` | guest | Paged list with `total_seconds` across all pages |
| `GET …/time-report?date_from=&date_to=&user_id=` | guest | Sums by person, issue and day, for up to 92 days |
| `POST …/issues/{i}/timer` | member | Start your timer. Answers with any `stopped_entry` |
| `GET /timer` · `POST /timer/stop` · `DELETE /timer` | any signed-in user | Your timer: read (`null` when none), stop and log, discard |
| `GET` / `PATCH …/settings/time-tracking` | guest / admin | Settings. Changes are audited |

`IssueCard` gains `time_estimate_seconds` and `time_spent_seconds`, and `IssueCreate` and
`IssueUpdate` gain the estimate.

Refusals follow the house rules:

- 404 for another tenant's id, or for another member's entry under `own` visibility.
- 403 for an entry you can see but not change, or for asking for someone else's time under
  `own`.
- 422 when tracking is switched off, a date has not started anywhere yet (later than today in
  UTC+14), or a start time is in the future.

## 7.5 Edge cases, and what the system does

- **Forgotten timer.** Stopping a timer that ran more than a day saves one day and answers
  `capped: true`. The SPA tells the person to correct the entry. Refusing outright would lose
  the time.
- **Very short timer.** It is saved as at least 1 second, never as nothing.
- **Timer whose issue was deleted, or whose owner left the workspace.** It is discarded when
  stopped (`entry: null`), and `GET /timer` hides a timer in a workspace you have left.
- **Clock skew.** The API returns `elapsed_seconds`, measured as it answered. The SPA ticks
  from the moment it received that value, never from "now minus `started_at`".
- **Time zones.** `started_at` must carry an offset; a naive value is refused, not guessed.
  PyMySQL and SQLite drop offsets silently, so `10:00+05:30` would otherwise be stored as
  10:00 UTC. Day keys in the SPA come from local date parts, never from `toISOString()`.
- **Deleted issue.** Its time disappears from timesheets and reports, like the issue itself.
  The rows remain.
- **Deleted account.** Cannot have time logged on its behalf. `services/workspace.is_member`
  alone would accept a deleted account, so a separate check is made.

## 7.6 Verified

| Gate | Result |
|---|---|
| `make lint`, `make typecheck` | Clean; ledger unchanged at 353 |
| Backend suite, SQLite | 562 passed: 70 new time-tracking tests, plus 12 checks the route-inventory sweep adds for the new routes. Full coverage 81.4%. |
| Backend, unit subset | 53.7% against the floor of 53. The new logic in `utils/` and the permission rules are unit-tested to keep the floor's headroom. |
| MySQL 8.0 | Migration up → down → up → base → up. Autogenerate shows no drift. Time-tracking, issue, webhook and migration suites pass. |
| `npm run verify` | 630 tests. Coverage rose on every metric: lines 94.1%, branches 92.9%, functions 97.3% |
| `npm run build` | Initial JS 195.1 / 198 kB (+1.2 kB); total 276.9 kB, ceiling raised to 288 with a dated note |

One test failed on MySQL and is unrelated to this change: `test_ai_summary_jobs`. Its job
claim compares `available_at <= now`, and MySQL rounds fractional seconds *up* on write, so a
job queued in the same second can look like it is in the future. A different test fails on
each run.

## 7.7 Not built yet, and what each item needs

| Item | What it needs |
|---|---|
| `#time 1h 30m` in commit messages (Jira Smart Commits, GitLab `@1h30m`) | GitHub `push` events (only `pull_request` is handled today). A *verified* link from GitHub users to PMT users, not email matching. De-duplication per (commit SHA, command), because rebases replay commits. |
| "Log PR open time on merge" automation | `PullRequestEvent` to parse `created_at` and `merged_at`, and the automation engine (Phase 2). `source='automation'` is reserved for it. |
| Approvals, locking periods | A status column on entries (or a periods table) and an admin override rule. The `source` column and the soft-delete audit trail are the hooks. The **billable flag** shipped with reports (doc 08 §12): `time_entries.billable`, defaulting from the setting `billable_default`. |
| Capacity and workload (`weekly_hours`, `capacity`, `time_off`) | A member-management screen to edit them. The tables in `schema.sql` are unchanged. |
| Logging time from the timesheet grid | An issue picker. No combobox primitive exists, and `cmdk` would be a new dependency. |
| Idle and long-timer reminders | Notifications, which do not exist yet. |
| Rounding in reports (Toggl-style intervals) | **Built** with reports (doc 08 §5.5): a report option, applied per entry; storage keeps whole seconds. |
| PostgreSQL cut-over | An `ended_at IS NULL` partial unique index could replace `active_timers`. Row-level security on the new tables: `/timer` looks up the row by `user_id` and then sets the tenant context from it. |

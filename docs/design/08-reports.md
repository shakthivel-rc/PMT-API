# 8. Reports

Built on 2026-10-05 from the reports plan. PMT now generates reports over time, projects,
sprints and flow, shows them as a chart plus a table, exports them as CSV, XLSX or PDF, and
saves and emails them on a schedule. Sections are numbered to match the plan, and code
comments cite them (`08-reports.md §5.5`, for example), so a section is renamed only with
its citations.

## 1. Goal

One place where a person can answer "where did the time go?", "how is this project doing?"
and "how did the sprint go?", without exporting to a spreadsheet first, and where the answer
on screen, in the downloaded file and in the Monday email is the same answer. Every report is
computed by one engine (§5.3) into one result shape, and every renderer reads that shape.

## 2. What Jira and ClickUp do, and what PMT took

| From | Taken | Left out, and why |
|---|---|---|
| Jira | Sprint report (completed, not completed, removed, added after start marked `*`), burndown with a guideline and scope steps, burnup, velocity from frozen commitments, cumulative flow, control chart with median and rolling average, created vs resolved | Gadget dashboards with free layout: a fixed grid of pinned saved reports covers the need (§4, R3) |
| ClickUp | Time reporting grouped by one or two levels, the timesheet grid, billable filter, an export permission per role, rounding to an interval, scheduled emailed reports | Arbitrary custom widgets and formula fields: they need custom fields, which do not exist yet |
| Both | Saved filters as shareable URLs, private or workspace visibility | — |

## 3. What the reports read

- **Issues** with their status (and its category: `todo`, `in_progress`, `done`), type,
  priority, assignee, reporter, story points (integer), estimate, parent (epics roll up).
- **`issue_events`**: `status_changed`, `assigned`, `points_changed` and `estimate_changed`
  (both new) and `sprint_added` / `sprint_removed` (new). History is replayed from them
  (`services/reports/history.py`).
- **`time_entries`**, now with a `billable` flag (default from the time-tracking setting
  `billable_default`, on unless switched off).
- **Sprints** (new, §5.1) and **daily snapshots** (new, §5.2).

History before a field had an event cannot be replayed: points history starts when
`points_changed` shipped (this release), status history when `issue_events` did
(2026-09-29). For earlier instants the replay uses the earliest known value, which is the
current one when the field never changed since.

## 4. Report catalogue

The **scope** is the workspace or one project (`space:{id}`), folder (`folder:{id}`), list
(`list:{id}`) or sprint (`sprint:{id}`). Filters combine with AND: people, assignees,
reporters, types, priorities, status categories, an epic (with everything under it),
specific issues, billable (time reports), and a date range or a preset (`today`,
`yesterday`, `this_week`, `last_week`, `this_month`, `last_month`, `last_30_days`,
`this_quarter`, `last_quarter`, `this_year`, or `custom` with `date_from`/`date_to`).
"This" periods run to their end, not to today. Weeks start on the time-tracking
`week_start`.

| Type | Report | Category | Chart |
|---|---|---|---|
| `timesheet` | People × days, with totals | time | stacked bar |
| `time_by_group` | Time grouped by one or two of person, project, list, epic, issue, type, date; the leaves drill down to entries | time | bar |
| `time_entries` | Every entry in the range | time | — |
| `estimate_vs_actual` | Per issue: estimate, spent (own and with sub-issues), remaining, over/under, % of estimate | time | bullet |
| `weekly` | One week: hours per person and project, completed, created, overdue, top time sinks | time | bars + KPIs |
| `project_overview` | Counts by status, category, type, priority, assignee; % done by count and points; overdue; logged against estimate | project | donut |
| `created_vs_resolved` | Created and resolved per day, week or month; cumulative option | project | line |
| `workload` | Per assignee: open issues, points, remaining estimate, hours logged in the range | project | bar |
| `epic_progress` | Per epic: issues, done, points, done points, estimate, spent | project | bar |
| `sprint_report` | Jira's sprint report (§5.5) | sprint | — |
| `burndown` | Remaining points, issues or hours per day with a guideline | sprint | line |
| `burnup` | Completed and total scope per day | sprint | line |
| `velocity` | Committed and completed for the last N closed sprints (default 7, at most 25) | sprint | grouped bar |
| `cfd` | Issues per status per day | flow | stacked area |
| `cycle_time` | Lead and cycle time per resolved issue: median, 85th percentile, rolling average | flow | scatter |

Sprint reports need a sprint: `scope=sprint:{id}`, `sprint_id`, or a project scope (the
active sprint, else the last closed one). At workspace scope they answer 422.

**R3** is built too: saved reports (private or workspace, pinnable to the Reports home),
scheduled emails (daily, weekly or monthly, at a local hour) and background export jobs.

## 5. Backend

### 5.1 Sprints

`sprints(id, workspace_id, space_id, name, goal, state planned|active|closed, starts_on,
ends_on, committed_points, completed_points, started_at, closed_at, …)`,
`sprint_issues(workspace_id, sprint_id, issue_id, points_at_add, added_at, removed_at)`
(one row per membership span, so an issue removed and added back has two) and
`issues.sprint_id` (the current sprint). Revision `20261005b001`.

- Sprints are **per project** (space). One active sprint per project: start takes the
  project's row lock (`lock_space`) and answers 409 with `active_sprint_id` when another is
  active. MySQL has no partial unique index; the lock is the portable rule.
- **Start** freezes `committed_points` (the members' points at that moment) and needs both
  dates. **Close** freezes `completed_points` (members done at the close) and moves the
  unfinished issues to the backlog or to a *planned* sprint of the same project.
- Adding and removing issues writes `sprint_issues` and a `sprint_added` / `sprint_removed`
  event in the same transaction. An issue is in at most one open sprint; adding it to
  another moves it.
- Start, close and delete are gated by the reports setting `sprint_manage_role` (default
  admin); planning (create, edit, add and remove issues) needs a member. Start, close and
  delete are written to the activity log.

### 5.2 Reporting tables

Revision `20261005c001`:

- `issue_daily_status(snapshot_date, issue_id, workspace_id, space_id, list_id, status_id,
  status_category, assignee_id, story_points, sprint_id)` plus `issue_snapshot_days`, a
  marker per written (workspace, day), which tells "no issues that day" apart from "never
  written". The worker writes yesterday for every workspace after midnight in the
  workspace's time zone; `scripts/backfill_snapshots.py --days N [--workspace W]` replays
  history for past days. Both are idempotent. Days without a snapshot are replayed from
  events at read time, so a missing night costs time, not correctness.
- `report_cache(key, workspace_id, report_type, payload, computed_at, expires_at)`.
- `saved_reports`, `report_schedules`, `report_jobs` (§7).
- `time_entries.billable` and the indexes `issues(workspace_id, space_id, status_id)`,
  `issue_events(workspace_id, event, created_at)`. The time-entry indexes the plan asked
  for already existed (doc 07 §7.3).

### 5.3 Engine

```text
services/reports/
  base.py            ReportContext, ReportError, the result shape (column, kpi, section, result)
  settings.py        the `reports` workspace settings (§6)
  scope.py           scope token -> tenant-checked issue / entry queries, filters
  history.py         replays issue_events: status, assignee, points, estimate at any instant
  common.py          shared lookups (names, keys, done statuses), range bounds, row limit
  time_reports.py    timesheet, time_by_group, time_entries, estimate_vs_actual, weekly
  project_reports.py project_overview, created_vs_resolved, workload, epic_progress
  sprint_reports.py  sprint_report, burndown, burnup, velocity
  flow_reports.py    cfd, cycle_time
  snapshot.py        writes issue_daily_status
  registry.py        REPORTS: type -> definition (title, category, params, chart, limits)
  cache.py           report_cache (§9)
  engine.py          build_context, compute, run (cache), catalog, can_view
  jobs.py            the report_jobs queue, signed downloads
  scheduler.py       due schedules -> jobs, DST-safe next run
  saved.py           saved reports and schedules
  exporters/         csv_export, xlsx_export, pdf_export + pdf_writer, formatting
utils/report_math.py pure arithmetic: presets, rounding, percentiles, guideline, buckets
workers/report_worker.py  claims jobs, emails, writes snapshots
```

Every report returns one shape: `meta` (type, title, scope and its name, range, time zone,
week start, generated at, rounding, `restricted_to_own`, notes, row count, cached),
`columns` (key, label, type: `text`, `number`, `duration`, `percent`, `date`, `datetime`,
`user`, `issue`, `boolean`), `rows`, `totals`, `chart` (kind plus labelled series), `kpis`
and `sections` (sub-tables such as the sprint report's four lists). Keys starting with `_`
are for the UI (ids for drill-down, `_depth` for trees) and are never exported.

Aggregation runs in SQL (`GROUP BY` with `SUM`/`COUNT`); replays and percentiles run in
Python over bounded row sets. Limits: 92 days for time reports, 366 for the others,
25 sprints for velocity, 10 000 rows on screen (more answers 413 with
`{"hint": "export"}`), 200 000 rows in an export.

### 5.4 API

All under `/api/v1/workspaces/{w}`, with the `SuccessResponse` envelope.

| Method and path | Role | Purpose |
|---|---|---|
| `GET /reports/catalog` | guest | Report types with their parameters and limits, the presets, the formats the caller may export, and the settings |
| `GET /reports/{type}?scope=&preset=&date_from=&date_to=&…` | guest* | Compute a report (cached for 60 s). List parameters repeat or are comma-separated. |
| `POST /reports/{type}/export` `{params, format, date_format, duration_format, background}` | export role** | Up to 5 000 rows: the file. More, or with `background`: 202 with a job. |
| `GET /report-jobs`, `GET /report-jobs/{id}` | requester (or a recipient of a scheduled job) | Your recent jobs; poll one |
| `GET /report-jobs/{id}/download?token=` | the same | The file, with the signed token from `download_path`. 409 until done, 410 after 24 h. |
| `GET, POST /saved-reports`, `GET, PATCH, DELETE /saved-reports/{id}` | guest (private only) / member | Save, share, pin. Changing someone else's needs an admin. |
| `GET, POST /saved-reports/{id}/schedules`, `PATCH, DELETE …/{sid}`, `POST …/{sid}/run` | schedule role** | Emailed reports; `run` sends now |
| `GET, PATCH /settings/reports` | guest / admin | §6 |
| `GET, POST /spaces/{s}/sprints`, `GET, PATCH, DELETE /sprints/{id}`, `POST /sprints/{id}/start`, `/close`, `GET, POST, DELETE /sprints/{id}/issues` | member; start, close, delete need `sprint_manage_role` | §5.1 |
| `GET /sprints/{id}/report`, `/sprints/{id}/burndown?unit=`, `/spaces/{s}/velocity`, `/spaces/{s}/cfd`, `/spaces/{s}/cycle-time` | guest | The aliases doc 04 §4.2 designed, on the same engine |

\* and the time-tracking visibility (§6). \*\* see §6.

### 5.5 Metric definitions

- **Done**: the status's category is `done`. **Resolved at**: the last `status_changed` into
  a done status; reopening clears it.
- **Time in a period**: `SUM(duration_seconds)` of live entries with `work_date` in the
  range, on live issues. Time belongs to the issue's *current* project (doc 07).
- **Rounding** (time reports): none, or 1, 5, 6, 10, 15 or 30 minutes, up, down or nearest;
  per entry, before summing. Every export footer states it.
- **Durations in exports**: decimal hours with 2 decimals (default), `h:mm`, ISO 8601
  (`PT3H20M`) or seconds. Dates: ISO (default), "normal" (`05 Oct 2026`), or epoch
milliseconds. The
  time zone is stated in `meta` and in every file.
- **Sprint report**: an issue is *completed* if it was done at the close (or now, for an
  active sprint); *removed* if its last membership ended before the close (in an active
  sprint: if it ended at all; a membership ending *at* the close is carry-over, not a
  removal); *added after start* (`*`) if a membership began more than 2 s after the start.
  Points changed during the sprint show as `a → b`.
- **Burndown**, remaining at the end of day D: the members at end(D) (`added_at ≤ end(D)`
  and not removed by then), not done at end(D), summing their points (or 1, or remaining
  estimate) at end(D) from the replay. The guideline falls from the starting total to 0 over
  the *working* days (`working_days`, default Mon–Fri) and is flat on the others. Scope
  changes are listed as steps. Future days are empty.
- **Velocity**: the frozen `committed_points` and `completed_points`. An issue counts only
  if done at the close; carried-over points do not count in the sprint they left.
- **Cumulative flow**: issues per status (or category) at the end of each day, from the
  snapshot when the day has one, otherwise from the replay.
- **Lead time**: created → first done. **Cycle time**: first entry into `in_progress` →
  final done, in days. Issues that skip `in_progress` count in lead time only, and the
  report says how many.

## 6. Permissions and privacy

| Act | guest | member | admin / owner |
|---|---|---|---|
| Run a report | if `guest_access` (default on) | yes | yes |
| Export a file | if `export_min_role` allows (default member) |||
| Save a report | private only | yes | yes |
| Change or delete a saved report | own | own | anyone's |
| Schedule an emailed report | if `schedule_min_role` allows (default admin) |||
| Start, close, delete a sprint | if `sprint_manage_role` allows (default admin) |||
| Change the report settings | — | — | yes |

- Every query is tenant-filtered and soft-delete aware; another workspace's ids answer 404.
- **Time visibility**: under the time-tracking setting `visibility=own`, a member's time
  reports hold only their own time, in every report, export, schedule and cached result
  (the cache key includes the viewer). `meta.restricted_to_own` says so.
- Exports, schedule changes and sprint start/close/delete are written to `activity_logs`.
- Schedules send only to **workspace members**; under `own` visibility each recipient gets
  a rendering computed as themselves.
- Download links carry an HMAC token (24 h); the job must also be readable by the caller.
  Files are deleted after 24 h, job rows after 7 days.
- Settings, `PATCH /settings/reports`: `timezone` (IANA, default UTC), `working_days`
  (0 = Monday), `guest_access`, `export_min_role`, `schedule_min_role`,
  `sprint_manage_role`.

## 7. Exports and scheduling

| Format | How |
|---|---|
| CSV | `csv` module, UTF-8 with a BOM. Cells starting with `= + - @` (or tab/CR) are prefixed with `'` against formula injection. A footer states report, scope, range, time zone, rounding and duration format. |
| XLSX | `openpyxl` (MIT, pure Python). A "Summary" sheet (meta and KPIs), the main table, one sheet per section. Durations are numbers with a number format, so they sum in Excel. |
| PDF | `services/reports/exporters/pdf_writer.py`, written in this repository: A4 landscape, the standard Helvetica font, KPI tiles, a vector chart, the tables with repeated headers. |

**Deviation from the plan, on purpose**: the plan named WeasyPrint. It needs Pango and about
60 MB in the image; `fpdf2` is LGPL-3.0; ReportLab pulls in Pillow. A report PDF is text,
lines and rectangles, which PDF can express directly, so it is written here with no
dependency (`pdf_writer.py`, ~170 lines, plus the layout in `pdf_export.py`). Its limit: the
standard fonts cover Latin-1, so other characters are replaced with `?`. The browser's print (§8.4) remains the high-fidelity path for other scripts.

- **Worker** (`workers/report_worker.py`, the `report-worker` compose service): claims
  `report_jobs` with `SKIP LOCKED` and a 10-minute lease; retries after 30 s, 2 min and
  5 min, then fails. Each minute it also enqueues due schedules, deletes expired files and
  cache rows, and writes yesterday's snapshots.
- **Scheduler**: `next_run_at` is computed in the schedule's time zone (local date and hour,
  then converted), so 09:00 stays 09:00 across a daylight-saving change.
- **Email**: subject `[PMT] {saved report name} - {scope} - {range}`. A file up to 5 MB is
  attached; bigger files get a link to `/reports/jobs/{id}` in the app.

## 8. Frontend

### 8.1 Structure

```text
src/api/reports.ts, src/api/sprints.ts     API helpers; waitForReportJobApi polls a job
src/api/client.ts                          `file: true` mode: a non-JSON answer comes back
                                           as {blob, fileName, rows} (Content-Disposition,
                                           X-Report-Rows); DELETE may carry a body
src/types/reports.ts, src/types/sprints.ts types matching the result shape
src/features/reports/
  routes.ts               the one lazy chunk: ReportsHome, ReportPage, SavedReportRoute,
                          ReportJobPage
  params.ts               URL <-> parameters, pickParams, presetRange (mirrors the API's)
  format.ts               values by column type; axis and tooltip formats
  renderers.ts            per-type layout: chart beside or above, sections first, titles
  useReport.ts            fetch with a 300 ms debounce and an AbortController; keeps the
                          last result on screen while the next one loads
  useReportsWorkspace.ts  workspaces, catalogue, tree and members for the screen
  useKeyed.ts             one fetch per key, as on the Timesheet
  components/
    ReportsHome.tsx       pinned previews (3 KPIs each), catalogue by category, saved
                          reports (pin, delete), recent exports, admin settings
    ReportPage.tsx        scope, filters, meta, notes, KPIs, chart, table, sections;
                          refresh, print, save/update, schedule, export
    ReportRoutes.tsx      /reports/saved/:id (redirects to the report with its
                          parameters) and /reports/jobs/:id (polls, then downloads)
    ScopePicker, FilterBar, ReportTable, ExportDialog, SaveReportDialog, ScheduleDialog,
    ReportsSettingsDialog
    charts/               scale.ts; BarChart (bar, stacked, grouped, horizontal, bullet),
                          LineChart (line, step, stacked area), ScatterChart, DonutChart,
                          KpiTile; ReportChart dispatches by `kind`
src/features/sprints/components/SprintPanel.tsx   plan, fill, start and close sprints
```

All parameters live in the query string, so a report is a URL: shareable, bookmarkable,
and saving a report saves its parameters (only the ones that report reads). `?workspace=`,
`?saved=` and `?view=` are the page's own keys. Routes: `/reports`, `/reports/:type`,
`/reports/saved/:id`, `/reports/jobs/:id`, all lazy. The nav has "Reports" after Timesheet.
Entry points: the project header ("Sprints" opens the sprint panel, "Reports" opens the
home scoped to the project), the Timesheet ("Reports" opens Time by group for the week
shown) and the sprint panel (sprint report, burndown, velocity). A table group's `_drill`
links to the `time_entries` report behind its number.

### 8.2 Data fetching

Plain helpers in `src/api` (no React Query). Filter changes are debounced by 300 ms and the
request in flight is cancelled. A 413 shows the limit and an "Export instead" button; a 422
(no sprint, a planned sprint) says what to choose instead. An export answers with the file,
or with a job the dialog polls every 1.5 s and then downloads; closing the dialog stops the
polling, not the job, which Recent exports keeps listing.

### 8.3 Charts

Hand-written SVG (bar, stacked bar, grouped bar, line, step, stacked area, scatter, donut,
bullet) on one `scale.ts`, because a chart library would cost 100 kB or more of the bundle
budget. Each chart has `role="img"` and a summary label, SVG `<title>`s as tooltips, a
legend, and a "View as table" toggle showing the same data. Colours are theme variables
(`var(--chart-N)` and the API's tokens), so every theme and dark mode apply; non-working
days are shaded on line charts.

### 8.4 Print

`@media print` (end of `src/index.css`) hides the sidebar, the header and anything marked
`data-print-hide` (filters, actions), shows `.print-only` (the workspace name), unclips the
layout and keeps charts and tables from splitting across pages. The browser's "Save as PDF"
is the other PDF path.

## 9. Performance

- One grouped query per time report; history replays read events in chunks for the issues
  in scope only. `TestQueryCounts` pins that no report's query count grows with issues or
  entries.
- **Cache**: the key is a SHA-256 of workspace, type, normalised parameters, the viewer and
  a data version (the latest event, time entry, sprint change, membership and settings
  change). Any change moves the version, so a stale result is never served; the TTL (60 s,
  24 h for a closed sprint) only bounds how long an unchanged one is kept.
- Load test: `loadtest/reports.js` (k6) targets p95 < 800 ms on a seeded workspace with
  ~10k issues and 50k entries.

## 10. Tests

- `tests/unit/test_report_math.py`: presets for each week start, rounding, percentiles,
  guideline over non-working days, buckets, formats, DST-safe schedule times.
- `tests/integration/test_reports.py`: every report type with hand-computed answers,
  visibility, guests, cross-tenant 404, limits, exports (CSV guard, XLSX, PDF), jobs,
  signed downloads, the worker, retries, cleanup, snapshots, saved reports, schedules,
  settings, query counts.
- `tests/integration/test_sprints.py`: the sprint lifecycle and its rules.
- The SPA: Vitest for `scale.ts`, params and presets per week start, `FilterBar`,
  `ReportTable` grouping and totals, every chart kind, the export dialog (file and job),
  the report page (URL, 413, 422, save), the home, schedules, settings, the saved and job
  routes, and the sprint panel. `e2e/reports.spec.ts` (mocked): navigation, filters in the
  URL across a reload, an export download, axe on the loaded report, the home and the export
  dialog, theme colours on the chart, and colour contrast of the report under every theme.

## 11. Delivery

R0–R3 and the hardening items were delivered together on 2026-10-05.

## 12. Decisions on the plan's open questions

1. **Sprint semantics**: per project (space), as `schema.sql` designs them.
2. **Burndown unit**: points (default), issue count or remaining hours, chosen per report.
3. **Billable time**: added now. `time_entries.billable`, default from the time-tracking
   setting `billable_default` (on); a team that tracks internal effort switches it off once.
4. **Server PDF**: built, without WeasyPrint (§7).
5. **Time zone**: `settings.reports.timezone`, default UTC; snapshots, day boundaries and
   schedules use it.
6. **History before events**: the replay uses the earliest known value (§3); the backfill
   script writes snapshots only as far back as the events reach.
7. **Guest access**: on by default, read-only, switchable.
8. **PostgreSQL**: the report SQL is portable; `SKIP LOCKED` exists on both.

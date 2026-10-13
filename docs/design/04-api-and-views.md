# 4. API design and the multi-view strategy

## 4.1 Style: REST plus a WebSocket channel (GraphQL was considered and deferred)

The API is resource-oriented REST over JSON. Every response uses the base project's envelope
(`{status, status_code, message, data}`). The contract is pinned by
`docs/openapi-digest.txt`, which a test compares against the running app. Realtime updates go
out over one WebSocket per client.

**Why not GraphQL now:**

- **Views don't need arbitrary graph shapes.** Every view asks for the same card projection
  (§4.3), grouped differently, so the flexibility GraphQL adds is not needed.
- **Authorisation.** REST keeps it per route: one tenancy gate and one role check. GraphQL
  moves it to per-field resolvers, which is the easiest place in a multi-tenant system to
  leak data.
- **Performance.** HTTP caching (ETags, CDN) and per-route rate limiting work as-is with
  REST. Query-cost analysis and N+1 protection in GraphQL would be extra work.

**When to revisit:** a public integration API (Phase 3) is where GraphQL's client-chosen
shapes pay off. It can sit alongside REST over the same services.

**Conventions:**
- **Tenancy in the URL.** Everything tenant-scoped is under `/workspaces/{workspace_id}/…`, so
  the tenancy gate is a single dependency.
- **Errors.** A non-member gets a 404, identical to "does not exist". A member with an
  insufficient role gets a 403. A stale `version` gets a 409. A broken business rule
  (for example, a parent of the wrong type) gets a 422.
- **Creates.** HTTP 201, or 202 when the work is queued. From Phase 2, POSTs accept an
  `Idempotency-Key` header (stored for 24 h per workspace) so mobile and flaky clients can
  retry safely.
- **Partial updates.** `PATCH` with a required `version`. Omitting a field leaves it
  unchanged; sending `null` clears it.
- **Pagination.** Page and limit today. Keyset cursors on `(sort key, id)` once lists grow,
  which gives stable pages under concurrent writes.

## 4.2 Endpoints

### Phase 1 (implemented; `docs/openapi-digest.txt`)

| Method & path | Role | What it does |
|---|---|---|
| `POST /workspaces` | signed in | Create a workspace; the caller becomes owner |
| `GET /workspaces` | signed in | My workspaces, with my role in each |
| `GET /workspaces/{w}` | guest | The whole hierarchy: spaces with their statuses, folders and lists |
| `PATCH /workspaces/{w}` | admin | Rename the workspace or change its slug. A deleted workspace's slug stays taken (2026-10-06) |
| `DELETE /workspaces/{w}` | owner | **Soft delete** (2026-10-06): gone from every list, and every route under it answers 404; its issues, time and history stay in the database. Refused while a timer runs on one of its issues or a migration plan is running |
| `GET, POST /workspaces/{w}/members` | guest / admin | List and add members |
| `PATCH /workspaces/{w}/members/{u}` | admin | Change a member's role (2026-10-06). Only an owner makes owners or changes an owner's role; the last owner cannot be demoted |
| `DELETE /workspaces/{w}/members/{u}` | any member for themselves / admin | Leave, or remove someone (2026-10-06). Only an owner removes an owner, and the last owner stays. Refused while they own a space that is not archived, have a timer running here, or have report schedules that need them; their work stays, assigned issues included |
| `POST /workspaces/{w}/spaces` | admin | Create a space with the default four-column workflow |
| `POST /workspaces/{w}/spaces/{s}/folders` | admin | Create a folder |
| `POST /workspaces/{w}/lists` | admin | Create a list in a space or one of its folders |
| `GET, POST /workspaces/{w}/repositories` | guest / admin | Link GitHub repositories |
| `POST /workspaces/{w}/issues` | member | Create an issue (hierarchy rules enforced; added at the bottom of its column) |
| `GET /workspaces/{w}/issues` | guest | **List view.** Filters: `list_id`, `space_id`, `status_id`, `assignee_id` (or `me`), `type`, `q`. Sort and page. |
| `GET /workspaces/{w}/issues/{i}` | guest | Detail: the card plus description, reporter, children and PR links |
| `PATCH /workspaces/{w}/issues/{i}` | member | Partial update with an optimistic `version` |
| `POST /workspaces/{w}/issues/{i}/move` | member | **Board move:** a column plus the card it now sits under or above |
| `DELETE /workspaces/{w}/issues/{i}` | member | Soft delete; refused while the issue has children |
| `GET, POST /workspaces/{w}/issues/{i}/comments` | guest / member | Comments |
| `GET /workspaces/{w}/board?list_id=…` or `?space_id=…` | guest | **Board view:** every column of the workflow, with cards in rank order |
| `POST /workspaces/{w}/issues/{i}/summary` | member | Queue an AI thread summary (returns 202 with a job) |
| `GET /workspaces/{w}/ai-jobs/{j}` | guest | Poll an AI job |
| `POST /integrations/github/webhook` | HMAC | Link PRs to issues; move an issue to Done when a linked PR merges |

### Phase 2 and later (designed)

| Area | Endpoints |
|---|---|
| Views | `GET/POST/PATCH /workspaces/{w}/views`, and `POST /workspaces/{w}/views/query` (§4.3) |
| Search | `GET /workspaces/{w}/search?pql=…&cursor=…`, `POST /workspaces/{w}/pql/validate` (parse, type-check, autocomplete) |
| Sprints | **Built** (doc 08 §5.1): `…/spaces/{s}/sprints` (CRUD), `…/sprints/{id}/start`, `…/close` (carry-over options), `…/sprints/{id}/issues` (plan and groom) |
| Reports | **Built** (doc 08 §5.4): `…/reports/catalog`, `…/reports/{type}` and `/export`, `…/report-jobs`, `…/saved-reports` and their schedules, `…/settings/reports`; and the aliases `…/sprints/{id}/report`, `…/sprints/{id}/burndown`, `…/spaces/{s}/velocity?sprint_count=6`, `…/spaces/{s}/cfd?date_from=&date_to=`, `…/spaces/{s}/cycle-time` |
| Releases | `…/spaces/{s}/releases` (CRUD), `…/releases/{id}/release`, `…/releases/{id}/notes` |
| Custom fields | `…/custom-fields` (CRUD). Values go through `PATCH …/issues/{i}` using `custom_fields: {key: value}`. |
| Links, labels, watchers | `…/issues/{i}/links`, `…/labels`, `…/issues/{i}/watchers` |
| Time | `…/issues/{i}/time-entries`, `…/workload?sprint_id=…` (capacity against assigned work, per person) |
| Automation | `…/automations` (CRUD), `…/automations/{id}/runs`, `…/automations/{id}/test` (dry run against an event) |
| Docs | `…/documents` (the wiki tree). Content syncs over the realtime gateway (Yjs), not REST. |
| Notifications | `GET /me/notifications`, `POST /me/notifications/read` |
| AI | `…/issues/{i}/estimate`, `…/issues/{i}/assignee-suggestions`, `…/ticket-drafts` (from a transcript), `…/ai-suggestions/{id}/decision` |
| VCS / CI | `…/integrations/github/install` (GitHub App flow), `…/issues/{i}/branch` (create a branch named after the issue key), CI runs shown on the issue from `ci_pipeline_runs` |
| Realtime | `POST /realtime/ticket` (a short-lived WebSocket ticket), then `wss://…/ws?ticket=…` |

## 4.3 The multi-view engine: data is separate from how it is shown

**Rule: one projection of an issue, many groupings of it.** Board, list, calendar, Gantt and
timeline all draw the same `IssueCard` (`schemas/responses/issues.py`): key, title, type,
status, list, parent, priority, points, assignee, rank, start and due dates, PR state and
version. They differ only in:

| View | Groups by | Orders by | Uses |
|---|---|---|---|
| Board | status (or any field: assignee, priority, `cf.*`) with optional swimlanes | `rank` | move, WIP limits |
| List | nothing, or any field | any sortable field | inline edit, bulk actions |
| Calendar | `due_date` bucketed by day or week | priority | drag to reschedule (a PATCH of `due_date`) |
| Gantt | parent (epic → children) | `start_date` | `issue_links(kind='blocks')` for dependencies and the critical path |
| Timeline | team or assignee | `start_date` | capacity overlay from `workload` |

**Server side.** A *view definition* is data (`saved_views`): `{view_type, scope, pql, config}`,
where config holds grouping, sort, visible fields, swimlanes and date fields.
`POST /views/query` takes a definition, or the ID of a saved one, and returns a
**normalised** payload:

```json
{
  "issues": { "<id>": { …IssueCard… } },
  "groups": [ { "key": "<status id | user id | date>", "issue_ids": ["…"], "cursor": "…" } ],
  "fields": { "cf.severity": { "type": "select", "options": […] } },
  "generation": 4812
}
```

Phase 1's `GET /board` and `GET /issues` are two fixed instances of this: status grouping
with rank order, and ungrouped with a sort.

**Client side.** The SPA keeps one normalised store of cards keyed by issue ID. Redux Toolkit
is already in the SPA, so RTK Query's entity adapter can serve as that store with no new
dependency.

- Switching from Board to List to Calendar over the same filter **regroups data already in
  the store** instead of fetching again. The Phase 1 SPA already does this: its List view is
  built from the loaded board data (see `pmt-app/src/features/projects`).
- A different filter or scope fetches with the same query machinery.
- Realtime events (`issue.updated`, `issue.moved`) patch the store by ID, so every open view,
  the detail drawer and any list update together.
- Large groups load per group with their own cursor. The board virtualises columns past a few
  hundred cards.

**Caching (Phase 2).**
- A board or query result is cached in Redis under
  `view:{workspace}:{hash(definition)}:{generation}`.
- `generation` is a per-scope counter, bumped by the outbox relay whenever an issue in that
  scope changes. Invalidation is therefore one `INCR`, and stale entries expire by TTL instead
  of being hunted down.
- The API sends the generation as an `ETag`, so a client can revalidate with `If-None-Match`
  and get a 304 at almost no cost.

## 4.4 The query language: PQL (JQL-style)

**Grammar (EBNF, abridged):**

```
query      := [ expr ] [ "ORDER BY" order ("," order)* ]
expr       := term ( ("AND" | "OR") term )*
term       := "NOT" term | "(" expr ")" | clause
clause     := field op value
            | field ("IN" | "NOT IN") "(" value ("," value)* ")"
            | field "IS" ["NOT"] "EMPTY"
            | field ("WAS" | "CHANGED") [ "FROM" value ] [ "TO" value ] [ "DURING" "(" value "," value ")" ]
            | "text" "~" string
op         := "=" | "!=" | ">" | ">=" | "<" | "<=" | "~"
field      := standard_field | "cf." key | "pr.state" | "ci.status"
value      := string | number | date | relative_date | function
function   := "currentUser()" | "openSprints()" | "startOfWeek(" [offset] ")" | "membersOf(" string ")"
relative_date := ("-" | "+") number ("d" | "w" | "m")
order      := field ["ASC" | "DESC"]
```

**Examples:**

```
assignee = currentUser() AND status.category != done ORDER BY priority DESC, rank
type = bug AND cf.severity IN ("S1", "S2") AND created >= -14d
sprint IN openSprints() AND "story points" IS EMPTY
pr.state = merged AND status.category != done          -- merged work still open: a process smell
status CHANGED TO "In Review" DURING (startOfWeek(-1), startOfWeek())
text ~ "refund timeout" AND space = PAY
```

**Pipeline:**

1. **Lex and parse.** A hand-written recursive-descent parser (or Lark) turns the query into an
   AST. Errors carry positions so the editor can underline the problem.
2. **Resolve and type-check** against the workspace's *field registry*: standard fields plus
   `custom_field_definitions`. Unknown fields, type mismatches (`due > "abc"`) and bad
   operators are rejected with suggestions. The same pass powers autocomplete.
3. **Compile to SQL** using SQLAlchemy Core expressions, never string concatenation. Values are
   always bound parameters and field names come from the registry, so injection is impossible
   by construction.
   - Standard fields map to columns.
   - `cf.*` maps to JSONB operators, using the hot-field expression index when one exists.
   - `pr.*` and `ci.*` map to `EXISTS` subqueries on `vcs_links` and `ci_pipeline_runs`.
   - `WAS` and `CHANGED` map to `EXISTS` over `issue_events`.
   - `text ~` maps to the `search` tsvector, or to OpenSearch once it exists.
   - RLS and the `workspace_id` predicate are always added. The user's query can only narrow
     the result.
4. **Guard the cost.** A statement timeout (2 s), a maximum query depth, and an `EXPLAIN` cost
   check for saved views that run on a schedule.

## 4.5 The automation engine ("if this then that")

- **Model.** A rule is `trigger` + `conditions` + `actions`, stored as JSON in
  `automation_rules`. Example rule:

```json
{
  "name": "Merged PR closes the ticket and logs time",
  "trigger":    { "event": "vcs.pull_request.merged" },
  "conditions": [ { "pql": "status.category != done" } ],
  "actions":    [ { "type": "set_status_category", "category": "done" },
                  { "type": "log_time", "minutes_from": "pull_request.open_duration", "cap": 480 },
                  { "type": "comment", "body": "Merged in {{pull_request.url}}" } ]
}
```

- **Triggers.** Domain events from the outbox: issue created, updated, moved or commented;
  sprint started or closed; PR opened, merged or closed; CI failed or succeeded; a scheduled
  cron; a due date approaching.
- **Conditions.** PQL evaluated against the subject issue, so the query language gets reused.
- **Actions.** Set a field or status, assign (optionally through the AI suggester), comment,
  log time, create a sub-task, notify, call an outbound webhook (signed), or queue an AI job.
- **Safety.**
  - Idempotent: one run per `(rule_id, event_id)`, enforced by a unique key.
  - Loop protection: events caused by automation carry `depth`, and runs stop at depth 3.
  - A rule never re-triggers on its own actions.
  - Every run is recorded in `automation_runs`, with a per-rule run history and a dry-run
    endpoint.
- **Phase 1** ships exactly one built-in rule, hard-wired and tested: *a merged linked PR moves
  the issue to the first `done` column* (`controllers/github_controller.py`). The engine
  replaces this code with the same rule expressed as data.

## 4.6 Realtime, collaboration and notifications

- **Gateway.** A Node service using `ws` and Hocuspocus.
  - **Connecting.** Clients connect with a single-use ticket (valid for 30 s) from
    `POST /realtime/ticket`. JWTs are never put in a URL.
  - **Channels.** `workspace:{w}`, `board:{scope}` and `issue:{i}`. The gateway checks
    membership once, when a client subscribes.
  - **Fan-out.** Uses Redis pub/sub, fed by the outbox relay, so any gateway pod can serve any
    client.
- **Multiplayer editing.** Issue descriptions and wiki pages are Yjs documents edited in TipTap.
  - Hocuspocus merges concurrent edits (CRDT) and broadcasts awareness: cursors and who is
    viewing.
  - It persists `ydoc_state` to `documents` in batches of a few seconds, writing a plain-text
    snapshot (`content_text`, and `issues.description` for issue descriptions) for search and
    AI to read.
  - Documents are routed to a pod by consistent hashing of the document ID, so each live
    document has one authoritative in-memory copy.
- **@mentions and notifications.**
  1. When a comment or document is saved, mentions are parsed into `notifications` rows.
  2. The gateway pushes each new row to the recipient's `user:{u}` channel.
  3. Email and chat digests are batched by a scheduler and respect each user's preferences.

## 4.7 Rate limits and protection

- **Already in the base:** a per-address limiter that refuses requests before authentication
  (`core/ratelimit.py`), a body-size limit, and security headers.
- **Phase 2:** a per-workspace token bucket in Redis (distributed, unlike today's per-process
  limiter), per-route weights (a PQL search costs more than a card read), and AI budgets
  enforced in the LLM gateway.

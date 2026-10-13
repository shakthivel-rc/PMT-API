# 9. Restructuring: delivery boundaries, custom workflows and controlled migration

Built from `../pmt-restructuring-plan.md` (prepared 2026-10-05). That plan kept PMT's
hybrid foundation — ClickUp-style organisation, Jira-style work items, boards that display
work rather than own it — and listed what has to exist before work can be moved between
containers safely, and what the full target experience needs. This document is the design
of what was built for it. Sections are numbered so code comments can cite them
(`09-restructuring.md §4.3`), and a section is renamed only together with its citations.

The four concepts the plan separates, and where each lives:

| Concept | Question | Record |
|---|---|---|
| Where work belongs | Which container is its canonical home? | Workspace › Space › optional Folder › List (`lists.id` on the issue) |
| What work delivers | Which outcome does it serve? | Epic › Story / Task / Bug › Subtask (`issues.parent_id`, `issues.type`) |
| When work is delivered | Which timebox? | Sprint membership (`sprint_issues`), independent of the List |
| How work is viewed | Which picture of it? | Board, list, active-sprint board, backlog, reports — projections of the same `IssueCard` |

## 1. Plan coverage

| Plan item | What it became | § |
|---|---|---|
| Step 2 §2 rules 3–5: peer Story/Task/Bug, Subtasks for detail, legacy kept until migrated | Standard parent rules for every relationship created or changed; legacy relationships stay valid until something changes them; the inventory lists them | 2 |
| §2 table: Epic needs an owner and completion criteria | `issues.completion_criteria`; enforced on create / re-type (governance setting, default on) | 2.3 |
| §3 Project creation standard | Space charter (intake checklist), owner, delivery method, template, initial lists; optional enforcement | 3 |
| §4 Default workflow, user-customisable stages and flow | Stage editor model: names, colours, reporting category, WIP flag and limit, initial stage, free or restricted transitions, templates, preview, publish with replacement of populated stages, per-space workflow editors | 4 |
| §4 "Keep reporting meaningful … distinguish canceled work" | A fourth reporting category, `canceled`, and every report taught it | 4.2, 9 |
| §4 Definition of Done and GitHub | Per-space merge action: move to a chosen stage, or only link; explicit failures, never a fallback | 6 |
| §4 Required views | Space overview board, active-sprint board, space-wide ordered backlog, saved board filters, WIP indicators, blocked flag, stage age | 5 |
| §5 Controlled transfer/reorganisation tools | List/folder rename and reparent; same-space and cross-space issue transfer with dry-run | 7 |
| §5 Cross-space transfer, old-key aliases | `issue_key_aliases`, `space_key_aliases`; every resolver (webhook, deep link, search) goes through one function | 7.3 |
| §5 Migration audit and recovery | Migration plans: register, dry-run, approval, freeze, batched idempotent execution, reconciliation gates, rollback | 8 |
| §5 Completion automation alignment | §6 | 6 |
| §5 Archive / read-only | Archive spaces, folders and lists; freeze spaces during a migration; one write guard | 3.4 |
| §5 Typed dependencies, release tracking, project metadata | `issue_links`, `releases` + `issue_fix_versions`, the charter | 10, 3 |
| §5 Finer-grained access only where required | Per-space workflow editors — the one grant the plan requires | 4.6 |
| §6 Phase 1 inventory and baseline | `GET …/inventory` (+ CSV), migration baselines | 8.1, 8.2 |
| §6 Phase 5 pause writes and integrations, replay deliveries | Space freeze; GitHub deliveries for frozen spaces are deferred and replayed on unfreeze | 3.4, 6.3 |
| §7 Hard migration gates | `GET …/migrations/{id}/reconcile` evaluates every gate | 8.6 |
| §6 Phase 6 publish the conventions | `docs/guides/structure-and-boards.md`; the runbook is `docs/runbooks/restructuring-migration.md` | — |

What is deliberately **not** built: cross-*workspace* moves (a tenancy boundary is not a
container, and moving data across it is a disclosure risk with no requirement behind it),
custom fields, expedite fields (the Kanban policy allows one expedited item "with explicit
approval", which is a convention until a field is justified), and automatic database backups
(an operator task with a runbook, §8.8).

## 2. Issue hierarchy

### 2.1 The standard convention

Story, Task and Bug are **peer delivery types**; Subtasks are execution detail beneath any of
them; Epics are outcomes and sit at the top. Every standard item may also stand alone.

| Type | May sit under (standard) | Parent required |
|---|---|---|
| epic | — (top level only) | no |
| story | epic | no |
| task | epic | no |
| bug | epic | no |
| subtask | story, task, bug | **yes** |

Depth is therefore at most three: epic › story/task/bug › subtask.

### 2.2 Legacy relationships

Before 2026-10-05 the rules also allowed task under story, subtask under task-under-story,
and a bug under anything (`LEGACY_PARENT_TYPES` in `models/issue.py`):

| Type | Also accepted when it already exists |
|---|---|
| task | story |
| bug | story, task, subtask |

**The rule: a relationship you create or change follows the standard; one you leave alone is
kept.** Concretely:

- Creating an issue with a parent, or changing `parent_id`, checks the standard rules.
- Changing `type` checks the issue's own relationship to its parent *and* every child's
  relationship to it, against the standard — re-typing changes those relationships.
- An update that touches neither leaves an existing legacy relationship alone, however it
  was made. Nothing is flattened or re-typed automatically: the plan says to review what a
  Story › Task relationship means before converting it, and the inventory (§8.1) lists them.
- Transfers (§7) move subtrees intact, legacy relationships included; moving is not changing.

### 2.3 Epics need an owner and completion criteria

`issues.completion_criteria` (text) is the epic's Definition of Done. With the governance
setting `require_epic_owner_and_criteria` on (the default):

- creating an `epic`, or re-typing an issue to `epic`, needs an `assignee_id` (the owner) and
  non-blank `completion_criteria` — 422 otherwise;
- unassigning an epic, or blanking its criteria, is refused;
- other edits to an existing epic that lacks either are allowed, so legacy epics stay
  editable; the inventory counts them.

### 2.4 Structural events

Every structural change appends to `issue_events` in the same transaction (02-data-model.md
§2.2 rule 3): `parent_changed` (from/to parent id), `type_changed` (from/to type),
`list_changed`, `space_changed`, `key_changed` (§7), alongside the existing `updated`.

## 3. Containers and governance

### 3.1 Meanings

| Level | Purpose | Creation rule |
|---|---|---|
| Workspace | Organisation/tenant and membership boundary | One internal workspace by default; a separate one where access must be isolated |
| Space | Stable product or delivery unit sharing workflow and sprint cadence | Only for an independent backlog/cadence or a materially different workflow |
| Folder | Optional grouping of related lists | Only when several durable workstreams need grouping |
| List | Canonical home of a coherent stream of work | Start with one delivery list |

One active sprint per space is unchanged (`services/sprint.start_sprint`), so independently
managed sprint teams need separate spaces; several lists do not create several cadences.

### 3.2 The space charter (the creation checklist)

`spaces` gains `description`, `owner_id` (the accountable owner, a member), `delivery_method`
(`scrum` | `kanban`) and `charter` (JSON, validated by `schemas/workspaces.SpaceCharter` on
every write and every read, the way `workspaces.settings` documents are):

| Charter field | Plan §3 item |
|---|---|
| `objective`, `success_measure` | Business objective and success measure |
| `delivery_team` | Accountable owner (`owner_id`) and delivery team |
| `scope`, `exclusions` | Scope and exclusions |
| `access_requirements` | Access requirements |
| `reuse_justification` | Existing space to reuse, or documented reason for a new one |
| `canonical_lists` | Canonical list(s) — free text; the lists themselves are `initial_lists` |
| `epic_completion_criteria`, `initial_backlog` | Epic completion criteria and initial backlog |
| `reporting_cadence`, `retention_policy` | Reporting cadence and archival/retention policy |
| `notes` | Anything else |

Every field is optional text of at most 2,000 characters — the checklist is guidance first.
With the governance setting `require_space_charter` on, creating a space needs `owner_id`,
`delivery_method`, `objective`, `success_measure` and `reuse_justification`; the 422 names
every missing item at once.

`POST /workspaces/{w}/spaces` also takes `workflow_template` (a built-in key or a workspace
template id, §4.5; default: the governance `default_workflow_template`, itself `default`) and
`initial_lists` (up to 10 names, created in order). Both are optional, so the old two-field
body still works and still creates the four-stage workflow.

### 3.3 Governance settings

`workspaces.settings["governance"]`, read and written through
`schemas/workspaces.GovernanceSettings` like the time-tracking and reports documents:

| Field | Default | Meaning |
|---|---|---|
| `require_space_charter` | `false` | §3.2 enforcement |
| `require_epic_owner_and_criteria` | `true` | §2.3 enforcement — the plan states it as a creation rule |
| `default_workflow_template` | `"default"` | Template for new spaces |
| `inactive_after_days` | `90` | When the inventory calls a container inactive (7–3650) |

`GET /workspaces/{w}/settings/governance` (guest), `PATCH` (admin).

### 3.4 Write states: archived and frozen

A container is **writable**, **archived** (permanently read-only, history kept, reports still
work) or — spaces only — **frozen** (temporarily read-only while a migration wave runs).

| Column | On |
|---|---|
| `archived_at`, `archived_by` | spaces, folders, lists |
| `frozen_at`, `frozen_by`, `freeze_reason`, `frozen_by_plan_id` | spaces |

**One guard decides**: `services/write_guard.py`. Every write to an issue, its comments, its
time, its sprint membership, its links and fix versions, and every workflow publish, asks it
first; the answer is **423 Locked** with a message that says which container, why, and what
to do. Order of checks: list archived, folder archived, space archived, space frozen. Reads
are never affected. The migration runner (§8) is the one writer allowed into a frozen space,
and only one frozen by its own plan.

| Endpoint | Role | Refused when |
|---|---|---|
| `POST …/spaces/{s}/archive`, `/unarchive` | admin | an active sprint (close it first); frozen; a running timer on its issues |
| `POST …/folders/{f}/archive`, `/unarchive` | admin | a running timer on its issues |
| `POST …/lists/{l}/archive`, `/unarchive` | admin | a running timer on its issues |
| `POST …/spaces/{s}/freeze` | admin | archived |
| `POST …/spaces/{s}/unfreeze` | admin | frozen by a migration plan that is `running` or `failed` (§8.5) |

Unfreezing replays the workspace's deferred GitHub deliveries (§6.3) and says how many.

Archived containers stay in the workspace tree, flagged, so the SPA can show them in an
"Archived" section; their issues stay in reports.

### 3.5 Renaming and reparenting containers

| Endpoint | Role | Body |
|---|---|---|
| `GET …/spaces/{s}` | guest | — : the space with its charter and key aliases (`SpaceDetail`) |
| `PATCH …/spaces/{s}` | admin | `name`, `key`, `description`, `owner_id`, `delivery_method`, `charter` |
| `PATCH …/folders/{f}` | admin | `name`, `position` |
| `PATCH …/lists/{l}` | admin | `name`, `folder_id` (a folder **of the same space**, or null for the space root), `position` |

**A space key change keeps the old key resolving** (plan Step 2 §2 rule 1 says not to rename
prefixes casually; when it is done anyway, nothing that quotes the old key breaks). The old
key is stored in `space_key_aliases` and stays reserved, so no other space can take it and make
`PAY-7` ambiguous. Changing a key back to one of the space's own aliases removes that alias.

Moving a list to another space is not a list operation: its issues change space and keys, so it
is a transfer (§7) or a migration (§8).

## 4. Workflows

### 4.1 Stages

`workflow_statuses` keeps its stable `id` — names and positions change, the id does not, so
issues, events and reports keep pointing at the same stage. New columns:

| Column | Meaning |
|---|---|
| `color` | One of `neutral`, `info`, `primary`, `success`, `warning`, `danger` — the SPA's theme-safe tones, never a hex value. Always shown with the name (no colour-only signal) |
| `counts_toward_wip` | Whether issues here count as work in progress |
| `wip_limit` | 1–999, or null for none |
| `deleted`, `deleted_at` | A removed stage is soft-deleted: history and reports still resolve its id and category |
| `updated_at` | — |

At most **20 stages** per space. A column is 288 px wide; twenty is already a 5,760 px board,
and past that the board stops being something a person can scan.

### 4.2 Reporting categories

Every stage maps to one of four categories. **Reports, automations and sprint maths reason
about categories, never about names or positions.**

| Category | Label | Open/closed | Counts as delivered |
|---|---|---|---|
| `todo` | Not started | open | — |
| `in_progress` | Active | open | — |
| `done` | Completed | closed | **yes** |
| `canceled` | Canceled | closed | **no** |

`canceled` is new. Closing a sprint does not carry canceled issues over and does not count
their points as completed; burndown removes them from scope at the instant they were
canceled; velocity, cycle time and "resolved" count only `done`; the cumulative flow diagram
has a Canceled band. §9 has the full list.

Changing a populated stage's category re-labels its current issues *and its history* in every
report (history stores stage ids, and a stage has one category). The preview says so and
publishing needs `acknowledge_reporting_changes: true`.

### 4.3 Flow

`spaces.transition_mode`:

- `any` (the default): an issue may move from any stage to any other. Board column order
  imposes nothing.
- `restricted`: only the transitions in `workflow_transitions` are allowed. A transition is
  `from_status_id → to_status_id`; `from_status_id = NULL` means "from any stage" (a global
  transition, e.g. to Canceled). Branching, skipping and reopening are just transitions.

Reordering inside a stage is always allowed. `spaces.initial_status_id` is where new issues
start; in `restricted` mode a create naming another stage is refused, in `any` mode any live
stage is accepted (the board's quick-add creates into the column it is in). There is no FK on
`initial_status_id` or on `merge_target_status_id` (§6): `workflow_statuses.space_id →
spaces.id` already exists, and the reverse FK would make the two tables a cycle that neither
`metadata.sorted_tables` nor MySQL's drop order can resolve. The application validates both
on every write.

`services/transitions.py` is the one place that answers "may this issue move from A to B",
"which stages can it reach from A" and "what is the initial stage"; the board move, the
webhook and the SPA's tree payload all go through it. The refusal names the allowed
destinations: *"In Review → Done is not an allowed move in this workflow. From In Review an
issue can move to: QA, In Progress."*

### 4.4 Editing: draft, preview, publish

The SPA holds the draft; nothing is stored until it is published, so cancelling an
unpublished edit is discarding the draft. Two endpoints:

`POST …/spaces/{s}/workflow/preview` with a `WorkflowDraft`:

```json
{
  "base_version": 3,
  "transition_mode": "restricted",
  "wip_policy": "warn",
  "stages": [
    {"ref": "6b1…", "name": "Backlog", "category": "todo", "color": "neutral",
     "counts_toward_wip": false, "wip_limit": null},
    {"ref": "new:qa", "name": "QA", "category": "in_progress", "color": "warning",
     "counts_toward_wip": true, "wip_limit": 2}
  ],
  "initial": "6b1…",
  "transitions": [{"from": "6b1…", "to": "new:qa"}, {"from": null, "to": "new:qa"}],
  "replacements": {"<removed stage id>": "new:qa"},
  "merge_action": {"mode": "status", "target": "new:qa"}
}
```

A `ref` is an existing stage id, or `new:<anything>` for a stage to create. Array order is
board order. A live stage not in `stages` is being **removed**.

The answer (`WorkflowPreview`) is everything the plan's "preview before publishing" asks for:
`valid`, `errors[]` and `warnings[]` (each `{code, message, refs}`), the resolved stages with
what changed and how many issues each holds, the removed stages with their issue counts and
replacements, category changes, how many issues will move (into closed stages; in the active
sprint), the transition list with names, the merge action's validity, and
`affected_issue_count`.

Errors (publishing is refused while any exists):

| Code | When |
|---|---|
| `no_stages`, `too_many_stages` | 0 or more than 20 stages |
| `name_invalid`, `duplicate_name` | blank / over 50 characters; two stages with the same name ignoring case |
| `category_invalid`, `color_invalid`, `wip_limit_invalid` | outside the enums / 1–999 |
| `unknown_ref` | a transition, replacement, initial or merge target names no stage of the draft |
| `initial_missing`, `initial_closed` | no initial stage; an initial stage in a closed category |
| `no_completed_stage` | no stage in `done` — nothing could ever be delivered |
| `self_transition` | `from` equals `to` |
| `unreachable_stage` | `restricted`: a stage cannot be reached from the initial one |
| `replacement_required` | a removed stage still holds live issues and has no replacement |
| `replacement_invalid` | a replacement that is itself being removed, or not a draft stage |
| `merge_target_invalid` | `status` mode without a remaining target, or a `canceled` target |

Warnings: `dead_end_stage` (an open stage nothing leaves, in `restricted` mode),
`category_change`, `moves_into_closed`, `active_sprint_moves`, `wip_over_limit` (a stage
already holds more than its new limit), `merge_target_unreachable` (stages from which a merge
could not reach the target, so merges there will fail visibly, §6), `duplicate_transition`
(dropped).

`PUT …/spaces/{s}/workflow` with the same draft plus `confirmed_affected_issues` (the count
the person saw) and `acknowledge_reporting_changes`. One transaction, under a row lock on the
space:

1. `base_version` must equal `spaces.workflow_version` — else 409 (someone published since).
2. Validation runs again against the database as it is now — 422 with the preview as `data`.
3. The number of issues that will move must equal `confirmed_affected_issues` — else 409
   ("7 issues are in Review now, not 5; preview again").
4. New stages are inserted, kept ones updated, removed ones soft-deleted.
5. Each issue in a removed stage moves to its replacement, appended to the bottom of that
   column in its existing order, with a `status_changed` event by the publisher. Identities,
   keys, comments, time and history are untouched; nothing is silently completed — moves into
   closed stages were in the preview.
6. Transitions are replaced; initial stage, mode, WIP policy and merge action are set;
   `workflow_version` increments.
7. A `workflow_revisions` row stores the published definition and the impact summary, and the
   audit log gets one line.

Resetting to the default is loading the `default` template into the draft and publishing it
through the same steps — the same impact review and replacement safeguards.

### 4.5 Templates

Built in (`services/workflow_templates.BUILTIN_TEMPLATES`):

| Key | Stages | Flow | Merge |
|---|---|---|---|
| `default` | To Do → In Progress → In Review → Done | any | Done |
| `delivery` | Backlog → Ready → Development → Review → QA → Released; rework Review → Development, QA → Development; reopen Released → Development | restricted | QA |
| `support` | New → Triage → Investigating → Resolved; reopen Resolved → Investigating | restricted | none |

These are the plan's examples, not enforced shapes. A workspace can save its own
(`workflow_templates`): from a space's current workflow or from a definition. A template is a
definition with stages by position, `initial` and transition ends as stage indexes, and
`merge_target` as an index or null. `GET/POST …/workflow-templates`, `DELETE
…/workflow-templates/{t}`.

### 4.6 Who may edit

Editing a space's workflow needs **workflow-edit permission on that space**: workspace admins
and owners always have it; an admin can grant it per space to members
(`space_workflow_editors`, `GET/PUT …/spaces/{s}/workflow/editors`). That is the plan's
"authorized users can customize their Space's workflow without requiring administrator
intervention for each change": the admin grants once, the space's lead edits freely. Everyone
else can still *execute* the transitions the workflow allows. The SPA warns that publishing
affects the whole space; boards and lists of the space all show the same stages and obey the
same rules, and board filters never create states of their own.

### 4.7 WIP

A stage's WIP count is the space-wide number of live **story, task and bug** issues in it —
epics are outcomes and subtasks are parts of an item already counted. The board shows
`count / limit` on every column with a limit and marks it over. `spaces.wip_policy`:

- `warn` (default): a move over the limit succeeds and the answer says it is over;
- `block`: the move is refused (409) with the limit in the message.

Blocked work stays in its stage and still counts (§5.3).

## 5. Views and planning

### 5.1 Boards

`GET /workspaces/{w}/board` takes exactly one of `list_id`, `space_id` or `sprint_id`:

- **Delivery Kanban** — one list (as before).
- **Space overview** — every list of a space; cards carry `list_id` and the SPA labels them.
- **Active sprint board** — every issue in the sprint, across lists; columns are its space's
  stages.

Filters on all three: `assignee_id` (an id, `me` or `none`), `type` and `priority`
(repeatable), `q` (title, or an issue key including old keys), `blocked=true`, `epic_id` (the
epic and everything under it). Each column carries `wip: {count, limit, over_limit}` computed
without the filters — WIP belongs to the team, not to whoever is filtering. The board also
returns the space's `transition_mode`, `initial_status_id` and transitions, so a drag can be
checked before it is sent.

### 5.2 Backlog

`GET …/spaces/{s}/backlog`: the active sprint and the planned sprints, each with its cards,
then the **ordered backlog** — open story/task/bug issues of the space in no sprint — and the
space's open epics for filtering. Order is `issues.backlog_rank`, a fractional rank over the
whole space kept apart from the board's per-column `rank`: the backlog is one ordered list per
delivery unit (plan §4 Scrum policy), the board orders within a column, and one key cannot do
both without rewriting a space whenever a column rebalances. `POST …/issues/{i}/backlog-rank`
with `after_id`/`before_id` reorders it, with the same stale-neighbour rule as the board move
(02-data-model.md §2.4). At most 500 backlog cards come back (`truncated` says when there were
more); filters narrow it.

Planning across the whole space is therefore the backlog plus the existing sprint routes
(`…/sprints/{id}/issues`), which already refuse an issue of another space.

### 5.3 Blocked and age

`issues.blocked` with `blocked_reason` (required when blocking: what blocks it, who owns the
blocker, the next action — plan §4), `blocked_at`, `blocked_by_id`; events `blocked` /
`unblocked`. Set through `PATCH …/issues/{i}`.

`issues.status_entered_at` is set on create and on every status change, and backfilled from
the last `status_changed` event. The card shows how long an issue has been in its stage — the
work-item age the Kanban policy inspects daily.

### 5.4 Saved board filters

`saved_views`: a name, a scope (`list`/`space`/`sprint` and its id), a view type (`board`,
`list`, `backlog`), the filters of §5.1 and a visibility (`private`, `workspace`). `GET/POST
…/saved-views`, `PATCH/DELETE …/saved-views/{v}`; the owner edits, an admin may delete a
shared one.

## 6. Definition of Done and the GitHub merge action

### 6.1 Configuration

The merge action is part of the space's workflow because it must be revalidated whenever the
stages change: `spaces.merge_action` is `status` (move a linked issue to
`merge_target_status_id` when its pull request merges) or `none` (link only). The migration
sets every existing space to `status` with its current first Done stage — exactly what the
hard-wired rule did — and new spaces take the template's target. There is no "first done
stage" mode any more: choosing a target by position is the inference the plan rules out.

### 6.2 Applying a merge

For each issue the pull request names (keys resolved through §7.3, old keys included):

1. Space frozen → the whole delivery is **deferred** (§6.3).
2. Space, folder or list archived → the issue is skipped and named in `skipped`.
3. The link is created or refreshed, as before.
4. On merge with mode `status`: already at the target → nothing; already in a closed category
   → nothing (a merge never reopens or re-closes work); target no longer a live stage, or
   (`restricted`) no transition from the current stage → **failure**: an `automation_failed`
   event on the issue with the reason, the key in `failed`, a warning log line. Never a
   fallback to some other completed stage.

`WebhookResult` gains `moved` (keys moved), `failed` and `skipped` (`{key, reason}`), and
`deferred`. `moved_to_done` stays, and lists only moves into a `done`-category stage.

### 6.3 Deferral and replay

`webhook_deliveries` gains `status` (`processed`, `deferred`, `replayed`, `failed`),
`workspace_id`, `payload` (kept for deferred deliveries only), `processed_at` and `note`. A
deferred delivery is acknowledged to GitHub with 200 so it is not retried. Unfreezing a space
replays the workspace's deferred deliveries oldest first through the same code path; one whose
issues are still in a frozen space stays deferred. `GET …/integrations/deferred` (admin) lists
them.

## 7. Transfers

### 7.1 Within a space

`POST /workspaces/{w}/issues/transfer` with a target list of the same space changes `list_id`
only: key, status, rank, sprint (by default) and parent are kept, `list_changed` is recorded.
Member role — moving work between lists is everyday work.

### 7.2 Across spaces

The same endpoint with a list in another space of the same workspace. Admin role, because it
re-keys issues and changes their workflow. Rules:

- **Subtrees move whole.** A parent must be in the same space as its children, so an issue
  with descendants moves with all of them (`include_descendants`, default true; false is a
  problem when descendants exist). Relationships inside the subtree are kept as they are.
- **A parent that stays behind is detached only on request** (`detach_from_parent: true`);
  then a `relates` link to the former parent keeps the context (plan Phase 2: "preserve
  original context in history"), and `parent_changed` is recorded.
- **Every stage in play needs a mapping** (`status_map`: source stage id → live stage of the
  target space). A mapping may not turn canceled work into completed work, ever, and may not
  change the open/closed/completed class at all unless `allow_category_changes` is true.
  Transitions are not checked: a mapping is data placement, not a move through the flow.
- **Sprints belong to spaces.** An issue in a sprint of the source space leaves it — the
  membership row is closed, not deleted, with `sprint_removed` — and joins `target_sprint_id`
  (a planned or active sprint of the target space) when one is given.
- **New keys, old keys kept.** Numbers are allocated in the target space parent-first, under
  the space row lock that already serialises creates. The old `(space key, number)` is stored
  in `issue_key_aliases`. Events: `space_changed`, `key_changed`, `list_changed`, and
  `status_changed` when the mapping changes the stage.
- Ranks go to the bottom of the target column and backlog, in the moved issues' existing order.
- Comments, time entries, pull-request links, events, AI jobs and historical sprint rows keep
  pointing at the same issue id: nothing is deleted and recreated.

Request: `issue_ids` (1–200), `target_list_id`, `include_descendants`, `status_map`, `sprint`
(`keep` — same space only — or `backlog`), `target_sprint_id`, `detach_from_parent`,
`allow_category_changes`, `dry_run`. With `dry_run` nothing is written and the planned moves
come back with their would-be keys omitted; with problems nothing is written and the answer is
422 with every problem (`{issue_id, key, code, message}`) — all or nothing. At most 500 issues
including descendants.

### 7.3 Resolving keys

`services/key_resolution.resolve_key(db, workspace_id, space_key, number)` is the only
resolver: the live key first, then a space-key alias, then an issue-key alias. The GitHub
webhook, `GET /workspaces/{w}/issue-keys/{key}` (deep links: answers the card, its list and
space, the current key and how it was resolved) and the `q` filter all use it. Issue numbers
are never reused — `issue_counter` only increments — so an alias cannot be shadowed by a new
issue.

## 8. Migration

The plan's Phases 1–5 as tooling, all admin-only, under `/workspaces/{w}/…`.

### 8.1 Inventory

`GET …/inventory` (`?format=csv` for one row per list): per space — owner, delivery method,
charter gaps, write state, workflow summary and merge action, sprints (the active one and
counts), issue counts by type, by category, by stage and by **parent depth**, legacy
relationships by kind with example keys, epics without owner or criteria, blocked count, time
logged, pull-request links, last activity; per folder and list — counts, last activity and
flags. Findings, each with a severity: sprint-named lists ("Sprint 12"), status-named lists (a
list named like a stage of its space), duplicate list names, empty and inactive containers,
spaces without an owner or a complete charter, legacy relationships, bugs under subtasks,
merge targets with open stages after them (merges would complete work before acceptance —
plan §4 migration gate), deferred deliveries. Plus membership counts by role and the linked
repositories.

### 8.2 Baselines

`POST …/migration-baselines` captures, for chosen spaces (default all), per-space totals and a
per-issue fingerprint: key, space, list, stage, parent, comment count, seconds logged,
pull-request links, sprint rows, event count. `GET …/migration-baselines`, `GET …/{id}`
(totals), `GET …/{id}/compare` (now against then). Plans capture one at approval.

### 8.3 Plans and the register

`POST …/migrations` with a name and **operations**:

| `op` | Fields | Expands to |
|---|---|---|
| `move_issues` | `issue_ids`, `target_list_id`, transfer options (§7.2) | one `issue_move` item per issue, descendants included, grouped by subtree |
| `move_list_issues` | `source_list_id`, `target_list_id`, transfer options | the same, for every live issue of the list |
| `move_list` | `list_id`, `folder_id` | one `list_reparent` item (same space) |
| `retype` | `issue_ids`, `to_type` | one `issue_retype` item each |
| `reparent` | `issue_id`, `parent_id` | one `issue_reparent` item |
| `set_status` | `issue_ids`, `status_id` | one `issue_set_status` item each — a status-named list mapped to a stage |
| `archive` | `kind`, `id` | one `container_archive` item |
| `retain` | `issue_ids` or `list_id`, `reason` | `retain` items: explicit retain-in-place decisions |

Destination containers are created beforehand with the ordinary routes; an empty list harms
nothing and the dry-run can then check against it.

Each item is one row of the plan's **migration register** (`migration_items`): kind, subject
(id and key at validation), source and target ids, options (status mapping, sprint treatment),
owner, exception note, approval, state (`pending`, `applied`, `failed`, `skipped`,
`rolled_back`), batch, the `before`/`after` snapshot (old and new key, list, space, stage,
parent, sprint, version) and errors. `GET …/migrations/{m}/register?format=csv|json` exports it.
Items are ordered parent-first, and a subtree is one group that is never split across batches —
an intermediate state with a cross-space parent would be an illegal relationship.

### 8.4 Lifecycle

```text
draft ─validate─▶ validated ─approve─▶ approved ─freeze, execute─▶ running ─▶ completed
  ▲                  │                                         │            │
  └──── re-validate ─┘                                       failed ◀───────┘ (an item fails)
canceled (before execution)          rolled_back (after rollback)
```

- `validate` is the dry-run: it re-expands the operations against current data (keeping each
  item's approval and exception by subject), runs the transfer and rule checks, and reports
  every problem. Zero problems → `validated`.
- `PATCH …/items/{i}`: approve, set the owner, write an exception, or mark retain-in-place.
- `approve` needs `validated`, every item approved or retained (plan §7 gate 1), and captures
  the baseline.
- `freeze` freezes every affected space with `frozen_by_plan_id` (plan Phase 5 step 2).
- `execute` needs `approved`/`running`/`failed` and every affected space frozen by this plan.
  It re-plans each batch against the data as it is (the final dry-run), applies up to
  `max_batches` × `batch_size` items, each batch in **one transaction**, and returns progress;
  the SPA calls it until nothing is pending. Applied items are skipped, so a repeated call
  never applies anything twice (idempotent, resumable). A failing batch is rolled back whole,
  the failing item is marked `failed` with its error in a separate commit, and the plan stops
  as `failed` — fix the cause (or mark the item retained) and execute again.
- `cancel` (before execution) unfreezes and closes the plan.

### 8.5 Rollback and unfreezing

`rollback` reverses applied items in reverse order, in batches: an `issue_move` goes back to
its space, list, number (numbers are never reused, so the original key is still free; its
alias row is removed and the destination key becomes an alias, because it was public for a
while), stage, parent and — when that sprint is still open — sprint, with events recording the
move back. An item whose issue changed since it was applied (`issues.version` differs from the
recorded one) is **not** reverted: it is listed for forward repair, because reverting it would
erase work done after the migration (plan §7: after writes reopen, use forward repair or a
specifically reconciled rollback).

`unfreeze` is refused while the plan is `running` or `failed` — the plan says to keep writes
paused while reversing — and otherwise unfreezes the plan's spaces and replays deferred
deliveries.

### 8.6 Reconciliation gates

`GET …/migrations/{m}/reconcile` evaluates the plan's §7 hard gates against the baseline and
the register, each `{gate, passed, summary, details[]}` (details capped at 50, with the count):

| Gate | Check |
|---|---|
| `mappings_complete` | every in-scope issue (baseline, source containers) has an approved item or a retain decision |
| `no_missing_or_duplicate` | every baseline issue still exists; no two live issues share a key; no issue appeared in scope during the freeze |
| `relationships_valid` | no parent missing or deleted, no parent in another space, no relationship outside standard ∪ legacy, no stage of another space or removed, no list of another space |
| `history_reconciles` | per issue: comments, seconds logged and PR links equal the baseline; sprint rows and events never fewer |
| `access_unchanged` | every moved issue in the same workspace; assignees still members |
| `keys_resolve` | for every applied move both the old and the new key resolve to the issue |
| `no_premature_done` | no move into a closed stage during the plan except those its mappings name; no automation completed anything in the plan's spaces since the freeze |
| `workflow_consistent` | every issue's stage is a live stage of its space |

### 8.7 What a wave does to reports

Reports filter by an issue's *current* space and list, so an issue moved to another space takes
its history with it; days already snapshotted (`issue_daily_status`) keep the old space. The
baseline is the pre-migration record; the plan says not to promise identical historical numbers
without validating their scope, and this is why.

### 8.8 Backups

Taking and test-restoring a database backup is an operator step, not an API: see
`docs/runbooks/restructuring-migration.md`.

## 9. Reports and sprints under the new categories

- `done` is completed; `canceled` is closed but not delivered.
- Sprint close: `done` issues stay and count; `canceled` issues stay, do not count, and are not
  carried over (`canceled_issues` in the result); open issues carry over as before.
  `SprintSummary` gains `canceled_issue_count`.
- Burndown and burnup: an issue canceled at an instant leaves the scope at that instant.
- Velocity and the sprint report: completed is `done` only; the sprint report names canceled
  issues as their own outcome.
- Cumulative flow: a Canceled band; removed stages appear only while they held issues, labelled
  "(removed)".
- Project overview, epic progress: percentages are of non-canceled issues; overdue excludes
  closed issues.
- Cycle and lead time, created vs resolved: resolved means reached `done`.
- The report cache key includes the space workflow versions, so a category change is not
  answered from a cache computed under the old one.

## 10. Dependencies and releases

**Typed dependencies** (`issue_links`): `blocks`, `relates`, `duplicates`, `clones`, between any
two issues of the workspace, including across spaces — the plan's "record cross-space
coordination explicitly" without forcing a cross-space parent. Directional kinds read "A blocks
B / B is blocked by A"; `relates` is stored once and refused in reverse. `GET/POST
…/issues/{i}/dependencies`, `DELETE …/dependencies/{d}`. Events `dependency_added` /
`dependency_removed` on both issues.

**Releases** (`releases`, `issue_fix_versions`): per space, `unreleased` → `released` (→
`archived`), with start and release dates. `GET/POST …/spaces/{s}/releases`, `GET/PATCH/DELETE
…/releases/{r}`, `POST …/releases/{r}/release` (optionally moving open issues to another
release), `GET …/releases/{r}/notes` (the release's issues by type and outcome, as text),
`GET/PUT …/issues/{i}/fix-versions`. Managing releases is admin; assigning fix versions is
member.

## 11. Data model summary

Revisions `20261005d001` (workflow), `20261005e001` (structure and governance),
`20261005f001` (transfer and migration) and `20261005g001` (dependencies, releases, views);
each has a real downgrade.

| Table | New columns / purpose |
|---|---|
| `workflow_statuses` | `color`, `counts_toward_wip`, `wip_limit`, `deleted`, `deleted_at`, `updated_at` |
| `spaces` | `initial_status_id`, `transition_mode`, `workflow_version`, `wip_policy`, `merge_action`, `merge_target_status_id`, `description`, `owner_id`, `delivery_method`, `charter`, `archived_at`, `archived_by`, `frozen_at`, `frozen_by`, `freeze_reason`, `frozen_by_plan_id`, `updated_at` (existing) |
| `folders`, `lists` | `archived_at`, `archived_by`, `updated_at` |
| `issues` | `blocked`, `blocked_reason`, `blocked_at`, `blocked_by_id`, `status_entered_at`, `backlog_rank`, `completion_criteria` |
| `webhook_deliveries` | `status`, `workspace_id`, `payload`, `processed_at`, `note` |
| `workflow_transitions` | the restricted flow |
| `workflow_templates` | workspace templates |
| `workflow_revisions` | every published workflow and its impact |
| `space_workflow_editors` | per-space edit grants |
| `space_key_aliases`, `issue_key_aliases` | old keys that must keep resolving |
| `migration_plans`, `migration_items`, `migration_baselines` | §8 |
| `issue_links`, `releases`, `issue_fix_versions`, `saved_views` | §10, §5.4 |

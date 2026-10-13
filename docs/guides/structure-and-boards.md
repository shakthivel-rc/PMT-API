# Structuring work and running boards in PMT

For PMT administrators, project leads, product owners and delivery teams. It is the published
form of the conventions in `pmt-restructuring-plan.md` (Phase 6: "publish the hierarchy, naming
rules, board policies, and creation checklist"). How the product enforces them is in
[`docs/design/09-restructuring.md`](../design/09-restructuring.md).

## 1. Where work lives versus how it is viewed

Every issue has **one home**: one list, in one space. Boards, the backlog, the sprint board
and reports are **views** of those same issues. Moving a card on a board, filtering a view or
adding an issue to a sprint never copies it and never changes where it lives.

| Question | Answered by | Not by |
|---|---|---|
| Where does this work belong? | Its space and list | A list named after a status or a sprint |
| What outcome does it serve? | Its epic | A folder or a list |
| When is it being delivered? | Its sprint | A list called "Sprint 12" |
| What state is it in? | Its stage on the board | Which list it is in |

## 2. The hierarchy

| Level | Use it for | Create one when | Do not create one for |
|---|---|---|---|
| **Workspace** | The organisation, or a client that needs its own access boundary | Access must be isolated | Every team or project |
| **Space** | A stable product or delivery unit with its own workflow and sprint cadence | It has an independent backlog or cadence, or a materially different workflow | A feature, a phase or a sprint |
| **Folder** (optional) | Grouping related lists | Several durable workstreams need grouping | A single list |
| **List** | The canonical home of a coherent stream of work | Ownership or intake policy genuinely differs | A status, a sprint or a person |
| **Epic** | A measurable outcome spanning several items | It has an owner and completion criteria | A department or a permanent bucket |
| **Story / Task / Bug** | Independently prioritised delivery items — peers | — | — |
| **Subtask** | Execution detail under a story, task or bug | Separate assignment or tracking adds value | Standalone work (make it a task) |

**Standard shape:** Epic › Story / Task / Bug › Subtask. Stories, tasks and bugs can also stand
alone (maintenance work, defects). PMT enforces this shape for every relationship you create or
change. Older trees (a task under a story, a bug under a subtask) keep working until someone
changes them; the inventory lists them so they can be reviewed.

**One active sprint per space.** Teams that run independent sprint cadences need separate
spaces; several lists in one space share one sprint.

**Across spaces,** use a dependency link (*blocks*, *relates to*, *duplicates*, *clones*) — never
a parent from another space.

## 3. Naming rules

- **Space keys** are 2–10 capitals or digits (`PAY`, `OPS2`). They appear in every issue key,
  branch name and commit message, so keep the key you have. If a key must change, PMT keeps the
  old one working and reserved — but every external tool that displays keys will show the new
  one, so treat a change as an announcement.
- **Spaces** are named for the product or delivery unit (`Payments`, `Client A Delivery`), never
  for a date or phase.
- **Lists** are named for a stream of work (`Product Delivery`, `Service Requests`). Never name
  a list after a status (`In Progress`), a sprint (`Sprint 12`) or a person.
- **Epics** are named for the outcome (`Reliable time reporting`), with completion criteria that
  someone could check.
- **Stages** are named for what is happening (`Review`, `QA`), not for who is doing it.

## 4. Creating a space: the checklist

The **New space** form walks through this. An administrator can make the starred items
mandatory (Workspace settings › Governance › *Require the space charter*).

1. ★ Business objective and ★ success measure.
2. ★ Accountable owner, and the delivery team.
3. Scope, and what is explicitly out of scope.
4. Access requirements (who must, and must not, see it).
5. ★ Delivery method: Scrum or Kanban.
6. ★ Why an existing space cannot be reused.
7. The canonical list(s) — usually one to start with.
8. The workflow template.
9. Epic completion criteria and the initial backlog.
10. Reporting cadence, and the archival/retention policy.

**Roles.** The PMT administrator manages structure, permissions and default templates. The
product owner or delivery lead owns priorities and outcomes. The team owns estimates and
execution. An administrator can grant a space's lead *workflow-edit* permission once, so the
lead can change that space's workflow without asking each time.

## 5. Boards and workflows

### The default and how to change it

Every new space starts with **To Do → In Progress → In Review → Done**, and any stage can move to
any other. That is a starting point, not a rule. **Customize workflow** (on the board, or in the
space settings) lets anyone with workflow-edit permission:

- add, rename, recolour, reorder and remove stages (renaming or reordering never changes an
  issue's identity or history);
- choose each stage's **reporting category** — *Not started*, *Active*, *Completed* or
  *Canceled* — which is what reports and automation use, never the name or the position;
- choose which active stages count toward **WIP**, and set limits;
- keep movement **free**, or **restrict** it to the transitions you draw (including branches,
  skips, rework and reopen paths, and "from any stage" moves such as *to Canceled*);
- choose the **initial stage** for new issues;
- choose what a **merged pull request** does: move the issue to a chosen stage (for example QA),
  or only link it;
- start from a template (*Default*, *Delivery with QA*, *Support*, or your workspace's own), and
  save your workflow as a template.

**Preview before you publish.** The preview shows the new board, every issue that will move,
reporting changes and what happens to merges. Publishing changes the workflow for the **whole
space** — every list, the sprint board and reports. Removing a stage that still holds issues
asks you to choose where they go and to confirm how many; nothing is moved, completed or deleted
silently. Unpublished edits can be discarded at any time.

### Blocked work

There is no Blocked column by default. Mark the issue **Blocked** and say what blocks it, who
owns the blocker and the next action. It stays in its stage and still counts toward WIP. A team
that wants a Blocked stage can add one; decide whether it counts toward WIP.

### Canceled work

Close work that will not be delivered in a **Canceled** stage (add one to your workflow). It
leaves the backlog and the sprint without counting as delivered: velocity, burndown "completed",
cycle time and release notes all treat it separately. Never move canceled work to Done to make
the numbers tidy.

## 6. Views

| View | Where | What it shows |
|---|---|---|
| Delivery board | A list | That list's issues by stage |
| Space overview | A space | Every list's issues by stage, labelled with their list |
| Active sprint board | A space | Every issue in the active sprint, across lists |
| Backlog | A space | The sprints and the ordered backlog of unscheduled work; plan sprints from here |
| Reports | Reports | Outcome, flow and sprint reports over any space, list or sprint |

Save a filter (assignee, type, priority, blocked, epic, search) as a **saved view**, privately or
for the whole workspace.

## 7. Scrum policy

- Start with **two-week sprints** unless the team has a reason for another cadence.
- One **ordered backlog** per space and a clear **sprint goal**.
- Plan from availability, recent throughput and readiness — not a utilisation target.
- Commit stories, tasks and bugs; do not count both a parent's estimate and its subtasks'.
- Review scope changes explicitly; PMT keeps the added/removed history.
- At close, unfinished work returns to the backlog or a chosen future sprint. Never recreate it.
- Hold planning, daily coordination, review and retrospective; refine the backlog regularly.

## 8. Kanban policy

- Use it for support, operations, maintenance and unpredictable intake.
- Define when work is accepted, started and completed — your stages and categories say it.
- Set **WIP limits** from actual capacity (a team of four might pilot four active items and two
  in review), then adjust from evidence. A space can warn when a limit is exceeded or refuse the
  move.
- At most **one expedited item** at a time, with explicit approval (a convention: PMT has no
  expedite field).
- Replenish weekly; inspect blocked and **ageing** work daily (cards show how long they have been
  in their stage).
- Track throughput, cycle time and work-item age, and set service expectations from what you
  observe.

## 9. Definition of Done and GitHub

Agree what Done requires: review, tests, deployment, business acceptance, documentation. Then make
the merge action match it. If acceptance or QA happens after merge, point the merge action at the
QA stage, not at Done — otherwise merged work is reported complete before it is accepted. When a
merge cannot move an issue (the workflow does not allow that move, or the target stage was
removed), PMT records the failure on the issue instead of choosing another stage.

## 10. Measuring the change

Compare against the baseline after enough delivery cycles: backlog readiness, WIP adherence,
blocked-item age, cycle time, sprint-goal achievement and carryover. Do not promise a percentage
improvement in advance, and never use velocity to rank teams.

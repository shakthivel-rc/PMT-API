# PMT design

PMT is a multi-tenant project-management platform for software teams. It combines Jira's
depth for developer workflows (issue keys, a query language, VCS and CI on the ticket, Agile
reports) with ClickUp's flexible hierarchy and views, and it has AI features built in.

This folder holds the design, and the first phase of it is built in this repository and in
`../pmt-app`. For each document, the table says what it answers.

| # | Document | What it answers |
|---|---|---|
| 1 | [Architecture](01-architecture.md) | The components, request flows, high availability (no single point of failure, RTO/RPO targets), multi-tenancy (application layer plus RLS, and cells), consistency and the latency budget. |
| 2 | [Data model](02-data-model.md) and [schema.sql](schema.sql) | The tables and why: hierarchy, issues, custom fields, sprints, time, VCS/CI, events, vectors, and RLS. The full PostgreSQL DDL was tested on PG16 with pgvector. |
| 3 | [AI strategy](03-ai-strategy.md) | The queue and worker infrastructure, the model gateway, prompts, RAG, and each AI feature (summaries, estimation, auto-assignment, ticket generation). Also evaluation and governance. |
| 4 | [API and views](04-api-and-views.md) | REST endpoints (Phase 1 built, later phases designed), the multi-view engine, the PQL query language, automation and realtime. |
| 5 | [Tech stack](05-tech-stack.md) | What to use at each layer and why, including the move from MySQL to PostgreSQL and what is deliberately left out for now. |
| 6 | [Phase 1 plan](06-phase-1-plan.md) | What is built today, the remaining steps with their "done when" criteria, the SLOs, and how to run it. |
| 7 | [Time tracking](07-time-tracking.md) | Brought forward from Phase 2 on 2026-10-05: time on tickets, time by person, the timer, and why each decision differs from Jira's (seconds, a derived remaining estimate, full roll-ups, a visibility setting). |
| 8 | [Reports](08-reports.md) | Built on 2026-10-05: fifteen reports over time, projects, sprints and flow from one engine; CSV, XLSX and PDF exports; saved and scheduled reports; sprints; the metric definitions and the permissions. |
| 9 | [Restructuring](09-restructuring.md) | Built from 2026-10-05 (`pmt-restructuring-plan.md`): the standard hierarchy (Epic › Story/Task/Bug › Subtask), the space charter and governance, archive and freeze, user-customisable workflows (stages, reporting categories with `canceled`, free or restricted transitions, templates, preview and safe publish), WIP, blocked and ageing work, the space and sprint boards and the backlog, saved views, the configurable GitHub merge action, transfers with old-key aliases, migration plans with dry-run, batched execution, reconciliation and rollback, dependencies and releases. The user guide is [`../guides/structure-and-boards.md`](../guides/structure-and-boards.md); the operator's runbook is [`../runbooks/restructuring-migration.md`](../runbooks/restructuring-migration.md). |

## Decisions in one paragraph

- **Stack.** Build on the hardened base project (FastAPI and React) rather than rewriting it,
  and move its database to PostgreSQL for RLS, JSONB and pgvector.
- **Tenancy.** `workspace_id` is on every row, enforced twice: once by the application's
  membership gate, which returns 404 to non-members, and again by row-level security in the
  database.
- **Views.** Every view draws one issue projection. A view is a grouping of that data, not a
  separate endpoint shape.
- **Board order.** Board order is a fractional rank, so moving a card writes one row.
- **Events.** Every change is appended to an event log in the same transaction, and the Agile
  reports are computed from that log.
- **AI.** Model calls never run on the request path: a table-backed queue feeds a separate
  worker pool, and the API process does not have the model client installed. AI output is
  structured, versioned and cached, and people accept or reject each suggestion.
- **New infrastructure.** Redis, OpenSearch and Kafka are each added when a measurement calls
  for them, not before.

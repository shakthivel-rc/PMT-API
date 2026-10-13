# 6. Phase 1 implementation plan: the MVP

**Phase 1 goal:** a team can plan and track its work on a Kanban board, see its pull requests
on its tickets (with a merge moving the ticket to Done), and get an AI summary of any thread.
This has to run on an architecture that later phases extend rather than replace.

**Scope:** the core issue tracker, the board view, GitHub integration, and one AI feature
(thread summarisation).

**Status legend:** ✅ built and tested in this change · 🔜 next · ⏳ later in Phase 1

## 6.1 What already exists (built in this change)

| Area | Built | Where |
|---|---|---|
| Foundation | The base project copied to `pmt-api` / `pmt-app`, with sibling-repo wiring renamed. `base-project-*` is left untouched as the reusable template. | whole repos |
| Schema | 13 tables in a portable Alembic revision, with a real downgrade. The upgrade → downgrade → upgrade chain and model/migration parity pass on SQLite **and MySQL 8**. | `alembic/versions/20260929a001_project_management_core.py`, `models/` |
| Tenancy | Workspaces, members, roles (guest, member, admin, owner). `workspace_access` returns 404 for non-members and checks the role. | `dependencies/workspace.py`, `routes/workspaces.py` |
| Hierarchy | Space (issue key and default workflow), folder, list, and one tree read for the sidebar | `services/workspace.py` |
| Issues | Create, list and search, detail, PATCH with optimistic `version`, soft delete, comments. Epic → story → task → sub-task (bugs anywhere), with a cycle check. `issue_events` on every change. | `controllers/issue_controller.py`, `services/issue.py` |
| Board | Board by list or space, all columns included. Moves rewrite one row using fractional ranks. The server re-reads neighbours so stale boards still drop cards correctly, and long keys are rebalanced. | `utils/rank.py`, `…/board`, `…/move` |
| GitHub | HMAC-verified webhook. PRs are linked from the key in the branch, title or body. A merge moves the issue to Done. Duplicate deliveries are ignored. A repository can be linked to only one workspace. | `integrations/github.py`, `controllers/github_controller.py` |
| AI | Summaries run through a queue (`ai_jobs`) and a worker with a lease and fencing token. The result is cached against the input hash. Claude is called with fallbacks, adaptive thinking and structured output. Off by default. | `ai/`, `workers/ai_worker.py`, `services/ai_jobs.py` |
| SPA | `/projects`: workspace list and creation, the hierarchy sidebar, a Board/List toggle sharing one dataset, drag-and-drop plus a keyboard "Move to…" menu, quick-add, and an issue drawer with comments and "Summarize thread" | `pmt-app/src/features/projects/`, `src/api/{workspaces,issues}.ts` |
| Design | These documents, plus the PostgreSQL target DDL (`schema.sql`), tested on PG16 with pgvector including RLS | `docs/design/` |

**Gates, measured 2026-09-29:**

| Repo | Result |
|---|---|
| `pmt-api` | 477 tests pass (70 live-stack skips). Coverage 78.9% overall, 54.9% for the unit subset. mypy is clean on the widened scope. The lint ledger has no new findings. The OpenAPI digest is regenerated. |
| `pmt-app` | `npm run verify` passes: 506 tests, 93.3% statement coverage. The build is within budget; the total ceiling was raised to 272 kB for the new lazy-loaded chunk, with the reason recorded. |

## 6.2 Remaining steps, in order

Estimates assume two full-stack engineers and one frontend engineer.

### Step 1: PostgreSQL cutover 🔜 (1 week)

- Add `psycopg[binary]` and switch `DATABASE_URL` to `postgresql+psycopg://`.
- Compose uses `pgvector/pgvector:pg16`. Update the CI service, `bootstrap.sh --native`, and
  the `make db-shell` target.
- Fix the baseline revision: `Boolean` with `server_default=sa.text("0")` becomes
  `sa.false()`. Convert `CHAR(36)` keys to `uuid` with a new revision, not by editing the
  baseline.
- Delete the MySQL `READ COMMITTED` note in `tests/conftest.py`. PostgreSQL's default
  isolation level already behaves the way the test harness needs.
- **Done when:** `make verify` passes with `TEST_DATABASE_URL` pointing at Postgres, the
  migration chain runs up → down → up, and the live suite passes against the compose stack.

### Step 2: Row-level security 🔜 (3 days)

- Create a `pmt_app` role without `BYPASSRLS`, and write a migration applying the policies from
  `schema.sql`.
- Make `workspace_access` run `SET LOCAL app.workspace_id` and `app.user_id` on the request's
  session.
- Route the webhook and worker cross-tenant paths through the two `SECURITY DEFINER`
  functions.
- **Done when:** a new test deliberately drops the `workspace_id` filter from one service query
  and shows that the query returns nothing across tenants.

### Step 3: Finish the issue tracker UI ⏳ (1.5 weeks)

- In the drawer, edit title, description, type, parent, assignee, priority, points and due
  date through `PATCH` with `version`. A 409 prompts the user to reload.
- Create issues with a type and parent. Add a delete action.
- Member management screen: add a member by email, change a role. Link repositories from the
  UI.
- **Done when:** every Phase 1 API route is reachable from the SPA, and the Playwright journey
  "create a workspace, space and list; add issues; drag across the board; comment" passes,
  including the axe accessibility scan.

### Step 4: Board polish and performance ⏳ (1 week)

- A filter bar: assignee (including "me"), type and text, served by the list and board filters.
- WIP limits per column, shown as warnings.
- Per-column pagination and a virtualised column past 200 cards.
- Board load test with k6, using 50 concurrent users on a 2,000-card board. Target p95 under
  200 ms. **Add Redis caching only if the p95 is above about 120 ms**, using the generation-key
  design in 04 §4.3.
- **Done when:** the load-test report has been reviewed and the SLO dashboard shows board p95.

### Step 5: GitHub App ⏳ (1.5 weeks)

- Replace per-repository webhooks with a GitHub App. The install flow stores the installation
  ID. Installation tokens are fetched on demand and envelope-encrypted with a key separate
  from `SECRET_KEY` if they are cached.
- Handle `push` events: link commits by key and populate `file_touches`, which feeds
  auto-assignment later.
- Handle `check_run` and `workflow_run` events: populate `ci_pipeline_runs` and show CI status
  on the issue.
- Outbound: "Create branch" on an issue, named `{key}-{slug}`.
- Move webhook processing behind the queue, so the route only verifies and enqueues.
- **Done when:** PR, commit and CI status all appear on the issue within 5 s (p95) of the
  GitHub event, and a redelivery storm of 1,000 deliveries is absorbed with no duplicate
  effects.

### Step 6: AI to production quality ⏳ (1 week)

- Build a golden set of 50 real threads, a rubric grader, and a CI regression gate on any
  change to `PROMPT_VERSION`, the model or the effort level.
- Measure `AI_EFFORT=medium` against `high` on that set, and keep the cheaper setting if it
  matches.
- Add `ai_usage` accounting and a per-workspace monthly budget. Add the per-workspace setting
  `settings.ai.enabled`.
- Add the queue metrics (depth, age of the oldest job) and KEDA scaling for the worker.
- **Done when:** summary p95 from request to result is under 30 s, the golden-set score is at
  or above the agreed bar, and cost per summary is on the dashboard.

### Step 7: Production readiness ⏳ (1.5 weeks, overlaps steps 4–6)

- Helm charts for web, api and ai-worker (the gateway comes in Phase 2), with
  PodDisruptionBudgets, anti-affinity across 3 AZs, and HPA/KEDA.
- Postgres high availability: CloudNativePG, or a managed service with a synchronous standby,
  plus PgBouncer. Test a failover while under load.
- Backups: WAL archiving and PITR, with a **restore drill** performed and timed.
- OpenTelemetry tracing, SLO dashboards, and alerts on burn rate.
- A security review of the new public surface (webhook, tenancy) and a penetration-test pass
  on tenant isolation.
- Runbooks: database failover, stuck AI queue, webhook secret rotation, restore.
- **Done when:** a game day covering a killed primary, a lost AZ and a Redis outage keeps the
  SLOs, and the restore drill meets an RTO under 1 h with RPO 0 inside the region.

### Step 8: Private beta ⏳

- Onboard 3–5 internal teams. Collect acceptance data on summaries and friction reports on the
  board.
- **Phase 1 exit criteria:** the SLOs below have held for 2 weeks, no P1 security findings are
  open, and teams keep using the product after the beta week.

## 6.3 Phase 1 SLOs

| Signal | Target |
|---|---|
| API availability | 99.9% during beta, 99.95% at general availability |
| Board read p95 | < 200 ms |
| Move card p95 | < 150 ms |
| Webhook to link visible p95 | < 5 s |
| AI summary, request to result p95 | < 30 s |
| Tenant isolation defects | 0. Every one is a P1. |

## 6.4 After Phase 1

| Phase | Adds |
|---|---|
| **2** | The realtime gateway (live boards, presence, Yjs multiplayer descriptions and wiki). The PQL parser and compiler with saved views. List, Calendar and Timeline views on the normalised store. Sprints with burndown, velocity and CFD. Releases. Custom fields. Workload (time tracking itself was brought forward and built on 2026-10-05; see 07). The automation engine (the merge→Done rule becomes data). Redis and OpenSearch. |
| **3** | AI story-point estimation, auto-assignment and ticket generation on the RAG pipeline (03 §3.4–3.7). GitLab and Bitbucket. The Gantt view with dependencies. SSO/SAML and SCIM. Cells and a warm-standby region. A public API, possibly GraphQL. Kafka for the event bus when throughput calls for it. |

## 6.5 Running it

```bash
cd pmt-api
make setup                       # MySQL today (PostgreSQL after step 1), API and SPA behind nginx on :8080
make verify                      # every gate for both repos

# AI summaries: opt in, then start the worker profile
#   .env: AI_ENABLED=true, and ANTHROPIC_API_KEY=... (read by the worker only)
docker compose --profile ai up -d

# GitHub: set GITHUB_WEBHOOK_SECRET in .env, link a repo in a workspace
# (POST /workspaces/{id}/repositories), and point the repo's webhook at
#   https://<host>/api/integrations/github/webhook   (content type: application/json;
#   events: pull requests)
```

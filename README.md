# Base Project API

A reusable FastAPI + SQLAlchemy 2.0 + MySQL foundation. It provides authentication, users,
roles and permissions, an activity log and health probes, with the security, contract and
tooling gates already wired. Start a product from it by adding route → controller → service
→ model modules.

Pairs with the React SPA in the [`pmt-app`](https://github.com/shakthivel-rc/PMT-APP) repository.

> **PMT — the project-management product built on this base.** This copy adds workspaces,
> the Space › Folder › List hierarchy, issues (Epic › Story / Task / Bug › Subtask — the
> standard convention since 2026-10-05; older trees keep working until they are changed),
> boards with fractional ranking and user-customisable workflows, inbound GitHub (PR linking,
> and a merge action each space configures) and AI thread summaries through a queue and a
> separate worker (`python -m workers.ai_worker`). The restructuring tools — space charters,
> archive and freeze, workflow editing with preview, transfers, and migration plans with
> reconciliation and rollback — are in
> [`docs/design/09-restructuring.md`](docs/design/09-restructuring.md), with a
> [user guide](docs/guides/structure-and-boards.md) and a
> [migration runbook](docs/runbooks/restructuring-migration.md).
> It also has time tracking, sprints and **reports** (timesheets, time by grouping, project,
> sprint and flow reports; CSV, XLSX and PDF exports; saved and scheduled email reports),
> with exports, schedules and nightly snapshots run by `python -m workers.report_worker`
> (the `report-worker` compose service) — see
> [`docs/design/08-reports.md`](docs/design/08-reports.md).
> The design — architecture, data model, AI strategy, API and views, tech stack and the
> Phase 1 plan — is in [`docs/design/`](docs/design/README.md). The identity, RBAC and
> hardening sections below are the base's and still apply unchanged.

---

## What it does

| Area | Detail |
|---|---|
| **Identity** | Login by email *or* username, per-account lockout, revocable server-side sessions, rotating refresh tokens, OTP email verification, invite and password-reset links |
| **Authorization** | Role-based. Permissions attach to roles, resolved through a three-branch check that also honours coarse permission implications |
| **Auditing** | Activity log of user and admin actions |
| **Workspaces & Hierarchy** | 4-tier organization: `Workspace > Space > Folder > List`. Tenant isolation with zero-disclosure (404), governance, container archiving and freezing |
| **Issues & Boards** | Central work items (Epic › Story / Task / Bug › Subtask), Kanban/List boards, Lexorank re-positioning, issue dependencies (`blocks`, `relates`, `duplicates`, `clones`) |
| **Notifications & Inbox** | In-app user notifications for issue assignments and status updates, unread badge counter, individual/bulk read markers |
| **Workflows** | Per-space customizable stages, categories (`todo`, `in_progress`, `done`, `canceled`), transition rules (`any` vs `restricted`), WIP policies (`warn` vs `block`), and GitHub PR merge hooks |
| **Sprints** | Iteration planning, commitment point baseline freezing at start, real-time KPI progress tracking, closing with carry-over to backlog or future sprint |
| **Time Tracking** | Whole-second precision, active stopwatch timer per user, daily/weekly logs, timesheets, lock periods, and workspace visibility settings |
| **Reports** | 15 analytical report types across Time, Sprint, Project, and Flow categories; background CSV/XLSX/PDF generation; saved reports and scheduled email digests |
| **Storage & Files** | Local and cloud storage adapters (AWS S3, Azure Blob Storage) with MCP integration for issue attachments |
| **Integrations** | Inbound GitHub webhooks with HMAC-SHA256 signature verification, automated stage moves on PR merge, deferred delivery queuing for frozen spaces |
| **AI Assistant & Chat** | Conversational agent API, persistent multi-turn conversations, LLM thread summarization, model credentials management, and tools execution |
| **Health** | `/api/v1/health/live`, `/ready`, `/dependencies` (database) |
| **Email** | OTP, invitation and reset mail over SMTP (AWS SES); logged instead of sent when `SMTP_HOST` is unset |
| **Hardening** | Rate limiting, security headers, body-size limit, request ids, JSON logs with secret redaction |

## Stack

Python 3.12 · FastAPI 0.141 · SQLAlchemy 2.0 · Alembic · MySQL 8 (PyMySQL) · PyJWT · bcrypt ·
cryptography (Fernet)

Email is plain `smtplib` against any SMTP endpoint (AWS SES's SMTP interface in production),
in `integrations/aws_ses.py`. The one vendor SDK in `requirements.txt` is `anthropic`, used
only by the AI worker (`ai/client.py`); the API process never imports it
(`tests/unit/test_ai_isolation.py`).

## Quick start

**One command, from a clean machine (Linux, macOS, or Windows):**

```bash
git clone https://github.com/shakthivel-rc/PMT-API.git pmt-api && cd pmt-api && ./setup.sh
```
*(or `make setup` if you have `make` installed).*

*(Note: If you ran `git clone` without specifying `pmt-api`, Git clones to `PMT-API` by default on case-sensitive filesystems; simply run `cd PMT-API && ./setup.sh` — both directory casings are fully supported).*

On **Windows**, run:
```cmd
setup.cmd
```
*(or in PowerShell: `powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1`)*

That clones the SPA beside this repo, writes a `.env` with freshly generated secrets, settles free ports, brings
up the database, runs the migrations, seeds the default superadmin, and starts everything. When it
finishes, it prints the URLs and credentials directly in your terminal:
- **Application:** <http://localhost:8080>
- **API Health:** <http://localhost:8000/api/v1/health/ready>
- **Superadmin:** `superadmin@base-project.local` (password printed at the end of setup).

### Dynamic Auto-Detection & Zero-Friction Fallback

The installer dynamically detects your system's capabilities:
1. **Containerized (Docker, recommended):** Uses Docker and Docker Compose if present.
2. **Missing Docker with Permissions:** Offers to install Docker automatically across Linux (`apt`, `dnf`, `pacman`, `apk`, `zypper`), macOS (`brew`), and Windows (`winget`).
3. **No Docker / No Root Privileges:** Automatically falls back to **Native Mode** (`Python venv + npm`), which runs 100% in user space without requiring root or sudo. If no MySQL server is running, setup dynamically configures an instant embedded SQLite database (`base_project.db`) so the application is immediately usable without any external dependencies.

Re-running `./setup.sh` is safe: it fills in only what is missing and never overwrites a value you already have in `.env`.

### Direct Native Mode (Without Docker)

```bash
./setup.sh --native   # or make setup-native
make dev              # starts both backend and frontend dev servers with hot reload
```

Native mode needs Python 3.12, Node, and a MySQL server you point `DATABASE_URL` at. The
driver is `PyMySQL`, which is pure Python — there is nothing to compile.

### Everything else

`make` on its own lists the rest — `make logs`, `make db-shell`, `make test`, `make reset`.

> Always start the API from this directory. `load_dotenv()` reads the *current working
> directory* and settings are read at **import** time, so a missing required value raises
> before uvicorn binds. The container sets `WORKDIR` for exactly this reason.

See [`.env.example`](.env.example) for every variable. `SECRET_KEY` signs JWTs and encrypts
verify/reset links, so rotating it invalidates every outstanding reset link.

## Tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest
python -m pytest -m unit      # no database required
```

Runs against in-memory SQLite by default, so it needs no infrastructure. Set
`TEST_DATABASE_URL` to point the integration suite at MySQL, as CI does; `LIVE_API_URL`
additionally enables the live-stack suite, which is the only thing that skips. See
[`tests/README.md`](tests/README.md).

## The gates

`make verify` is the one to run before calling work done. It covers both halves of the
product — until 2026-08-26 it entered the SPA and nothing else, which meant the side
holding the credentials was checked by nobody.

| Command | What it runs | Fails when |
|---|---|---|
| `make lint` | ruff, gated by `ruff-suppressions.json` | a finding appears that is not already recorded |
| `make typecheck` | mypy over the modules in `[tool.mypy]` | any of them stops type-checking |
| `make secrets` | the credential scanner over every tracked file | an `.env`, key, token or JWT is committed |
| `make openapi` | regenerates `docs/openapi-digest.txt` | (run it; the test fails if you did not) |
| `make verify-api` | lint + types + pytest + the coverage ratchet | any of the above, or coverage below the floor |
| `make verify-app` | the SPA's typecheck, lint and coverage ratchet | same, on that side |
| `make verify` | **both** | — |
| `make hooks` | installs the pre-commit hooks | — |
| `make audit` | known CVEs in both dependency sets | a new, unwaived advisory in the runtime set |

Every threshold in this repository is a ratchet with its reasoning written next to it:
the coverage floors in `pyproject.toml` and `vitest.config.ts`, the chunk ceilings in
`bundle-budget.json`, the lint ledgers in `ruff-suppressions.json` and
`eslint-suppressions.json`. Raising one is a reviewed commit with the measurement in it.
Lowering one to make a build pass is not a thing that happens here.

CI (`.github/workflows/`) runs these same `make` targets — a CI that can drift from the
local command produces failures nobody can reproduce. The nightly workflow adds the
dependency audits and the live-stack suite.

How the code is written, and why, is [`CLAUDE.md`](CLAUDE.md). How to report a
vulnerability, and what this service is trusted with, is [`SECURITY.md`](SECURITY.md).

## Layout

```
main.py             app assembly and middleware order
core/               settings, JSON logging + secret redaction, middleware, error handlers,
                    permission catalogue, rate limiting
routes/             APIRouters: auth, user, roles, permissions, activity_logs, workspaces,
                    issues, notifications, time_tracking, reports, sprints, workflow, transfer, migrations,
                    dependencies, releases, saved_views, storage, integrations, chat, health
dependencies/       check_permissions (RBAC), workspace_access tenancy gates
controllers/        business logic; owns the transaction boundary (including notification_controller)
services/           domain operations (issues, notifications, sprints, reports, workflows, workspace, etc.)
schemas/            Pydantic request bodies + the response envelope
models/             SQLAlchemy declarative models (User, Issue, Notification, Workspace, etc.)
db/                 engine, session factory, get_db
integrations/       SMTP (AWS SES) transport, GitHub webhook parsing & verification
ai/                 prompts (versioned), model client, thread context reader
workers/            background processors: ai_worker, report_worker
mcp/                Model Context Protocol servers (e.g. cloud_storage)
seeders/            first-boot data: the superadmin, the member account, the default roles
utils/              password hashing and policy, Fernet link tokens, small leaf helpers
alembic/            migrations
scripts/            setup, dev servers, and the gates that are not tests
docs/               API documentation standard, OpenAPI digest, token-storage decision,
                    security exceptions, hardening audit, readiness plan, guides, runbooks
tests/              pytest suite: unit / integration / live
```

Layering runs one direction only: `routes → controllers → services → models`. Controllers
own `commit()`/`rollback()`; services `flush()` but do not commit.

**The session rule**: a `Session` is synchronous and never crosses an `await`. A handler
with no `await` of its own is a plain `def`. An `async def` that must touch the database
does so in a nested `def` handed to `asyncio.to_thread`, as `JWTAuthMiddleware.dispatch`
does. Enforced by `tests/unit/test_no_blocking_db_calls.py`.

A new product feature is a new route module, controller, service, model and schema — plus a
permission slug in `core/permissions.py` if it needs one, and a migration. Nothing in
`core/` should need to change; if it does, that is worth a second look.

Every endpoint returns the same envelope:

```json
{ "status": "...", "status_code": 200, "message": "...", "data": {} }
```

`core/errors.py` installs handlers, so an unhandled exception, an `HTTPException` or a
validation error also leaves in this envelope, carrying `X-Request-ID` and the CORS/CSP
headers.

## Database

Alembic manages the schema: `users`, `roles`, `permissions`, `user_roles`,
`role_permissions`, `user_sessions` (hashed tokens) and `activity_logs`. It starts from a
single squashed baseline revision, `20260821a001`. A database created by the product this
base was split from has a different revision history and must be recreated, not upgraded.

Every revision has a real downgrade, and CI runs `upgrade head` → `downgrade base` against
MySQL. There is no broker, no Redis and no second process: the API and MySQL are the whole
runtime.

## API docs

OpenAPI, Swagger and ReDoc are served when `ENVIRONMENT != prod`, so the SPA and integrators
have a contract:

* <http://localhost:8000/docs> · <http://localhost:8000/redoc> · <http://localhost:8000/openapi.json>

In production they are **absent**, not guarded — `openapi_url` is `None`, so the schema is
never generated and the paths 404. Both halves are asserted by tests.

## Dependency posture

The runtime set is fastapi/starlette, SQLAlchemy, alembic, PyMySQL, pydantic, PyJWT, bcrypt,
cryptography, email-validator, python-dotenv and uvicorn, each pinned in `requirements.txt`.
Nothing at runtime makes an outbound HTTP call, so there is no HTTP client in that set.

The service boots with only `DATABASE_URL` and `SECRET_KEY` set. With no SMTP configured,
mail is logged instead of sent; any optional piece a derived project adds should behave the
same way — degrade one capability at the moment it is used, never at import, and never as a
refusal to start.

## License

Copyright 2026 SHAKTHIVEL RAVICHANDRAN.

Licensed under the [Apache License, Version 2.0](LICENSE). You may use, modify and
redistribute this software, including commercially, provided you retain the copyright
notice, state your changes, and include the [NOTICE](NOTICE) file. The license also
grants an explicit patent licence from every contributor.

Third-party dependency licenses are catalogued in
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md). Every runtime dependency is permissively
licensed; there is no GPL, LGPL, AGPL or SSPL in the tree.

"Base Project" is a project name, not a licensed mark. Apache-2.0 grants no trademark
rights (§6).

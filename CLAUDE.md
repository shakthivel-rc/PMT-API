# CLAUDE.md — how this codebase is written, and why

Three files cite this document by section number and none of them could resolve the
citation, because it did not exist:

| Citation | Where | Section here |
|---|---|---|
| "CLAUDE.md §7, rule 5" | `scripts/bootstrap.sh` | [§7 Identity and secrets](#7-identity-and-secrets) |
| "See CLAUDE.md §14." | `Dockerfile` | [§14 Containers and start-up](#14-containers-and-start-up) |
| "see CLAUDE.md §17" | `Makefile` (`make revision`) | [§17 Migrations](#17-migrations) |

Those three sections are **reconstructed from the code that implements them**, on
2026-08-26, and are marked as such. Where a reconstruction and the original intent differ,
the code is the record and this file is the summary.

Everything else here is the standard as it stands after the hardening pass. It is short on
purpose: rules that are enforced by a mechanism are stated once and pointed at, and rules
nothing enforces are marked as conventions so nobody mistakes them for guarantees.

---

## 0. The one rule the rest follow from

> **A standard nobody can enforce is a preference. A number with no reason is a decoration.**

Every rule below is either (a) enforced by something that fails a build, and says where, or
(b) labelled *convention* — meaning it is what reviewers ask for and nothing checks.

---

## 1. What this is

A FastAPI + SQLAlchemy 2.0 + MySQL base service that provides the cross-cutting foundation
a product is built on. Authentication covers login by email or username, rotating refresh
tokens, revocable server-side sessions stored as hashes, OTP email verification,
invitations, forgot/reset password, lockout and the password policy. It also provides
users, roles and permissions (an RBAC slug catalogue with coarse implications), an activity
log and health probes. It ships with a React SPA in a sibling repository
(`pmt-app`).

Product features are added as new route → controller → service → model/schema modules;
adding one should not require changing `core/`.

## 2. Layout

```
routes/        thin. Path, method, dependencies, and a call into a controller.
controllers/   the business logic and the transaction boundary. Owns commit/rollback.
services/      reusable domain operations shared by controllers.
schemas/       pydantic request/response models.
models/        SQLAlchemy tables.
core/          config, logging + secret redaction, middleware, error handlers,
               permission catalogue, rate limiting.
dependencies/  FastAPI dependencies — check_permissions (RBAC) and workspace_access (tenancy).
db/            engine, session factory, `get_db`.
integrations/  transports — SMTP email (aws_ses.py); GitHub webhook verification and
               parsing (github.py), pure functions.
ai/            prompts (versioned), the model client, and reading a thread's context.
               Only the worker imports ai/client.py; the API process never calls a model.
workers/       long-running processes beside the API — the AI worker (ai_worker) and
               the report & export worker (report_worker).
mcp/           Model Context Protocol servers (e.g. cloud_storage).
utils/         small leaf helpers with no project imports.
alembic/       migrations. One revision per schema change.
seeders/       first-boot data (superadmin, member, default roles).
scripts/       setup, and the gates that are not tests.
tests/         pytest: unit / integration / live.
```

A route that contains business logic, or a controller that formats HTTP responses by hand
instead of returning `SuccessResponse`/`ErrorResponse`, is the two most common drifts.
*Convention.*

## 3. Comments

This repository explains **why**, at length, and the explanations are the most valuable
thing in it. `bundle-budget.json`, `pyproject.toml`, `.gitignore` and most module
docstrings carry the reasoning behind a decision, including what was tried and rejected.

- Write the reason a number is that number, next to the number.
- When you change a decision, extend its comment with the new measurement and the date.
  Do not replace the history; it is why the next person will not repeat the mistake.
- Never delete an explanatory comment because it is long.
- Do not write comments that restate the code.

*Convention, and the one most worth defending in review.*

## 4. Python style

**Enforced.** `make lint` runs ruff and then `scripts/lint_ledger.py`.

- Config: `[tool.ruff]` in `pyproject.toml`. Line length **100**.
- Existing findings are recorded per file in `ruff-suppressions.json`. **A count may fall
  or stay flat; a build that raises one fails.**
- Fix a finding, then `python scripts/lint_ledger.py --update` and read the diff. A number
  going *up* in that diff means the commit is wrong, not that the ledger is stale.
- Prefer fixing to suppressing. If a rule is genuinely wrong for this codebase, narrow it
  in `pyproject.toml` with a comment saying why — that is a reviewed change; a scattered
  `# noqa` is not.
- **No formatter.** `ruff format` is deliberately not wired in: the comments here are
  hand-wrapped prose and a formatter destroys them. Reformatting a file you are editing is
  not a courtesy, it is a diff nobody can review.

## 5. Types

**Enforced.** `make typecheck` runs mypy over the modules listed in `[tool.mypy]`:
`core/config.py`, `core/redact.py`, `utils/`, `schemas/`, and since 2026-09-29
`ai/prompts.py`, `ai/client.py`, `integrations/github.py` and `dependencies/workspace.py`.
Those are green and must stay green.

The list is scoped rather than repo-wide because a repo-wide run on partly-untyped code
produces a number so large that the first person to see it turns the check off. **The list
is meant to grow** — adding a module is one line plus whatever it takes to make it pass,
which is the right size for a piece of work someone can finish.

## 6. Tests

**Enforced.** `make test-api`; coverage floor in `[tool.coverage.report]`.

- Markers: `unit` (no database), `integration` (needs one — SQLite locally, MySQL in CI),
  `live` (needs a running stack; skipped unless `LIVE_API_URL` is set).
- The suite runs on in-memory SQLite by default. It must keep working with **zero**
  infrastructure; that property is why people run it.
- Coverage is a ratchet, set just under the measured value of the `-m unit` subset. Raising
  it is a commit with the measurement in the comment. Lowering it is not a thing that
  happens.
- A bug fix without a test that fails before it is not a bug fix.
- Tests that assert on behaviour, not on implementation. The suite is full of docstrings
  explaining what a test would catch — write those; they are what makes a red test
  actionable at 2 a.m.

## 7. Identity and secrets

*Reconstructed 2026-08-26 from `utils/password.py`, `core/config.py`, `utils/token.py`,
`models/user_session.py` and `scripts/bootstrap.sh`.*

1. **Passwords are hashed with bcrypt**, directly (`utils/password.hash_password` /
   `verify_password`). Never stored in plain text, never logged, never returned by an
   endpoint.
2. **`SECRET_KEY` is the one key.** It signs JWTs and is the Fernet key for verify/reset
   links (`utils/token.py`), so rotating it invalidates every outstanding link. Production
   refuses a short or low-entropy value, and `ALGORITHM` is checked against an allow-list in
   every environment (`core/config.py`).
3. **Third-party credentials, when a derived project adds them, are encrypted at rest with a
   key separate from `SECRET_KEY`.** `SECRET_KEY` is rotated whenever a JWT secret should
   be; a stored credential encrypted under it would become unreadable at every routine
   rotation, which is exactly the pressure that stops people rotating. Prefer revocable,
   least-privilege tokens over passwords for the same reason. *Convention: the base stores
   no third-party credential.*
4. **Anything unguessable comes from `secrets`, never `random`.** OTPs, verification and
   reset codes. `random` is the Mersenne Twister and its state is recoverable from its
   output; for a second authentication factor that is the whole attack.
   (`controllers/auth_controller.py:_generate_otp` was `random.randint` until 2026-08-26.)
5. **The password policy**, enforced on every entry point by
   `utils/password.validate_password_policy`: at least `PASSWORD_MIN_LENGTH` characters
   (default 12), with at least one uppercase letter, one lowercase letter, one digit and
   one non-alphanumeric character, and **no whitespace**. This is the rule
   `scripts/bootstrap.sh` cites when it generates `SUPERADMIN_PASSWORD`: a bare random hex
   string has no uppercase and no symbol, so the seeder would reject it, which is why the
   generated value is padded rather than raw.
6. **No secret is ever committed.** `.gitignore` ignores every `.env` sibling;
   `scripts/check_staged_secrets.py` refuses a commit that stages one, or a line matching a
   Fernet key, JWT, private key or basic-auth URL. Install it with `make hooks`. CI runs it
   over the whole tree.
7. **No secret reaches a log.** `core/logging.SecretRedactingFilter` sits on the root
   handler and runs the redactor (`core/redact.py`) over every record's message, structured
   fields and traceback, whether or not the code that logged it knew the redactor exists. It
   also masks the site's literal secrets listed in `LOG_EXTRA_SECRETS`. **Enforced:**
   `tests/unit/test_log_redaction.py`, and `tests/integration/test_secret_canary.py`, which
   submits a password through login and change-password and asserts it appears in no row of
   any table and in no log record.
8. **Session tokens are stored as SHA-256 digests, never raw**
   (`models/user_session.hash_session_token`; `access_token_hash`, `refresh_token_hash`;
   revision `20260821a001`). Every authenticated request checks a live `user_sessions` row,
   so logout and revocation take effect immediately. **Enforced:**
   `tests/unit/test_session_token_hashing.py`.

## 8. The API surface

- Every response is `SuccessResponse` or `ErrorResponse` (`schemas/response.py`).
- 404, not 403, for a resource that exists but is not yours: confirming which ids exist is
  a free disclosure.
- Error messages are written for the person who will read them, and never quote a
  connection string, a token or a stack frame.
- Adding a route means adding it to the contract digest — see §15.

## 9. Async and the database

**Enforced.** `tests/unit/test_no_blocking_db_calls.py`.

SQLAlchemy here is synchronous. Therefore:

> **A database call may block the thread it is on, so it must never be on the event loop.**

- A handler that does no `await` of its own: make it a plain `def`. Starlette runs it in a
  threadpool.
- A handler that genuinely interleaves database and network work: keep it `async def`, put
  the database work in a nested `def`, and `await asyncio.to_thread(...)` it — the way
  `JWTAuthMiddleware.dispatch` (`routes/auth_middleware.py`) does.
- The same applies to expensive pure-CPU work (bcrypt, large regex passes): off the loop,
  not on it.
- The check is an AST walk and **does not follow calls**. An `async def` that calls a
  synchronous helper which queries is invisible to it — that blind spot is how the session
  lookup in `JWTAuthMiddleware.dispatch` survived the first pass. When you find one, fix it
  the same way, which also brings it inside the check.
- The escape hatch must be *taken*, not merely present: the gate records which nested
  functions are handed to `asyncio.to_thread` and exempts only those. A nested `def` nobody
  offloads runs on the event loop like any other call.

## 10. Security posture

- **Authentication is by default and exemption is by list.** `JWTAuthMiddleware`
  authenticates everything not in `UNAUTHENTICATED_ROUTES`/`UNAUTHENTICATED_PREFIXES`.
  **Enforced:** `tests/unit/test_route_inventory.py` fails unless every exempt route is
  written down in its `PUBLIC` map with a sentence saying why, and asserts that every other
  route answers 401 to an anonymous caller.
- **Middleware branches on `request.scope["path"]`, never on `request.url.path`.**
  Starlette builds `request.url` by re-parsing `f"{scheme}://{host}{path}"` with the
  **Host header**, which the caller controls, while the router dispatches on
  `scope["path"]`. Reading the wrong one was a complete authentication bypass —
  `Host: x/api/v1/health` made `GET /users` answer **200 with no token** — and the same
  mistake, copied into two new middlewares, also removed the rate limit and let a caller
  drop the CSP header. One bug, three symptoms. **Enforced:**
  `test_route_inventory.py::TestPathConfusion`.
- **A justification in the `PUBLIC` map must be true, not merely present.** The test checks
  its length; only a reviewer checks its content. An entry once claimed a request-signature
  check that did not exist anywhere in the service. Write what the code does.
- **Middleware order is load-bearing** and documented in `main.py`. CORS is outermost so a
  401, 403 or 429 carries `Access-Control-Allow-Origin` and the SPA can read it.
- **Inbound rate limiting** (`core/ratelimit.py`) refuses before authentication. In-process
  and therefore per-worker; that limitation is written down rather than pretended away.
- **Security headers** are set by the API for its own port. "The reverse proxy does it" is
  only true of the origin the reverse proxy serves.

## 11. Authorization (RBAC)

Users hold roles; roles carry permissions; a permission is a slug.

- **The slug catalogue is `core/permissions.TARGET_PERMISSION_CATALOG`**, seeded by
  `seeders/user_seeder.py`. All component permissions across PMT (workspaces, spaces, folders,
  lists, issues, sprints, workflows, initiatives, time tracking, reports, releases, ai,
  storage, integrations, poker, timeline, notifications) are declared here.
- **Colon-bearing slugs (`domain:action`)**: Always declare them in `TARGET_PERMISSION_CATALOG`.
  Custom permissions can also be created via UI with optional machine names; `POST /permissions`
  preserves colon-bearing identifiers.
- **Coarse grants stand in for sets of fine slugs.** `manage_users`, `manage_workspaces`,
  `manage_issues`, `manage_sprints`, `manage_initiatives`, `manage_reports`, `manage_time`,
  and `manage_workflows` imply their domain slugs through `LEGACY_PERMISSION_IMPLICATIONS`,
  resolved by `permission_is_satisfied`.
- **Routes declare `Depends(check_permissions([...]))`** from `dependencies/auth.py`, and a
  refusal there must be *raised*, never returned: a dependency that returns an error hands
  it to the handler as the resolved value and the handler runs anyway. **Enforced:**
  `tests/unit/test_authz_fail_closed.py`.
- **`permission_slugs` is the role → permission traversal.** Do not write another one;
  authorization logic that exists twice eventually disagrees with itself.
- **Soft-deleted users and roles grant nothing.** **Enforced:**
  `tests/integration/test_soft_delete_authz.py`.
- A missing permission is a 403. A resource that exists but is not the caller's is a 404,
  not a 403 (§8).
- **Default Roles**:
  - `Admin`: holds every catalogue slug plus `manage_users`.
  - `Member`: holds nothing by default (least-privileged role for self-registered users).
  - `Project Manager`: management of workspaces, spaces, issues, sprints, workflows, initiatives,
    approvals, and reports.
  - `Product Owner`: backlog, sprint management, roadmap, initiatives, and issue tracking.
  - `Developer`: issues execution, time logging, releases, integrations, and AI chat.
  - `QA Engineer`: issue lifecycle testing, reporting, time logging, and releases.
  - `Stakeholder`: read-only visibility into workspaces, issues, sprints, timeline, and reports.
  - `Finance Manager`: time tracking management, timesheet approvals, and financial reports.

### Developer Guide: Adding Permissions for New Components

When creating a new component, module, or feature in PMT, follow this checklist:

1. **Define the Slug in Backend**:
   Add `("Component Action", "component:action")` to `TARGET_PERMISSION_CATALOG` in
   `core/permissions.py`. If it falls under an existing coarse capability, add it to
   `LEGACY_PERMISSION_IMPLICATIONS`.
2. **Map to Default Roles**:
   Add the slug to the appropriate role tuples in `DEFAULT_ROLE_PERMISSIONS` in
   `seeders/user_seeder.py`.
3. **Database Migration**:
   Add an idempotent Alembic data migration (or include in the feature migration) that checks
   if the permission exists in `permissions`, inserts it if absent, and links it in
   `role_permissions` to `Admin` and relevant default roles.
4. **Protect API Routes**:
   In `routes/<feature>.py`, add `dependencies=[Depends(check_permissions(["component:action"]))]`
   or pass `Depends(check_permissions([...]))` to route handler parameters.
5. **Protect UI Presentation**:
   In `pmt-app`, import `hasPermission` from `@/types/index` and wrap the UI controls:
   `hasPermission(currentUser, ["component:action"])`. The `/rbac` administration screen
   provides search filtering and assignment modals for all registered permissions.

## 12. Configuration

- One settings object, `core/config.get_settings()`, `lru_cache`d and read at **import**
  time. A missing required value raises before uvicorn starts.
- Every setting is documented in `.env.example` with what it does and what happens at the
  extremes. A setting that is not in `.env.example` does not exist as far as an operator is
  concerned.
- Defaults are safe: anything that sends mail or calls an external service defaults to
  **off**. With no `SMTP_HOST`, email is logged, not sent — and the invite and reset links
  in the logged copy are masked by the redactor, so those flows need an SMTP server (a local
  mail catcher will do; `integrations/README.md`).

## 13. Logging and observability

- JSON to stdout (`core/logging.py`). Structured fields go in `extra={"extra_fields": {...}}`.
- Every request gets an `X-Request-ID` (minted or echoed) and it is returned on the
  response and exposed cross-origin so a user can quote it in a support request.
- Log a line for every state transition an operator would grep for, and
  do not log high-frequency chatter at INFO (per-row, per-iteration).
- `/api/v1/health/live` is up, `/ready` is serving, `/dependencies` reports database state
  and answers 200 even when degraded (`X-Health-Degraded: true`), because a diagnostic that
  cannot answer is the least useful thing it could be.

## 14. Containers and start-up

*Reconstructed 2026-08-26 from `Dockerfile`, `docker-compose.yml` and
`scripts/docker-entrypoint.sh`.*

1. **`WORKDIR /app` is load-bearing, not cosmetic.** `load_dotenv()` reads the *current
   working directory* and `get_settings()` runs at import time, so starting the process
   from anywhere else makes it abort before uvicorn binds. This is the citation in the
   Dockerfile.
2. Two stages, so no compiler reaches the running image. The builder produces a virtualenv;
   the runtime copies it and nothing else.
3. **Runs as a non-root user** (uid 10001). The application writes nothing to disk; all
   state lives in the database.
4. `.dockerignore` is committed and matters: without it, `COPY . .` bakes the developer's
   real `SECRET_KEY` into the image and `docker history` shows it to anyone who pulls.
   **Enforced:** the `image` job in `.github/workflows/ci.yml` builds the image and fails
   if any `.env` is inside it.
5. The healthcheck hits `/api/v1/health/ready` — serving, not merely started.
6. `pip install` uses `--retries 10 --timeout 60`. One dropped connection anywhere in the
   pinned set otherwise fails the build with a message that names a package and a pin and
   sends the reader to PyPI to check a version that was never the problem.
7. **apt fetches over HTTPS**, in both stages (added 2026-10-01): a `sed` switches
   `debian.sources` to `https://deb.debian.org` before `apt-get update`. The base image
   ships `http://`, and a firewall that inspects cleartext downloads answers some `.deb`
   requests with its own block page — apt then exits 100 with
   `403  Forbidden [IP: 151.101.x.x 80]`, which names Debian's CDN and was never Debian.
   **Enforced:** `tests/unit/test_dockerfile_apt.py`.

## 15. The contract with the SPA

The SPA is a separate git repository and hand-maintains every response shape in
`src/types/index.ts`. Nothing structurally connects the two, so:

**Enforced.** `docs/openapi-digest.txt` is a one-line-per-operation digest of
`app.openapi()`. `tests/unit/test_openapi_contract.py` fails when it and the app disagree.

Change an endpoint → run `make openapi` → commit the digest in the **same commit** → check
whether `src/types/index.ts` needs the same change. The gate is that the update is
deliberate, not that it is hard.

The emailed links are the other half of the contract, and the digest cannot see them: they
are SPA routes, not API routes. `utils/token.py` builds `/reset-password?token=…` and
`/verify-account?token=…`, the SPA's `src/App.tsx` routes both and reads `?token=`, and the
SPA posts the token back in the JSON body — never the URL (SEC-4). **Enforced where the SPA
is checked out beside the API** (a `make setup` tree, and CI's `tests-sqlite` job, which
fetches it): `tests/integration/test_emailed_links.py` follows each link out of the email
and back into the API, and fails when `src/App.tsx` no longer declares the route or reads the
token. With no SPA beside it, that one check skips and says so.

## 16. The gates

| Command | What it runs |
|---|---|
| `make lint` | ruff + the ledger |
| `make typecheck` | mypy over the scoped modules |
| `make secrets` | the credential scanner over every tracked file |
| `make openapi` | regenerate the contract digest |
| `make verify-api` | lint + types + tests + coverage ratchet |
| `make verify-app` | the SPA's typecheck + lint + coverage ratchet |
| `make verify` | both halves — the gate before calling work done |
| `make hooks` | install the pre-commit hooks |
| `make audit` | known CVEs in both dependency sets; fails on a new, unwaived runtime advisory (`docs/security-exceptions.md`) |

CI (`.github/workflows/ci.yml`) runs the same `make` targets for the API. The SPA has no
workflow yet (`docs/hardening-audit.md` F-07), so its half of `make verify` — `make
verify-app`, i.e. `npm run verify` — is a local gate only. If CI and the local command can
disagree, the local one stops being trusted and the CI failure stops being reproducible.

## 17. Migrations

*Reconstructed 2026-08-26 from `alembic/`, `Makefile` and `scripts/docker-entrypoint.sh`.*

1. **Read every autogenerated revision before applying it.** This is the citation in
   `make revision`. Autogenerate is a first draft: it routinely emits a `drop_column` for a
   column it cannot see, reorders constraints, and misses server defaults and index names.
2. **One revision per schema change**, with a message that says what changed and why.
3. **Every revision has a real `downgrade`.** Up is what the container runs on start; down
   is what nobody runs until the night they need it, which is the wrong night to find out it
   is `pass`. **Enforced:** CI runs `alembic upgrade head` then `alembic downgrade base`
   against MySQL on every push.
4. **Use `batch_alter_table`** for column alterations, so the same revision works on SQLite
   (the test harness) and MySQL (production).
5. **Mind the two MySQL divergences**: utf8mb4 makes indexed `VARCHAR` length limits real,
   and MySQL's default `REPEATABLE READ` hides other connections' commits — see the note in
   `tests/conftest.py` for why the harness sets `READ COMMITTED`.
6. The container applies migrations on start (`scripts/docker-entrypoint.sh`). A revision
   that cannot be applied unattended is a revision that takes the service down.

## 18. What "done" means

- `make verify` is green.
- The change has a test that fails without it.
- Every new number has a comment saying where it came from.
- No gate was relaxed to get here.
- `docs/openapi-digest.txt` and `ruff-suppressions.json` were regenerated in the same
  commit if the change moved them, and their diffs were read.
- The commit message says *why*, not what the diff already says.

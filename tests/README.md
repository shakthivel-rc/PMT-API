# Base Project API test suite

Automated tests for the FastAPI backend. Built on **pytest + httpx (FastAPI's `TestClient`
and the live suite) + pytest-cov**, configured in [`../pyproject.toml`](../pyproject.toml)
and [`conftest.py`](conftest.py).

## How to run

```bash
cd pmt-api
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

python -m pytest -m unit   # pure/unit tests only — no database, runs anywhere
python -m pytest           # full suite (unit + integration; live tests skip)
python -m pytest --cov     # with coverage, gated by fail_under in pyproject.toml
```

Use `python -m pytest`, not bare `pytest` — the project root is not on `sys.path`
otherwise, and every import in the suite is absolute.

By default the suite runs against a throwaway **in-memory SQLite** database, so it needs
zero infrastructure. Set `TEST_DATABASE_URL` to run the integration suite against real
MySQL:

```bash
TEST_DATABASE_URL=mysql+pymysql://root:root@127.0.0.1:3306/base_project_test python -m pytest
```

`LIVE_API_URL` additionally enables `tests/live/`, which drives a running stack over
HTTP — nginx, the proxy, the containers. Without it those tests skip. The only other skip
is the SPA-route half of `integration/test_emailed_links.py`, which reads
`../pmt-app/src/App.tsx` and skips when the SPA is not checked out beside this
repository. The authenticated live tests also need `SUPERADMIN_PASSWORD`, the
value in the stack's `.env`, and sign in as `SUPERADMIN_EMAIL` (default
`superadmin@base-project.local`), the same variable the seeder reads. The suite makes far
more than the shipped 10 credential calls a minute from one address, so the stack under
test needs a larger `RATE_LIMIT_CREDENTIAL_PER_MINUTE` in its `.env` — the nightly job sets
1000 — or the tests after the tenth sign-in fail on a 429.

## The harness ([conftest.py](conftest.py))

The one non-obvious piece. Environment variables are set **before any app import**, because
`core/config.py`, `db/session.py` and `utils/token.py` all read `os.environ` at import time
(`utils/token.py` even builds `Fernet(SECRET_KEY)` at import — so the test `SECRET_KEY` is a
valid Fernet key).

The test engine (SQLite `StaticPool`, or MySQL from `TEST_DATABASE_URL`) is then swapped
into `db.session` and into the JWT middleware, which opens its own session per request and
so holds its own `SessionLocal` reference. `Base.metadata.create_all` builds the schema. The
app has no lifespan today, and the `TestClient` is used **without** a `with` block so that
a startup hook a derived project adds never runs under the suite: a test sees only the
request it made.

**`PRAGMA foreign_keys=ON`** is switched on for every SQLite connection. SQLite does not
enforce foreign keys unless asked, which makes an unconfigured test database *more permissive
than production* — the worst direction for it to differ in. Turning it on found a real defect
the first time it was switched on: a delete that MySQL refuses with error 1451, because a
child table was never cleared, passed locally.

An autouse fixture clears every table after each test.

## What is covered

| Area | File | Why it's a priority |
|---|---|---|
| Password policy | `unit/test_password.py` | Security rule; mirrors the frontend validator |
| One password primitive | `unit/test_password_unification.py` | One hashing path, a byte-measured maximum (SEC-7) |
| RBAC resolution | `unit/test_permissions.py` | 3-branch resolver; a bug = silent privilege change |
| Authorization fails closed | `unit/test_authz_fail_closed.py` | A dependency that *returns* a refusal lets the handler run |
| Config helpers + prod guards | `unit/test_config.py` | Startup-critical parsing |
| Settings reach the process | `unit/test_settings_delivery.py` | `core/config.py`, `.env.example` and compose must agree |
| Fernet token crypto | `unit/test_token.py` | Verify/reset-link encryption, and `app_link`, which builds the emailed URLs |
| Session token hashing | `unit/test_session_token_hashing.py` | `user_sessions` holds digests, never tokens (SEC-5, P-1) |
| OTP generator + commit scanner | `unit/test_secret_hygiene.py` | `secrets`, not `random`; no credential committed |
| Secret redaction | `unit/test_redact.py` | The patterns every log record is run through |
| Log redaction | `unit/test_log_redaction.py` | The root-handler filter, for loggers that never heard of it |
| Route inventory + path confusion | `unit/test_route_inventory.py` | Every non-exempt route answers 401 anonymously; `Host` cannot move a path |
| Rate limiting | `unit/test_rate_limit.py` | The only place the limiter's behaviour is pinned |
| Security headers + middleware order | `unit/test_security_headers.py` | A 401 or 429 still carries CORS and CSP |
| Request-body ceiling + prod guards | `unit/test_body_limit.py` | 413 before anything buffers the body, chunked or not |
| Error handlers | `unit/test_error_handlers.py` | Every error leaves in the envelope, with the headers |
| No blocking DB on the event loop | `unit/test_no_blocking_db_calls.py` | A sync query in `async def` stalls every request |
| OpenAPI contract | `unit/test_openapi_contract.py` | The digest the SPA's hand-written types are checked against |
| Docs references | `unit/test_docs_references.py` | A cited `CLAUDE.md §N` must exist |
| Setup scripts | `unit/test_setup_scripts.py` | Runs where nothing else works yet, so nothing else can diagnose it |
| Health endpoints | `integration/test_health.py` | The only machine-checkable assertions |
| JWT middleware | `integration/test_auth_middleware.py` | Allow-list, decode, session lookup, revocation |
| Login + lockout | `integration/test_auth_login.py` | Lockout arithmetic (5×401; the lock is asserted in the row, not the status — SEC-6), counter reset |
| Login disclosure | `integration/test_auth_enumeration.py` | One answer whatever the account state (SEC-6); no credential in the session row (SEC-5) |
| Route authorization | `integration/test_permissions_endpoint.py` | 403 when the slug is missing |
| Soft-delete authorization | `integration/test_soft_delete_authz.py` | Deleted users and roles grant nothing |
| User creation | `integration/test_user_creation.py` | The admin console's primary write path |
| Emailed links | `integration/test_emailed_links.py` | Follows the reset and invite links out of the email and back into the API; tokens only in the body; the invite window in UTC; the paths are a contract with the SPA |
| User listing cost | `integration/test_user_listing_queries.py` | Query count must not grow with the number of users |
| Profile email | `integration/test_user_profile_email.py` | A stored `.local` address must not 500 the profile |
| Secret canary | `integration/test_secret_canary.py` | A submitted password reaches no table and no log record |
| Degraded boot | `integration/test_degraded_boot.py` | Boots with only `DATABASE_URL` + `SECRET_KEY`; OpenAPI absent in prod |
| Migration chain | `integration/test_migrations.py` | up → down → up, and models vs migrations |
| The deployed stack | `live/` | nginx, the proxy and real MySQL; only with `LIVE_API_URL` |

## What is intentionally NOT covered (and why)

- **SMTP / email** (`integrations/aws_ses.py`): no fake SMTP server wired up yet.
- **MySQL-specific behaviour, on the default run.** The migration chain runs on SQLite here
  and on MySQL in CI, but **SQLite ignores `VARCHAR` limits** — an overflow MySQL raises as
  error 1406 (surfacing as an unhandled 500) passes silently. Tests that care about a length
  assert it directly rather than relying on the database to complain. Point
  `TEST_DATABASE_URL` at a real MySQL to close the rest of the gap.

### Two divergences worth knowing, because both hid real behaviour

**MySQL `DATETIME` has second granularity and rounds on write.** A row created at
`12:15:19.7` is stored as `12:15:20` — half a second in the *future* of the `now()` a
request computes moments later. `created_at <= now()` then silently omits the most recent
row: any query bounded by a synthesised `now()` misses a row created a moment before it
ran. SQLite keeps the microseconds, so the bug is invisible locally. Two consequences the
suite encodes: no query uses a synthesised `now()` as an upper bound, and no test asserts
ordering between two rows written in the same second — ordering by `created_at` is only
defined across seconds, not within one.

**MySQL defaults to REPEATABLE READ.** A transaction's first read pins a snapshot. The
harness therefore opens its engine with `READ COMMITTED` (see `conftest.py`) so an inspecting
session sees what a request handler has since committed. Without it, tests fail with "the
endpoint did not write it" for rows that are demonstrably there. Production is unaffected —
a request opens a session, works, and closes it.

## Reports

`python -m pytest --cov` prints the terminal report and fails below the floor in
`pyproject.toml`. Add `--cov-report=xml` for `coverage.xml` or `--cov-report=html` for
`htmlcov/`; CI writes `coverage.xml` and uploads it as a workflow artifact.

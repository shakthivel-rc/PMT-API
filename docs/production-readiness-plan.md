# Production readiness plan — 2026-08-27

The work between "a rigorous codebase with a green suite" and "a service I would put in
front of real users". It is written the way `docs/hardening-audit.md` is: every item
carries a file and a line, every number was measured on 2026-08-27 rather than estimated,
and anything reasoned-but-not-reproduced says so.

**Scope.** This project was split out of a larger product after this plan was written.
Items that existed only because of that product's domain (SEC-4, P-3, P-6, parts of P-5,
SEC-10 and SEC-12) are reduced to stubs or trimmed to what still applies. **IDs are not
renumbered**, because code and other documents cite them, and the measurements below are
historical — they describe the original product on 2026-08-27, not this tree.

**This plan does not repeat the hardening pass.** The F-numbered findings in
`docs/hardening-audit.md` are fixed and pinned by tests; nothing below re-litigates them.
What follows is the set of things that pass left open, the things it created, and the things
it never looked at — plus the one workstream it did not touch at all, which is the API's own
documentation.

**Read the sequencing note before the phases.** Phase 2 is the largest single body of work
in this document and it is deliberately *not* first.

---

## Baseline, measured 2026-08-27

| | Measured | Where |
|---|---|---|
| Production Python | 28,129 lines, 118 modules | `find . -name '*.py'` excluding tests |
| Test Python | 13,885 lines, **1,082 tests collected** | `pytest --collect-only` |
| Unit suite | **554 passed in 5.25 s**, zero infrastructure | `pytest tests/unit` |
| Coverage floor | 48% (measured 73.6% full / 49.3% unit) | `[tool.coverage.report]` |
| Lint debt | **786 suppressed findings across 111 files** | `ruff-suppressions.json` |
| Type coverage | **6 of 118 modules** checked | `[tool.mypy] files` |
| Docstrings | **35% of functions, 52% of modules, 16% of schema classes** | AST walk, below |
| API operations | **109 routes** | `grep '@router\.' routes/` |
| …with a `response_model` | **7** | `grep -c response_model routes/` |
| …with a `summary` or `responses` | **0** | ditto |
| Schema field descriptions | **0**, against 123 `Field(...)` calls | `grep description= schemas/` |
| Runtime dependency advisories | **31 across 9 packages** | `pip-audit -r requirements.txt` |
| SPA dependency advisories | **0** | `npm audit` |

Docstring coverage by package, so the Phase 4 work list is not a guess:

```
package           funcs   doc     %    classes  doc     %
controllers         226    91   40%          0    0    0%
core                 30    10   33%          8    5   62%
routes              119    33   27%          2    2  100%
schemas               2     1   50%         53    9   16%
services             34    10   29%          0    0    0%
```

`core/` is the part a reader finds well explained. `routes/` and `schemas/` are the parts an
*integrator* reads, and they are the two worst — which is the same finding as the
API-documentation gap, seen from the other side.

---

## Sequencing

Six phases. The order is risk-reduction per engineer-day, with one exception noted.

| Phase | What | Effort | Blocks |
|---|---|---|---|
| **1** | Close the identity holes | 1.5–2 d | Everything. Do not ship without it. |
| **2** | Make the API self-describing | 4–5 d | The SPA type generator, the public contract |
| **3** | The performance items the audit left open | 2–3 d | Load testing |
| **4** | Standards, types, comments | 2–3 d | Nothing — but it is what stops phases 1–3 rotting |
| **5** | Operations and supply chain | 1.5–2 d | Production sign-off |
| **6** | SPA follow-through | 1–2 d | Depends on Phase 2 |

Total: **12–17 engineer-days**, which assumes one person and the existing gates staying
green throughout.

**Phase 2 is second, not first, on purpose.** It is the biggest item and it is the one the
brief asked for first, but it touches every route signature. Doing it before Phase 1
means the identity fixes land as conflicts inside a diff that spans every route module, and
the review that matters least swamps the review that matters most.

**Before Phase 1 starts:** the API working tree has ~30 modified files uncommitted (the
hardening pass). Commit or stash that first. A plan that starts by editing on top of an
unreviewed diff cannot report what it changed.

---

# Phase 1 — Close the identity holes

Eight items. Six are confirmed by reading the code; two are reasoned. Every one is in the
authentication or authorization path, which is why this phase is first.

### SEC-1 — an authorization dependency that returns instead of raising · **high** · CONFIRMED

`dependencies/auth.py:22-26`

```python
user = getattr(request.state, "user", None)
if not user:
    return ErrorResponse(status="error", status_code=401, ...)   # <-- returns
```

A FastAPI dependency that *returns* a value injects it and lets the handler run. Every
other refusal in the same function raises `HTTPException`; this one does not. The effect,
if reached, is that an unauthenticated caller runs the handler with an `ErrorResponse`
object bound where the `User` should be.

**Reachable today?** No — `JWTAuthMiddleware` populates `request.state.user` for everything
outside `UNAUTHENTICATED_ROUTES`, and no exempt route declares `check_permissions`
(verified). **The finding is that the fail-open exists, not that it is open.** One route
registration under an exempt prefix makes it live, and the person adding that route has no
reason to read this file. This is the same shape as F-02.

**Fix.** `raise HTTPException(401, ...)`, identical to the branch six lines below it.
**Proof.** Extend `tests/unit/test_route_inventory.py` with a route that is both exempt and
permission-guarded, and assert 401.

### SEC-2 — the authorization lookup is the one that does not filter soft deletes · **medium** · CONFIRMED

`services/user.py:16`

```python
def get_user_by_id(db: Session, user_id: str):
    return db.query(User).filter(User.id == user_id).first()      # no deleted filter
```

Compare `get_user_by_username` (`:14`), `get_user_by_email` (`:73`) and `fetch_user`
(`:104`), which all carry `User.deleted != 1`. `get_user_by_id` is the one
`check_permissions` calls, so it is the lookup made on **every authorized request**.

**Is it exploitable today? No — and the reason matters.** `DELETE /users/{id}`
(`controllers/user_controller.py:157-177`) does the right thing: it calls
`delete_user_session` at `:160` and `user_role_deletion` at `:162` before setting
`deleted = 1`. The sessions are gone, so `assert_session_is_live` fails and the token 401s
before `check_permissions` is ever reached; and the roles are gone, so there would be
nothing to grant anyway. **The delete path is thorough. That is not the finding.**

The finding is that the *authorization* layer's safety is entirely delegated to the
*deletion* layer remembering to do three things in the right order. Any other route to
`deleted = 1` — a bulk operation, a migration, a support query, a second delete endpoint
written later — reinstates a live session against a deleted account, and the one lookup
positioned to catch that is the one lookup that does not check. Every sibling function
checks; this one is the exception, and nothing says why.

**Fix.** Add the filter. It is one clause, it costs nothing, and it makes the guarantee
structural rather than procedural.
**Proof.** A unit test that sets `deleted = 1` directly and asserts `get_user_by_id`
returns `None` — deliberately bypassing the delete endpoint, because bypassing it is the
scenario.

### SEC-3 — a soft-deleted role still grants its permissions · **medium** · CONFIRMED

`models/user.py:36`, `models/roles.py:22-23`, `dependencies/auth.py:9`

```python
roles = relationship("Role", secondary="user_roles", back_populates="users")
permissions = relationship("Permission", secondary="role_permissions", ...)
```

Neither relationship filters `deleted`. `permission_slugs` walks both, so a role that was
soft-deleted from the RBAC screen keeps granting every permission it held to every user it
was assigned to.

**Fix.** Either a `primaryjoin` that excludes soft-deleted rows, or — better — delete the
`user_roles` rows when a role is soft-deleted, so the grant is gone rather than filtered.
The second is preferable: a filter is one place to forget, an absent row is nowhere.

### SEC-4 — a credential-bearing path segment in the request log · removed with the domain

The only route whose path carried a credential belonged to the domain this base was split
from, so the finding and its fix (a path-masking step in `RequestContextMiddleware`) went
with it. The rule it taught stands: a credential in a URL path is also a credential in every
access log, every proxy log and every `Referer` header. Put it in a header or a body.

### SEC-5 — live session tokens are stored in plaintext · **medium** · CONFIRMED

`models/user_session.py:15-16`

```python
access_token  = Column(String(1024), nullable=False)          # raw
refresh_token = Column(String(1024), nullable=True)           # raw
refresh_token_hash = Column(String(128), nullable=True, index=True)
```

The refresh token is stored **both** hashed and raw. The access token is stored raw only.
A read of `user_sessions` — a backup, a replica, a support query, a SQL injection anywhere
in the service — is a set of tokens that authenticate immediately, without the password.

The hash column already exists and already works, so the pattern is established; the access
token simply never adopted it.

**Fix.** Store `access_token_hash` (SHA-256, `CHAR(64)`, indexed), drop `refresh_token`'s
raw column, look up by hash. **This is also P-1 in Phase 3** — the same change removes the
full table scan. Do them as one migration.

### SEC-6 — account enumeration, by message and by timing · **medium** · CONFIRMED

`controllers/auth_controller.py:83-115`

Three distinguishable answers to a sign-in attempt:

* unknown account → 401 "Invalid username or password", returned **without calling bcrypt**
* known account, unverified → 401 "Verify your account to login"
* known account, locked → 423 "Account is temporarily locked"

The messages disclose which addresses have accounts. The missing bcrypt call discloses the
same thing to anyone with a stopwatch — the known-account path spends ~100 ms in
`checkpw`, the unknown-account path returns in microseconds.

The rate limiter (F-05, 10/min on credential paths) bounds how fast this can be walked; it
does not stop it, and the timing oracle needs one request per address.

**Fix.** One message and one status for every credential failure, and a dummy `checkpw`
against a fixed hash on the unknown-account path so both branches cost the same. Keep the
distinction in the *log line*, where an operator can see it and an attacker cannot.
**Note the trade honestly:** "verify your account" is genuinely useful to a real user. The
usual resolution is to send it by email rather than in the response.

### SEC-7 — two password implementations, and no maximum length · **medium** · CONFIRMED

`utils/password.py:6`, `controllers/auth_controller.py:56`, `seeders/user_seeder.py:18`

Hashing goes through passlib's `CryptContext`; verification calls `bcrypt.checkpw`
directly; the seeder constructs a third `CryptContext` of its own. One primitive, three
call sites, two libraries.

`validate_password_policy` (`utils/password.py:16`) enforces a *minimum* length and no
maximum. bcrypt truncates at 72 bytes, and passlib and bcrypt 4.x disagree about what to do
past it — passlib truncates silently, `bcrypt.hashpw` raises. So a 100-character password
is 72 bytes strong and the two halves of the system reach that state by different routes.

There is a third consequence: passlib 1.7.4 imports the stdlib `crypt` module, **removed in
Python 3.13**. It is the reason this service cannot move off 3.12.

**Fix.** Drop passlib. `bcrypt` is already a direct dependency and is already used for
verification. One `hash_password`/`verify_password` pair in `utils/password.py`, called
from everywhere including the seeder. Add a maximum length (72 bytes, stated in the policy
error). Existing hashes are bcrypt either way, so no re-hash is needed.

### SEC-8 — an unhandled exception escapes the envelope, CORS and the CSP · **medium** · CONFIRMED (by reading)

`main.py` — there is **no `@app.exception_handler`** anywhere in the service.

An exception that reaches the top is handled by Starlette's `ServerErrorMiddleware`, which
sits *outside* everything `main.py` adds. The 500 it returns therefore carries no
`Access-Control-Allow-Origin`, no `Content-Security-Policy`, no `X-Request-ID`, and is
`text/plain`, not a `SuccessResponse`/`ErrorResponse` envelope.

This is F-03's exact failure mode, still live for the 500 class: in a browser the SPA
cannot read the response, so a server error and a network outage are indistinguishable to
it. `tests/live/test_auth_live.py:268` already records one route to it —
`verify_password(None, hash)` raises inside passlib — and notes the absence of a handler.

**Fix.** Three handlers in `main.py`: `Exception` → 500 envelope with the request id and
nothing else; `HTTPException` → envelope; `RequestValidationError` → envelope, so a 422
looks like every other refusal. Log the traceback, return the request id.
**This is also Phase 2 work**: those three handlers are what make the documented error
shapes true.

### SEC-9 — 31 known advisories in the pinned runtime set · **high** · CONFIRMED

`requirements.txt`, measured with `pip-audit` on 2026-08-27:

```
Found 31 known vulnerabilities in 9 packages
pyjwt            2.10.1   6 advisories   (fixes at 2.12.0 / 2.12.1 / 2.13.0)
starlette        0.46.0   7 advisories
python-multipart 0.0.20   6 advisories
cryptography     44.0.2   6 advisories
click / h11 / mako / python-dotenv / idna   1 each
```

`docs/hardening-audit.md` listed this under "Not covered" with the instruction to run
`make audit` before the next deployment. It has now been run, and **PyJWT is the library
that authenticates every request in this service.** The SPA, by contrast, is clean:
`npm audit` reports 0.

**Fix.** Upgrade in one commit, run the suite, then flip `nightly.yml` to gating — which
`docs/hardening-audit.md` already describes as a one-line change. Anything that cannot be
upgraded goes in `docs/security-exceptions.md` with a reason and a date, not left silent.
**This is the single highest value-per-hour item in the plan.** Do it in Phase 1 even
though it is listed under supply chain.

### SEC-10 — no request body size limit · **low** · REASONED, NOT REPRODUCED

Nothing in `main.py` or the middleware stack bounds a request body, and any endpoint that
accepts a JSON body will take whatever it is sent. Starlette buffers the body before the
handler sees it. The rate limiter bounds request
*count*, not size.

**Fix.** A `ContentLengthLimitMiddleware` refusing above a configured ceiling, plus the
same limit in `nginx.conf` (`client_max_body_size`) so it is refused before it is read.

### SEC-11 — log redaction is behavioural, not structural · **low**

Redaction was applied only where a caller remembered to call it; nothing structural stopped
a `logger.info(secret)` added tomorrow in a different module. SEC-4 was an instance of
exactly that gap.

**Fix.** A `logging.Filter` on the root handler in `core/logging.py` that runs the redactor
(`core/redact.py`) over the formatted message and over `extra_fields`. Costs one pass per log record; makes
the guarantee hold for code nobody has written yet.

### SEC-12 — production config guards are partial · **low**

`core/config.py:414-417` refuses `CORS_ORIGINS=*` in production. It does not check:

* `SECRET_KEY` length or entropy — a deployment can ship with `"changeme"`;
* `ALGORITHM` against an allow-list — `none` is a legal value of that variable today;
* `DATABASE_URL` pointing at SQLite in production;
* that `SUPERADMIN_PASSWORD` was changed from the seeded default.

**Fix.** Four assertions next to the one that exists, each with the reason at the check.

---

# Phase 2 — Make the API self-describing

**This is the workstream the brief asked for, and the measurement is stark.**
`docs/openapi-digest.txt` — the repository's own generated contract — reported `200:any` for
**102 of 109 operations** in the original product. Re-measured on this tree on 2026-09-28 it
is **27 of 34**. The document is structurally complete and semantically empty:

```
GET /users                    | get_users_users_get             | … | 200:any,422:HTTPValidationError | body:-
GET /roles                    | get_roles_roles_get             | … | 200:any,422:HTTPValidationError | body:-
```

The seven exceptions declare `Union[SuccessResponse, ErrorResponse]`, whose `data` field is
`Any` (`schemas/response.py:12`) — so they describe an envelope and no payload.

The consequence is already paid for, in the SPA: `src/types/index.ts` is hand-written,
mirroring shapes that were read out of controllers rather than generated from a
contract. `tests/unit/test_openapi_contract.py` exists to make that drift *visible*; it
cannot make it *impossible*, because there is nothing to generate from.

### 2.1 — A generic envelope · 0.5 d

`schemas/response.py` becomes generic:

```python
DataT = TypeVar("DataT")

class SuccessResponse(BaseModel, Generic[DataT]):
    status: Literal["success"] = "success"
    status_code: int
    message: str
    data: DataT
```

`SuccessResponse[UserDetail]` then produces a real schema. `ErrorResponse` keeps
`data: Any` — its payload genuinely varies (the password endpoints' 400 `{errors: [...]}`,
a 422's `data.errors`, `request_id` on a 500; see the `ErrorResponse` docstring), and the
refusals that carry actionable content there get their own declared shapes in `responses=`.

Pydantic 2 renders generics into OpenAPI correctly; the digest will grow, which is the
point. Do this first — everything else in the phase depends on it.

### 2.2 — Response models for every operation · 2–2.5 d

The mechanical bulk of the phase. For each route:

* a response Pydantic model in `schemas/`, named for the resource, not the endpoint;
* `response_model=SuccessResponse[Thing]` on the decorator;
* `status_code=` where it is not 200 (no route sets one today; every route inherits 200).

**Derive the models from the SPA, not from the controllers.** `src/types/index.ts` already
holds the shapes as the only consumer actually uses them. Porting those into
Pydantic is faster than reading every controller, and it makes the two agree by
construction rather than by review.

Work in groups so each is a reviewable commit: identity (auth, users, roles, permissions,
activity logs) and health.

### 2.3 — Declared error responses · 0.5 d

Zero routes declare `responses=`. Every one of them can answer 401, 403, 429 and 500, and
none of that is in the schema. Add a shared constant:

```python
COMMON_ERRORS = {
    401: {"model": ErrorResponse, "description": "No token, an expired token, or a revoked session."},
    403: {"model": ErrorResponse, "description": "Authenticated, but the permission is not granted."},
    429: {"model": ErrorResponse, "description": "Rate limited. `Retry-After` says how long."},
    500: {"model": ErrorResponse, "description": "Unhandled server error. Quote `X-Request-ID`."},
}
```

applied per-router. Routes with their own refusals (a 404 for an unknown id, the 423
lockout) add theirs on top; a product that adds owner-scoped records adds its 404-not-403
answer (CLAUDE.md §8) on those routes, not to the shared 403.

**These become true only when SEC-8 lands** — today a 500 is `text/plain`. Documented error
shapes that the service does not actually produce are worse than none.

### 2.4 — Field descriptions and examples · 1 d

**123 `Field(...)` calls, 0 with `description=`, 0 with an example.** 53 schema classes,
9 with a docstring (16%).

A Pydantic field description is the *only* per-field documentation an integrator gets.
Every field in `schemas/` gets one sentence saying what it means and what the extremes do —
the standard CLAUDE.md §3 already sets for numbers in code, applied to the contract.

Each request model gets a realistic `json_schema_extra={"examples": [...]}` so Swagger's
"Try it out" is pre-filled with something that works. Use values from the test fixtures,
so an example that stops working fails a test.

### 2.5 — Route metadata · 0.5 d

Zero routes carry `summary`. Add per route:

* `summary` — one line, imperative, what the caller gets;
* `description` — the handler docstring, which several routes already have and which
  FastAPI already publishes; write the missing ones (routes are 27% documented);
* `operation_id` — explicit and stable. The generated ones are unusable
  (`delete_permission_permissions__permission_id__delete`) and they are what a generated
  client names its methods;
* `tags` with descriptions — `openapi_tags` in `main.py`, one paragraph per tag, which is
  the top-level orientation ReDoc renders and this API currently has none of.

Give the app a `description` and a `version` too. Both are absent from the `FastAPI(...)`
call.

### 2.6 — Generate the SPA's types · 0.5 d

Once 2.1–2.4 land, `src/types/index.ts` is derivable. Add `npm run api:types` running
`openapi-typescript` against `docs/openapi.json`, committed alongside the digest. The
hand-written file shrinks to the genuinely client-side types.

This converts CLAUDE.md §15's *"check whether `src/types/index.ts` needs the same change"*
from a question a reviewer must remember to ask into a file that regenerates.

### 2.7 — Documentation that survives production · 0.5 d

`/docs` is absent in production by design (`main.py`, and correctly — see F-06's reasoning).
That is right for the live port and wrong for the integrator, who then has no contract at
all.

* `make openapi` also writes `docs/openapi.json`;
* a CI step renders it to a static `docs/api/index.html` (ReDoc or Scalar, vendored — the
  SPA's CSP forbids a CDN);
* `README.md` §"API docs" points there for production, and at `/docs` for everything else.

### 2.8 — A gate, so this does not decay · 0.5 d

The pattern this repository already uses for every other standard: a check that fails.

`tests/unit/test_openapi_documented.py` — for every route in `app.routes`:

* a `response_model` that is not bare `Any`;
* a non-empty `summary`;
* an explicit `operation_id`;
* `401` declared unless the route is in the `PUBLIC` map of `test_route_inventory.py`.

Ratchet it the way `ruff-suppressions.json` works: a ledger of the routes not yet done,
which may shrink and never grow. That way 2.2 can land in five commits instead of one.

---

# Phase 3 — The performance items the audit left open

### P-1 — the highest-frequency query in the service is a full table scan · **high**

`routes/auth_middleware.py:80`, `models/user_session.py:15`

```python
db.query(UserSession).filter(UserSession.access_token == token, ...)
```

against `access_token VARCHAR(1024)`, **unindexed**. Every authenticated request runs it.
The module's own comment says so and calls it out as a known cost.

MySQL cannot index a 1024-character utf8mb4 column usefully at all (the 3072-byte key
limit — CLAUDE.md §17.5), so the column cannot simply have an index added. The fix is the
one SEC-5 already requires: an indexed `access_token_hash CHAR(64)`, looked up instead.
**One migration closes a security finding and the worst query in the service.**

Expected shape of the win: O(rows) → O(log rows) on the request path every user pays on
every call. Measure before and after with `EXPLAIN` on a seeded table and put the number in
the commit message.

### P-2 — three-to-N queries per request in `check_permissions` · **medium**

`dependencies/auth.py:20-38`, `services/user.py:16`

Every permission-guarded request: one session lookup (P-1), one user load, then
`permission_slugs` lazy-loads `user.roles` and, per role, `role.permissions`. `fetch_user`
at `services/user.py:100` already solves this with `selectinload` and says why in a comment
— the per-request path never adopted it.

**Fix.** `selectinload(User.roles).selectinload(Role.permissions)` in `get_user_by_id`.
Consider a short-TTL per-request cache: `check_permissions` is a factory, so a route with
two permission checks pays twice.

### P-3 — removed with the domain (no outbound calls hold a session)

The item was a request-scoped session held open across slow outbound network calls. Nothing
in this base makes an outbound call while it holds a session, so it does not apply. The rule
it came from stands in CLAUDE.md §9: database work goes to a thread, and a session never
crosses a slow `await`. A derived project that adds an outbound integration should know the
trap the original hit: releasing the session with `db.rollback()` expires every loaded row,
so the next attribute access queries *on the event loop*. Copy or detach the rows first.

### P-4 — pagination reports the page size as the total · **medium** · CONFIRMED

`services/user.py:141` and `:144`

```python
users = query.order_by(order_by).offset(skip).limit(limit).all()
total_users = len(users)          # the page, not the total
```

Both branches do this. Any client paginating on `total` gets the wrong page count. Fix with
a `select(func.count())` on the filtered query before the limit is applied, and check the
other list endpoints for the same shape.

### P-5 — the N+1 and index review nobody has done · **medium**

Named in the audit's "Not covered". `services/user.fetch_user` already uses `selectinload`
for `GET /users`, and `tests/integration/test_user_listing_queries.py` pins that one
endpoint's query count. Nobody has walked the rest of the list endpoints — roles,
permissions, activity logs.

**Method that fits this repository:** a cursor-execute listener counting statements
per request, and a test that asserts a per-endpoint ceiling — the same shape as
`test_user_listing_queries.py`, and as `test_no_blocking_db_calls.py`, which is a gate the
team already trusts. That turns "we did an N+1 sweep once" into "an N+1 fails the build".

Pair it with an index review: `users`, `user_sessions` and `activity_logs` are the tables
the list endpoints and the authentication path filter and sort on.

### P-6 — `ASYNC109` × 14 · **closed by removal**

All 14 suppressed findings (`async def` taking its own `timeout` parameter instead of using
`asyncio.timeout`) were in the domain modules this base was split from. The advice stands
for new code: a hand-rolled timeout parameter usually means the timeout is applied at one
call and not at the ones underneath it.

---

# Phase 4 — Standards, types and comments

### 4.1 — the mechanical 80% of the lint ledger · 0.5 d

786 findings in the original product on 2026-08-27. On this tree the live count is
whatever `ruff-suppressions.json` records (`make lint` prints it); it only goes down. The
2026-08-27 split by rule:

| Rule | Count | Nature |
|---|---|---|
| `E501` line length | 199 | manual, but trivial |
| `UP045` `Optional[X]` → `X \| None` | 146 | **auto-fixable** |
| `UP017` `timezone.utc` → `UTC` | 97 | **auto-fixable** |
| `I001` import order | 65 | **auto-fixable** |
| `W293`/`W291`/`W292` whitespace | 119 | **auto-fixable** |
| `RUF100` unused `noqa` | 21 | **auto-fixable** |
| `UP035`/`UP037`/`UP006`/`UP007` typing | 55 | **auto-fixable** |
| `S108` hardcoded `/tmp` | 16 | read each |
| `ASYNC109` | 14 | P-6 above |
| everything else | ~54 | read each |

Roughly **626 of 786 are auto-fixable**. One `ruff check --fix` (plus a reviewed
`--unsafe-fixes` pass), the diff read, `lint_ledger.py --update`, one commit. That takes the
ledger down by four-fifths and leaves a backlog small enough that the remaining entries get
read rather than scrolled past.

**Respect CLAUDE.md §4:** `ruff format` stays unwired. `--fix` on the selected rules does
not reflow the hand-wrapped comment prose; a formatter would. Verify that on one file
before running it on 111.

### 4.2 — grow `[tool.mypy]` to the request path · 1.5 d

Currently: `core/config.py`, `core/redact.py`, `utils/`, `schemas/`. The config comment says
the list is meant to grow and that adding a module is the right size for a piece of work
someone can finish.

Order, by consequence-of-a-type-error:

1. `core/` (the rest) and `db/` — small, and everything imports them
2. `dependencies/`, `routes/` — the request path; **and Phase 2 has just typed every
   response**, which is what makes these tractable
3. `services/` — mostly untyped signatures
4. `models/` — needs `Mapped[]`/`mapped_column` to be worth anything; do it with 4.4
5. `controllers/` — the largest layer, last, and possibly never at full strictness

Add each with `disallow_untyped_defs` off first, on second. One module per commit.

### 4.3 — docstrings where a reader needs them · 1 d

The baseline table sets the priority order:

* **`schemas/` — 16% of classes, and they are the public contract.** Phase 2.4 covers this.
* **`routes/` — 27%, and FastAPI publishes route docstrings into OpenAPI.** Phase 2.5.
* **`services/` — 29%, no class docstrings at all, and this is the reusable layer.** Not
  covered elsewhere; do it here.
* **`controllers/` — 40% across 226 functions.** Do the exported entry points; skip the
  private helpers whose names are already the explanation.
* **Module docstrings.** These are the cheapest and the most valuable — a module docstring
  is what tells the next reader whether they are in the right file.

CLAUDE.md §3 is explicit that comments explain *why* and never restate the code. A
docstring pass that adds `"""Get the user."""` above `get_user` makes the codebase worse.
Where a function needs no explanation, leave it alone and record that the number is
deliberately not 100%.

### 4.4 — the remaining consistency debt · 0.5 d

* **`routes/auth.py` is unmodernised scaffold** — single-line imports of twelve names,
  `Response,Request` unspaced, no blank-line convention, no docstrings, no
  `response_model`. It is the oldest file in the repository and the only one that does not
  look like the rest of it. It is also, being the authentication surface, the first file an
  auditor opens.
* **`datetime.utcnow()` at `controllers/auth_controller.py:99, 172, 173, 331`**, alongside
  an aware `_utcnow()` at `:33` used elsewhere in the same file. Naive and aware datetimes
  compared against each other is a bug waiting for a timezone. Deprecated in 3.12.
* **`services/user.py:89`** — `ErrorResponse(..., message="Error Occured : {e}")` inside
  `except Exception:` with no `as e`. An unformatted f-string, a typo, and an unbound name
  in one line. It is also a *service* returning a `JSONResponse`, which CLAUDE.md §2 names
  as one of the two most common drifts.
* **`assign_role_for_user` (`services/user.py:78`)** deletes every existing role before
  assigning one, against a many-to-many schema. Either the schema is wrong or the function
  is; decide and write it down.
* **96 `JSONResponse` constructions and 0 `HTTPException`s** in `controllers/`. Returning
  responses works, but it is why FastAPI cannot see the error shapes (Phase 2.3) and why
  return annotations like `-> SuccessResponse` at `auth_controller.py:83` are untrue. Pick
  one — raising is the one that documents itself — and note it in CLAUDE.md §8.
* **`models/` uses SQLAlchemy 1.x `Column`** throughout, on SQLAlchemy 2.0.38. `Mapped[]` +
  `mapped_column` is what makes 4.2 step 4 possible.

---

# Phase 5 — Operations and supply chain

* **SEC-9 (31 advisories) — but do it in Phase 1.** Then flip `nightly.yml` to gating,
  which the audit already describes as a one-line change, and open
  `docs/security-exceptions.md` for anything that cannot move.
* **Correlate the logs.** `X-Request-ID` is minted, returned, and exposed cross-origin —
  and appears in exactly one log line (`core/middleware.py:89-101`). Put it in a `ContextVar`
  and have `JsonFormatter` read it, so every record from every module carries it. Without
  that, the correlation id correlates one line with itself.
* **SEC-11 (the redaction filter).**
* **Per-module coverage floors** for `core/`, `dependencies/`, `routes/auth_middleware.py`
  and `utils/token.py`. Proposed in the last plan, never implemented; the global floor
  moved 40 → 48 instead, and a global floor lets a security module rot behind a
  well-covered one.
* **Close the gap between unit-only and full-suite coverage**, which the audit names twice
  as the highest-value hour available: the identity services, with tests that need no
  database.
* **The rate limiter is per-worker** (`core/ratelimit.py`, documented, not hidden). N
  workers means N times the limit. At one worker it is exact; before scaling out, move the
  counters to Redis or accept and document the multiplier at the chosen worker count.
* **Metrics.** There is no `/metrics`. Request count and latency by route, authentication
  failures, lockouts and rate-limit refusals are all computable; add a Prometheus client
  and a few counters.
* **The MySQL path has never run.** Everything was verified on SQLite; the MySQL CI job is
  written and unexecuted. Run it before sign-off — CLAUDE.md §17.5 names two real
  divergences.
* **The skipped live tests** and the Playwright/axe suites are wired and unexecuted.
  Same instruction: run them, then believe them.

---

# Phase 6 — SPA follow-through

* **Generated types** (Phase 2.6) — the largest item, and it deletes code.
* ~~**No timeout on `fetch`**~~ — RESOLVED 2026-08-27. Every call through the SPA client is
  bounded by `DEFAULT_TIMEOUT_MS` in `src/api/client.ts`, applied by `resolveTimeoutMs`, so a
  request against a hung API settles and the spinner stops.
* **`ApiResponse<T = any>`** (`src/api/client.ts`, `ApiResponse`) — the default that makes the whole
  client untyped when a caller forgets the parameter. After 2.6 there is no reason for a
  default at all.
* **S-01, tokens in `localStorage`** — documented in `SECURITY.md`, unchanged, and correctly
  so: httpOnly cookies need CSRF protection and that is a design change. Make it a decision
  with a date rather than a standing entry: either accept it in writing for this release, or
  schedule it.
* **S-06, the bundle ceiling had 0.2 kB of headroom** (299.8 / 300.0 kB). *(Resolved
  2026-09-28: with the original product's domain screens removed, every ceiling was
  re-measured and lowered — total 248.3 / 260.0 kB, initial JS 193.0 / 198.0 kB; see
  `bundle-budget.json`'s `_comment`.)* The rule it stated still holds: decide trim or raise
  with a reason before a feature branch goes red, because a ceiling that drifts up on its own
  measures nothing.

---

## Deliberately not in this plan

Recorded so the absence reads as a decision.

* **Moving to async SQLAlchemy.** `db/session.py` states why the synchronous engine is
  deliberate and what follows from it. The rule it implies is enforced by a gate. Changing
  it is a rewrite of every handler, and this plan is not where that decision gets made
  quietly.
* **Splitting `SECRET_KEY`'s two jobs** (S-03). Still true, still a migration rather than a
  hardening edit, still documented in `.env.example`.
* **`allow_credentials=False`** (S-02). Still no test that would prove the change safe, and
  the origin list is restricted.

---

## What "done" means for this plan

Beyond CLAUDE.md §18, which still applies to every commit in it:

1. `make verify` green, and `make audit` **gating** rather than reporting.
2. `docs/openapi-digest.txt` contains no `200:any`.
3. `src/types/index.ts` is generated, and the generator runs in CI.
4. `tests/unit/test_openapi_documented.py` passes with an empty ledger.
5. Every Phase 1 item has a test that fails without its fix.
6. `[tool.mypy] files` covers `core/`, `db/`, `dependencies/`, `routes/`, `schemas/`,
   `services/` and `utils/`.
7. `ruff-suppressions.json` is under 200 findings.
8. The MySQL job, the live tests and the Playwright suite have all been **run**, not
   merely wired.
9. Every measurement in this document has been re-taken, and the new number is in the
   commit that moved it.

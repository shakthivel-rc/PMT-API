from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.config import get_settings
from core.errors import UnhandledExceptionMiddleware, install_error_handlers
from core.logging import configure_logging
from core.middleware import (
    BodySizeLimitMiddleware,
    RateLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from routes import (
    activity_logs,
    ai_agents,
    auth,
    chat,
    dependencies,
    deployments,
    health,
    initiatives,
    integrations,
    issues,
    migrations,
    notifications,
    permissions,
    poker,
    pql,
    releases,
    reports,
    roles,
    saved_views,
    sprints,
    storage,
    time_tracking,
    timeline,
    transfer,
    user,
    workflow,
    workspaces,
)
from routes.auth_middleware import JWTAuthMiddleware


settings = get_settings()
# `LOG_EXTRA_SECRETS`, the literal values a site knows are secret, is handed to the logging
# filter: a log line is one of the places they surface, and no pattern could recognise them
# there. See `core/logging.py`.
configure_logging(settings.log_level, settings.log_extra_secrets)


#: The version of the contract, and the one place it is written down.
#:
#: **Not** a setting. `APP_NAME` is overridable by environment because a display name is a
#: property of the deployment; a version is a property of the code that shipped, and an
#: operator who can set it can make `/openapi.json` claim a version this binary is not.
#: **Not** `pyproject.toml` either: that file says at the top that it deliberately carries
#: no `[project]` table because this repository is a service, not an installable package,
#: and adding one to hold a string would change what the file is. **Not** `git describe`
#: at import: `get_settings()` runs at import time and the runtime image contains no
#: `.git` (`.dockerignore`, CLAUDE.md §14 item 4), so it would fail at boot rather than at
#: build.
#:
#: So: a literal, next to the only thing that reads it, bumped by hand in the commit that
#: moves the contract — the same discipline `docs/openapi-digest.txt` already asks for
#: (CLAUDE.md §15). 0.1.0 is what FastAPI has been defaulting to all along; declaring it
#: explicitly changes nothing served today and gives the number somewhere to be raised
#: from. If a second consumer ever needs it — a `/health/version`, an image label — move
#: it to a non-environment constant in `core/config.py` and import it in both places
#: rather than copying it.
API_VERSION = "0.1.0"

#: The orientation ReDoc renders above the route list, and the first thing an integrator
#: who has just opened `/docs` reads. Markdown; keep it to what someone needs *before*
#: their first call, and leave the per-endpoint detail to the endpoints.
API_DESCRIPTION = """\
The Base Project API: a reusable FastAPI + SQLAlchemy foundation with sign-in, server-side
sessions, users, roles, permissions, an audit log and health probes, hardened once so a
product built on it does not have to start from there. It pairs with the React SPA in the
`pmt-app` repository.

## Before your first call

* **Every response is the same envelope**: `{"status", "status_code", "message", "data"}`.
  A 2xx carries `SuccessResponse` and everything else carries `ErrorResponse`; `message` is
  written to be shown to a person, and `data` is the payload — or, on some refusals, the
  actionable part of the refusal. There is no third shape.
* **Authenticate with `Authorization: Bearer <access token>`.** Get the pair from
  `POST /user/login`, exchange the refresh token at `POST /user/refresh`. Sessions are
  server-side and revocable, so a token can stop working before its own expiry — treat a
  401 as "refresh, then sign in again", not as "the token was malformed".
* **No record here is scoped to an owner.** The user, role and permission reads answer any
  signed-in caller (see the Users tag), so a 404 from them is about the id, not about you.
  A 403 is always a statement about a missing permission. A product that adds records
  owned by one account answers 404, not 403, for someone else's: confirming which ids
  exist would be a free disclosure.
* **429 is real and it is per client address, per minute.** `Retry-After` carries the
  seconds to wait; the credential endpoints have a much smaller budget than everything else.
* **Quote `X-Request-ID`.** Every response carries one, minted or echoed from the request,
  and it is exposed cross-origin. On a 500 the same value is in `data.request_id`, and it is
  the key the traceback was logged under — it is what makes a bug report actionable.

## Where this documentation is

`/docs`, `/redoc` and `/openapi.json` are served in every environment except production,
where they are absent rather than protected. For a production deployment the contract lives
in the repository: `docs/openapi-digest.txt`, one line per operation, regenerated in the
same commit as any change to it.
"""

#: One entry per tag actually in use, in the order a reader should meet them rather than
#: the order the routers happen to be registered in: authenticate, then administer the
#: people, then the probes. ReDoc renders this list as its sidebar and its top-level
#: narrative, and the tag strings must match the routers exactly — a mismatch does not
#: error, it silently produces a new, empty section.
OPENAPI_TAGS: list[dict[str, str]] = [
    {
        "name": "Authentication",
        "description": (
            "Sign in, refresh, sign out, and the self-service flows around a password: "
            "registration, OTP verification, invite links and password reset. Every route "
            "here except `POST /user/logout` needs no token — with the health probes and, "
            "outside production, these docs they are the whole unauthenticated surface — "
            "and those token-less routes are the "
            "ones the inbound rate limiter gives the small per-minute budget to: a client "
            "that retries a failed login in a loop meets a 429 long before it would on any "
            "other surface. "
            "`POST /user/login` returns an access token and a refresh token; send the "
            "access token on everything else and exchange the refresh token at "
            "`POST /user/refresh`. Sessions are stored server-side and can be revoked, so a "
            "token can stop working before it expires."
        ),
    },
    {
        "name": "Users",
        "description": (
            "Accounts — create, list, read, update, soft-delete — plus the two routes a "
            "caller uses on themselves: `GET /users/profile` and `POST /users/change-"
            "password`. Creating, updating and deleting an account requires "
            "`manage_users`. Listing accounts and reading one by id currently need only "
            "authentication, so any signed-in account — a self-registered `Member` "
            "included — can read the user directory; a derived project that needs it "
            "private adds the check, rather than relying on this as a contract. A delete "
            "is a soft delete that also ends the user's sessions and removes their role "
            "assignments, so it takes effect on their next request rather than at their "
            "next token expiry. No endpoint in this group ever returns a password hash."
        ),
    },
    {
        "name": "Roles",
        "description": (
            "Roles are the only thing that carries a permission: a user holds permissions "
            "by holding a role and by no other route. An account with no roles can still "
            "sign in, read its own profile, change its own password, read its own audit "
            "entries, and use the reads that ask only for a token — the user directory and "
            "the role and permission catalogues — but nothing that declares a permission. "
            "Creating, editing and deleting a role needs "
            "`manage_users`; reading the catalogue does not, because any screen that draws "
            "a role picker has to be able to list what exists. Deleting a role does not "
            "delete the users who held it — it takes their grants away."
        ),
    },
    {
        "name": "Permissions",
        "description": (
            "The catalogue of permission slugs that can be attached to a role, and the CRUD "
            "over it. Reading it needs only authentication, as for roles; creating, "
            "editing and deleting a permission needs `manage_users`. Slugs are matched by "
            "`core/permissions.permission_is_satisfied`, "
            "which honours coarse implications — a caller holding `manage_users` "
            "satisfies a route asking for `users:read` — so the set a user effectively "
            "holds is wider than the set literally attached to their roles. Read that "
            "function before inventing a slug: here the naming *is* the mechanism, not a "
            "convention on top of one."
        ),
    },
    {
        "name": "Activity Logs",
        "description": (
            "The audit trail of user and admin actions, paginated and filterable by actor "
            "and by action. The one endpoint scopes itself to the "
            "caller: a holder of `manage_users` reads the whole log and everybody else "
            "reads only their own entries, so an empty page means \"nothing of yours\" "
            "rather than \"nothing at all\" and is not a reason to retry."
        ),
    },
    {
        "name": "Workspaces",
        "description": (
            "The tenant and everything that organises work inside it: workspaces, their "
            "members, spaces (each with its issue-key prefix and workflow), folders and "
            "lists, and the repositories whose pull requests feed it. **Every route under "
            "`/workspaces/{workspace_id}` answers 404 to anyone who is not a member**, "
            "exactly as if the workspace did not exist — a 403 would confirm the id is a "
            "real tenant. Inside a workspace, `guest` reads, `member` also writes issues, "
            "`admin` also changes structure and membership, and `owner` is an admin who can "
            "grant `owner`. A space is created from a workflow template and, when the "
            "workspace's governance settings require it, a creation checklist (owner, "
            "delivery method, objective, success measure, why no existing space will do). "
            "Spaces, folders and lists can be **archived** (read-only for good, history "
            "kept) and a space can be **frozen** (read-only while it is reorganised): every "
            "write inside one answers 423 with a message saying what lifts it. A space key "
            "can change; the old key keeps resolving and stays reserved."
        ),
    },
    {
        "name": "Issues",
        "description": (
            "Issues, the board, comments and the AI summary of a thread. The board and the "
            "list view return the same `IssueCard`, grouped or paged — switching views in a "
            "client is a regrouping of data it already holds. Moving a card rewrites one "
            "row whatever the length of its column. A summary request never waits for the "
            "model: it answers 202 with a job to poll, and reuses the last answer when "
            "nothing in the thread has changed. The board draws a list, a whole space or a "
            "sprint, with each stage's work in progress counted for the team; a move obeys "
            "the space's workflow (a restricted one names where the issue can go) and its "
            "WIP policy. Each space has one ordered backlog to plan from. Every "
            "relationship follows the standard hierarchy (Epic > Story/Task/Bug > Subtask); "
            "an issue key — current, or one the issue or its space used to have — resolves "
            "through `/issue-keys/{key}`."
        ),
    },
    {
        "name": "Workflow",
        "description": (
            "Each space's workflow, customised by the people allowed to: stages with a "
            "reporting category (`todo`, `in_progress`, `done`, `canceled` — only `done` "
            "is delivered), colours, WIP flags and limits; free or restricted transitions; "
            "the initial stage; and what a merged pull request does. Changes are previewed "
            "(every problem has a stable code) and published for the whole space at once; "
            "removing a stage that holds issues needs a replacement and a confirmed count, "
            "and nothing is moved, completed or deleted silently. Templates are copied "
            "into a space, never linked."
        ),
    },
    {
        "name": "Sprints",
        "description": (
            "Sprints per space, at most one active. Starting one freezes its commitment and "
            "closing it freezes what was completed; canceled work is closed but never "
            "counted as completed, and unfinished work returns to the backlog or a chosen "
            "planned sprint."
        ),
    },
    {
        "name": "Dependencies",
        "description": (
            "Typed links between issues — blocks, relates to, duplicates, clones — read "
            "from either end, across spaces. A parent never crosses spaces; a dependency "
            "is how cross-space work is coordinated."
        ),
    },
    {
        "name": "Releases",
        "description": (
            "A space's releases and each issue's fix versions. Progress and release notes "
            "are computed from the issues on every read, so canceled work is never counted "
            "as delivered."
        ),
    },
    {
        "name": "Views",
        "description": (
            "Saved board filters, private to their owner or shared with the workspace. A "
            "view is a way of looking at issues; it never changes where they live."
        ),
    },
    {
        "name": "Time tracking",
        "description": (
            "Time logged against issues, the one timer each person can have running, and "
            "the timesheet views over both. Durations are whole seconds everywhere; nothing "
            "is rounded on the server. Totals are summed from the entries on every read, so "
            "logging time never changes an issue's `version`. Members log and edit their "
            "own time; admins can log for others and edit anyone's; whether members see "
            "each other's entries is the workspace's `visibility` setting. `/timer` is "
            "yours, not a workspace's: it answers `null` when nothing is running."
        ),
    },
    {
        "name": "Integrations",
        "description": (
            "Inbound webhooks. `POST /integrations/github/webhook` needs no token — GitHub "
            "cannot hold one — and instead must carry an `X-Hub-Signature-256` HMAC of its "
            "raw body under the deployment's `GITHUB_WEBHOOK_SECRET`. With no secret "
            "configured, every delivery is refused. A merged pull request does what its "
            "space's merge action says (move to a chosen stage, or only link); a move the "
            "workflow cannot make is recorded as a failure on the issue, never redirected. "
            "A delivery naming an issue in a frozen space is deferred, listed for admins "
            "under `/workspaces/{workspace_id}/integrations/deferred`, and replayed when "
            "the space is unfrozen."
        ),
    },
    {
        "name": "Health",
        "description": (
            "Three endpoints answering three different questions, all unauthenticated and "
            "all disclosing no configuration: what they report is a state — `ok`, or "
            "`error:` and an exception class name — never an address, a DSN or a "
            "credential. `/live` says the process is up and touches nothing — a liveness "
            "probe that queries the database turns a brief outage into a restart loop. "
            "`/ready` says it can serve a request, and is the one a load balancer and the "
            "container healthcheck should use. `/dependencies` reports the database and a "
            "`degraded` flag, and answers 200 even when it is degraded, because a "
            "diagnostic that cannot answer is the least useful thing it could be."
        ),
    },
    {
        "name": "Storage",
        "description": (
            "Workspace storage settings and issue attachments. Supports local filesystem "
            "and cloud providers (AWS S3, Azure Blob Storage) with MCP integration."
        ),
    },
    {
        "name": "Initiatives",
        "description": (
            "Cross-space portfolio initiatives (Epics 2.0) uniting strategic goals, "
            "multi-space roadmaps, and progress/budget burn rollups."
        ),
    },
    {
        "name": "Deployments",
        "description": (
            "CI/CD deployment pipelines and environment markers across Development, "
            "Staging, and Production environments."
        ),
    },
]

# OpenAPI is served everywhere except production. The SPA and any integrator need a
# machine-readable contract; a production deployment does not need to publish its entire
# route table, parameter names and response shapes to anyone who can reach the port. Note
# that this is *absence*, not an authorisation check — there is no `/docs` in production to
# be authorised against, which is a stronger property than one guarded by a dependency.
app = FastAPI(
    title=settings.app_name,
    description=API_DESCRIPTION,
    version=API_VERSION,
    openapi_tags=OPENAPI_TAGS,
    # Points at the repository README tells a reader to clone. There is no support mailbox
    # to publish, and inventing one would be worse than omitting it.
    contact={
        "name": "PMT Project",
        "url": "https://github.com/shakthivel-rc/PMT-API",
    },
    # `identifier` rather than `url`: the SPDX id is what a licence scanner reads, and
    # OpenAPI 3.1 — which FastAPI emits — allows exactly one of the two.
    license_info={"name": "Apache License 2.0", "identifier": "Apache-2.0"},
    docs_url="/docs" if settings.docs_enabled else None,
    redoc_url="/redoc" if settings.docs_enabled else None,
    openapi_url="/openapi.json" if settings.docs_enabled else None,
)

# The envelope for everything nobody wrote a handler for. Registered here rather than from
# inside `install_error_handlers` so that the middleware below stays visible in one list —
# and note that these handlers alone do NOT fix SEC-8: Starlette gives the `Exception`
# handler to `ServerErrorMiddleware`, which sits outside every middleware added below, so
# the response it builds carries none of their headers. `UnhandledExceptionMiddleware` is
# the half that does, and its position in the stack is the reason it works. See
# `core/errors.py`.
install_error_handlers(app)

# Middleware, INNERMOST FIRST.
#
# `add_middleware` inserts at the front of the stack, so the last one added is the one that
# sees a request first and a response last. The order below is therefore read bottom-up,
# and every position in it is load-bearing:
#
#   CORS            outermost — so that a 401, a 403 and a 429 all carry
#                   `Access-Control-Allow-Origin` and the SPA can read them. This used to
#                   be innermost, and the effect was that an expired session produced an
#                   opaque network error in the browser instead of "your session ended":
#                   the 401 was generated by JWTAuthMiddleware *above* CORS and never got
#                   the header. Preflight worked, which is why it looked fine.
#   RequestContext  mints the request id and logs the outcome — including the outcomes the
#                   layers below it produce, which is the point of it being above them.
#   SecurityHeaders  applies to every response, including error responses.
#   UnhandledException  turns an escaped exception into the ErrorResponse envelope *here*,
#                   inside the three above it, so that a 500 carries the CORS header, the
#                   CSP and the request id like every other answer. Above everything that
#                   can realistically raise, and below everything that adds a header —
#                   there is exactly one position with both properties and this is it.
#   BodySizeLimit   refuses a body over `MAX_REQUEST_BODY_BYTES`. Outside the rate limiter
#                   because it is the cheaper of the two refusals — one header parse, no
#                   per-client state — and because nothing below it should ever be handed a
#                   body this process is not willing to hold. Inside the three header
#                   middlewares for the same reason as the 401: a 413 the SPA cannot read
#                   is indistinguishable from the upload dying.
#   RateLimit       refuses before authentication, so a refused request costs no session
#                   lookup and no database checkout.
#   JWTAuth         innermost of the six; the last thing before a route.
app.add_middleware(JWTAuthMiddleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(BodySizeLimitMiddleware)
app.add_middleware(UnhandledExceptionMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestContextMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    # The methods this API actually serves rather than `*`. `*` is not a vulnerability while
    # the origin list is restricted, but it describes a surface this service does not have,
    # and a header that overstates what is allowed is one nobody can use to reason about
    # what is allowed. PATCH is the one exception: no route here uses it today, but the
    # SPA's `src/api/client.ts` keeps a generic `patch()` helper for the first product
    # route that does, and `tests/unit/test_security_headers.py` pins this exact list.
    # 2026-09-29: that first route exists — `PATCH /workspaces/{id}/issues/{id}`.
    # PATCH is no longer an exception.
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    # Content-Type and Authorization are everything `src/api/client.ts` sends; Accept and
    # X-Request-ID are the two a caller may legitimately add. A new client header means a
    # deliberate edit here, which is the review this list exists to force.
    allow_headers=["Accept", "Authorization", "Content-Type", "X-Request-ID"],
    # So a browser client can read the correlation id off a response and quote it in a
    # support request. Without this, `X-Request-ID` is set, sent, and invisible.
    expose_headers=["X-Request-ID"],
)

app.include_router(auth.router)
app.include_router(user.router)
app.include_router(roles.router)
app.include_router(permissions.router)
app.include_router(activity_logs.router)
app.include_router(workspaces.router)
app.include_router(issues.router)
app.include_router(time_tracking.router)
app.include_router(time_tracking.timer_router)
app.include_router(reports.router)
app.include_router(sprints.router)
app.include_router(poker.router)
app.include_router(pql.router)
# 2026-10-05, docs/design/09-restructuring.md: workflows, transfers and migrations,
# dependencies, releases and saved board filters.
app.include_router(workflow.router)
app.include_router(transfer.router)
app.include_router(migrations.router)
app.include_router(dependencies.router)
app.include_router(releases.router)
app.include_router(saved_views.router)
app.include_router(storage.router)
app.include_router(integrations.router)
app.include_router(integrations.admin_router)
app.include_router(chat.router)
app.include_router(chat.user_chat_router)
app.include_router(ai_agents.router)
app.include_router(timeline.router)
app.include_router(timeline.issue_router)
app.include_router(initiatives.router)
app.include_router(deployments.router)
app.include_router(deployments.issue_deployments_router)
app.include_router(notifications.router)
app.include_router(health.router)


@app.get("/")
def read_root():
    return {"message": "Base Project API"}

"""Every route is either on the public list, with a reason, or it demands a token.

This is the highest-leverage test in the security set, and it is here because of what it
makes true of the *next* route rather than the current ones. `JWTAuthMiddleware`
authenticates by default and exempts by list, which is the right way round — but nothing
checked the list, so an endpoint added under an exempt prefix, or a path added to
`UNAUTHENTICATED_ROUTES` while wiring something up, was open and silent.

The mechanism is an inventory that has to be edited deliberately: adding a public route
fails this test until somebody writes it down here next to a sentence saying why it is
public. That sentence is the review.

Three assertions, in both directions and then about the sweep itself:

* the exempt set and `PUBLIC` are equal — no route slips out of authentication, and no
  stale entry sits here claiming an exemption that no longer exists;
* every non-public route, called with no `Authorization` header, actually answers 401.
  The first assertion reads configuration; this one reads behaviour, and they are only
  worth having together.
* the walk that produces both of those reaches every operation named in the app's own
  OpenAPI document. Added 2026-08-27, after the fastapi 0.141 upgrade emptied the walk and
  the 401 sweep passed over 1 route out of 114 without a word — see `tests/route_walk.py`,
  which is where that story is written down. "The sweep passed" and "the sweep passed over
  everything" are different claims, and only naming the routes makes the second one.
"""
import ast
import pathlib

import pytest

from main import app
from routes.auth_middleware import UNAUTHENTICATED_PREFIXES, UNAUTHENTICATED_ROUTES
from tests.route_walk import iter_routes

pytestmark = pytest.mark.unit


#: The complete public surface, path -> why it cannot require a token.
#:
#: Adding a line here is the deliberate act this test exists to force. Removing one is
#: free — the route simply becomes authenticated like everything else.
PUBLIC: dict[str, str] = {
    # --- identity: the endpoints you use before you have a token ---------------------
    "/user/login": "issues the token; requiring one would be circular",
    "/user/register": (
        "self-service sign-up. Rate limited as a credential path, and the account it "
        "creates is unverified until the emailed link is followed."
    ),
    "/user/refresh": "exchanges a refresh token, which is its own credential",
    "/user/verify-token": "the link in a verification email",
    "/user/set-password": "the link in a verification email",
    "/user/set-password-registration": "completes sign-up before a session exists",
    "/user/forgot-password": "by definition reached by someone who cannot sign in",
    "/user/verify-forgot-password-token": "the link in a reset email",
    "/user/reset-password": "the link in a reset email",
    "/user/verify-otp": "second factor, before the session is issued",
    "/user/resend-otp": "second factor, before the session is issued",

    # --- inbound integrations ---------------------------------------------------------
    "/integrations/github/webhook": (
        "GitHub cannot hold a session. The route authenticates each delivery itself: an "
        "X-Hub-Signature-256 HMAC-SHA256 of the raw body under GITHUB_WEBHOOK_SECRET, "
        "compared in constant time before the body is parsed, and every delivery is "
        "refused when no secret is configured (integrations/github.verify_signature; "
        "tests/integration/test_github_webhook.py)."
    ),

    # --- operational ------------------------------------------------------------------
    "/api/v1/health/live": "liveness probe; must answer before anything else works",
    "/api/v1/health/ready": "readiness probe, hit by the container healthcheck every 10 s",
    "/api/v1/health/dependencies": (
        "the same database check with more detail, for an operator; it reports state and "
        "never an address or a credential (routes/health.py)"
    ),

    # --- non-production only ----------------------------------------------------------
    # These exist only when ENVIRONMENT != prod (main.py sets docs_url=None otherwise), so
    # in production they are absent rather than unauthorised — a stronger property.
    "/docs": "Swagger UI, absent in production",
    "/redoc": "ReDoc, absent in production",
    "/openapi.json": "the machine-readable contract, absent in production",
}

#: Path parameters are substituted with something harmless. The value never matters: the
#: middleware rejects before routing reaches a handler, which is the property being tested.
PARAM_VALUE = "00000000-0000-0000-0000-000000000000"


def _routes():
    # `iter_routes`, never a bare `for route in app.routes`: fastapi 0.141 wraps each
    # `include_router` call in an `_IncludedRouter`, and the flat loop finds only the few
    # routes registered on the app itself.
    for route in iter_routes(app.routes):
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if path and methods:
            yield path, sorted(methods - {"HEAD", "OPTIONS"})


#: The verbs an OpenAPI path item may carry. A path item's other keys — `parameters`,
#: `summary`, `servers`, `$ref` — are not operations, so an intersection is taken rather
#: than every key counted.
_OPENAPI_VERBS = frozenset(
    {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
)


def _contract_operations() -> set[tuple[str, str]]:
    """Every `(path, METHOD)` the app publishes in its own contract.

    The independent second opinion on what the route table contains. It is derived from
    `app.openapi()`, which FastAPI builds by its own traversal rather than this file's, so
    the two cannot break together in the same way — which is the entire value of comparing
    them.

    It returns the pairs and not a count. Widened from `_operation_count()`, which returned
    only a size, on the same day that helper was written — 2026-08-27 — for the reason set
    out on the test below: a count is satisfiable by the wrong routes.
    """
    return {
        (path, verb.upper())
        for path, item in app.openapi()["paths"].items()
        for verb in _OPENAPI_VERBS & item.keys()
    }


def _routes_without_methods():
    """Route objects this file's HTTP assertions structurally cannot cover.

    `APIWebSocketRoute` has no `methods`, and `Mount` delegates to a sub-application. Both
    were silently dropped by the filter above, and a websocket is the worse of the two:
    `JWTAuthMiddleware` is a `BaseHTTPMiddleware`, which by its own docstring never sees a
    websocket scope — so such a route would be both unauthenticated and uninventoried, and
    every test in this file would still be green.
    """
    return [
        f"{type(route).__name__} {getattr(route, 'path', '?')}"
        for route in iter_routes(app.routes)
        if getattr(route, "path", None) and not getattr(route, "methods", None)
    ]


def _is_exempt(path: str) -> bool:
    return (
        path in UNAUTHENTICATED_ROUTES
        or path.startswith(UNAUTHENTICATED_PREFIXES)
        or path in {"/docs", "/redoc", "/openapi.json"}
    )


#: Every `(path, method)` an anonymous caller must be refused on. Module level so that
#: `TestBehaviour` parametrises over the same object `TestTheSweepCannotGoBlind` measures —
#: a sweep and a check on that sweep's size that read different sources check nothing.
PRIVATE_SWEEP = sorted(
    (path, method) for path, methods in _routes() if not _is_exempt(path) for method in methods
)


class TestTheInventory:
    def test_no_route_is_public_without_being_written_down(self):
        exempt = {path for path, _ in _routes() if _is_exempt(path)}
        undocumented = exempt - PUBLIC.keys()
        assert not undocumented, (
            "These routes skip authentication and are not in PUBLIC:\n  "
            + "\n  ".join(sorted(undocumented))
            + "\n\nIf that is deliberate, add each one to PUBLIC with the reason. If it is "
            "not, remove it from UNAUTHENTICATED_ROUTES in routes/auth_middleware.py."
        )

    def test_no_stale_entries(self):
        """A list nobody prunes stops describing the system and starts excusing it."""
        exempt = {path for path, _ in _routes() if _is_exempt(path)}
        stale = PUBLIC.keys() - exempt
        assert not stale, f"PUBLIC names routes that are not exempt (or gone): {sorted(stale)}"

    def test_nothing_is_silently_outside_the_inventory(self):
        """A route with no `methods` is not covered by anything else in this file.

        There are none today. If one is added — a websocket, a Mount — this fails, and the
        decision it forces is the point: either it authenticates itself (see the note at
        the top of `JWTAuthMiddleware`, which does not see websocket scopes) or it is
        listed here with the reason.
        """
        uncovered = _routes_without_methods()
        assert not uncovered, (
            "These route objects carry no HTTP methods, so the assertions in this file "
            "never reach them:\n  " + "\n  ".join(uncovered)
            + "\n\nJWTAuthMiddleware does not authenticate websocket scopes. Whatever "
            "this is, it must\nauthenticate itself, and it must be written down."
        )

    def test_every_reason_is_a_reason(self):
        for path, why in PUBLIC.items():
            assert len(why) > 20, f"{path}: write down why this is public"


class TestTheSweepCannotGoBlind:
    """A gate that stops seeing routes must fail, not pass over the ones it can still see.

    This is the lesson of F-22 arriving from upstream. On 2026-08-27 the SEC-9 dependency
    upgrade took fastapi from 0.115 to 0.141, which stopped flattening `include_router` into
    `app.routes` — see `tests/route_walk.py` for the mechanism. Every assertion in this file
    was built on `for route in app.routes`, so after the upgrade the walk found 5 routes
    where the service then had 114. `test_no_stale_entries` failed loudly, which is why anybody
    looked; `TestBehaviour` below parametrised over a single route and reported PASSED.

    Nothing was wrong with the app. The gate had simply stopped covering it, and said so in
    the only way a vacuous assertion ever does, which is not at all. The cheap repair is to
    make the sweep state what it covered and check that against a list built by somebody
    else: the first test below names every operation in `app.openapi()` and fails if the
    walk did not reach it, and the second names five endpoints that must be in the sweep by
    the time the exemption filter has had its say. A sweep now has to survive two questions
    it used to be trusted on — did you see the service, and did you keep the parts that
    matter.
    """

    def test_the_walk_reaches_every_operation_in_the_contract(self):
        """Would catch: enumeration returning a fraction of the route table — or wrong paths.

        Containment in one direction, never equality. The walk legitimately sees more than
        the contract does — Starlette's `/docs`, `/redoc`, `/openapi.json` and
        `/docs/oauth2-redirect` are reachable URLs and are swept, but are not operations of
        this service, and a route marked `include_in_schema=False` would be another. The
        failure being defended against is the walk coming back *smaller*, and only that
        direction is ever a mistake.

        Written as `len(swept) >= operation_count` and widened to this set difference the
        same day, 2026-08-27, because a count is satisfiable by the wrong routes and nothing
        else in this file would notice. `JWTAuthMiddleware` runs
        ahead of routing, so an anonymous request for a path that does not exist answers
        **401 exactly like a path that does** — measured 2026-08-27, `GET /no-such-route`
        returns 401. A walk that yielded 114 invented paths would therefore have satisfied
        the size check, passed all 110 sweep cases green, and covered nothing at all. Naming
        the pairs is what makes the sweep's *subject* checkable; a size only ever proved it
        was non-empty. The size check also carried 4 pairs of slack — 114 swept against 110
        operations, the `/docs` family being the difference — so four operations could have
        fallen out of the walk under it without a word.

        It is additionally the only check on the prefix hazard documented in
        `tests/route_walk.py`: routes reached through `original_router.routes` carry no
        include-time prefix, so the day somebody writes `include_router(r, prefix="/x")`
        this fails and names every path in `r`. A count would have stayed at 114 while the
        sweep swept `/foo` against a service serving `/x/foo` — and, per the paragraph
        above, would have got a 401 for it.
        """
        swept = {(path, method) for path, methods in _routes() for method in methods}
        operations = _contract_operations()
        missing = operations - swept
        assert not missing, (
            f"This file's route walk reached {len(swept)} (path, method) pairs but missed "
            f"{len(missing)} of the {len(operations)} operations the app's own OpenAPI "
            "document publishes:\n  "
            + "\n  ".join(f"{method} {path}" for path, method in sorted(missing))
            + "\n\nEvery assertion in this file is therefore passing over less than the "
            "service, the 401 sweep included — and the sweep cannot tell you, because a "
            "route it never reaches and a route it protects both look like 401 from here. "
            "Fix `iter_routes` in tests/route_walk.py; do not relax this assertion."
        )

    def test_the_anonymous_sweep_actually_reaches_the_routes_that_matter(self):
        """Would catch: a full walk whose routes all fall out again before they are swept.

        The test above measures `_routes()`, which is the walk. This one measures
        `PRIVATE_SWEEP`, which is the walk *after* `_is_exempt` has removed everything the
        allow-list covers — and that filter is the other place the sweep can quietly empty
        out. A `UNAUTHENTICATED_PREFIXES` that grew a `"/"`, or an `_is_exempt` that started
        returning True on an error, leaves the walk complete and the sweep hollow, and the
        assertion above would still pass.

        These five are the identity surface — every user account, role and permission, and
        the audit trail of what was done with them. F-14 proved `GET /users` servable with
        no token at all, so if the sweep is meaningful anywhere, it is meaningful here.
        """
        swept = {path for path, _ in PRIVATE_SWEEP}
        missing = {"/users", "/users/profile", "/roles", "/permissions",
                   "/activity-logs"} - swept
        assert not missing, (
            f"The anonymous sweep does not cover {sorted(missing)}. Either these routes have "
            "moved — update this list — or they have become exempt from authentication, "
            "which is not a thing that should happen quietly."
        )


ROOT = pathlib.Path(__file__).resolve().parent.parent.parent

#: Where an inbound request's path can be turned into a decision. A directory whose code
#: only makes **outbound** calls is deliberately absent: reading `.url.path` off an outbound
#: URL is perfectly correct there, and the scan below cannot tell the two apart, so it is
#: not pointed at code where the innocent form lives.
PATH_DECISION_DIRS = ("core", "routes", "controllers", "dependencies", "services")


def _url_path_reads(source: str) -> list[int]:
    """Line numbers of every `<something>.url.path` attribute read in `source`.

    An AST walk and not a grep, and that is load-bearing rather than tidy: all four sites
    that correctly read `scope["path"]` carry a comment naming `request.url.path` as the
    thing they are not doing, and those comments are the most valuable lines in the
    middleware. A textual scan would have to be taught to ignore them, which is the kind of
    exception that eventually swallows a real hit.
    """
    return [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Attribute)
        and node.attr == "path"
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "url"
    ]


class TestPathConfusion:
    """The allow-list must be decided on the path the router will actually serve.

    Starlette builds `request.url` by re-parsing `f"{scheme}://{host}{path}"` with the
    **Host header**, which the caller controls. Middleware that reads `request.url.path`
    therefore makes its decision about a different string from the one the router
    dispatches on. `JWTAuthMiddleware` did, and the result was total: with
    `Host: x/api/v1/health`, `GET /users` answered **200 with no token**, disclosing every
    account.

    This is the regression test for that. It is here rather than beside the middleware
    because the property being defended is the one this file is about: no route is
    reachable without authentication unless it is written down above.

    **2026-08-27, starlette 1.6.0.** The paragraph above describes starlette up to 1.5;
    the SEC-9 upgrade fixed the folding upstream and it no longer happens for any Host
    value. The history stays because it is why the middleware is written the way it is, and
    because a pin can be rolled back. See the second test below for what changed, what is
    now asserted, and why the defence in `routes/auth_middleware.py` is kept regardless.

    That upstream fix cost this class its teeth, which is why there are now three tests and
    not two. On starlette 1.6.0 `URL(scope=...).path` and `scope["path"]` are equal for
    every Host header, so both behavioural tests below pass whichever of the two the
    middleware reads — and CLAUDE.md §10 cites this class by name as what *enforces* that it
    reads the right one. A cited mechanism that cannot fail is the same defect as a `PUBLIC`
    justification that is present but untrue (CLAUDE.md §10): a guarantee claimed in prose
    and not held by anything. The third test is the one that holds it.
    """

    #: Makes `request.url.path` start with an exempt prefix while `scope["path"]` does not.
    SPOOFED_HOST = "attacker.example/api/v1/health"

    def test_a_forged_host_header_does_not_exempt_a_private_route(self, client):
        for path in ("/users", "/roles", "/permissions", "/activity-logs"):
            response = client.get(path, headers={"Host": self.SPOOFED_HOST})
            assert response.status_code == 401, (
                f"{path} answered {response.status_code} with a forged Host header and no "
                "token. Middleware is deciding on request.url.path (caller-controlled) "
                "instead of scope['path'] (what the router serves)."
            )

    def test_the_forgery_no_longer_moves_request_url_path_and_must_not_start_again(self):
        """starlette 1.6.0 fixed the hazard upstream. Recorded here on 2026-08-27.

        This assertion used to run the other way. It asserted that a forged Host header
        *does* move `URL(scope=...).path` onto an exempt prefix, so that the sibling test
        above could not start passing because the attack had stopped working rather than
        because the fix does. That was the right test to write, and the world moved under
        it: starlette 1.6.0 — in this repository since the SEC-9 upgrade on 2026-08-27 —
        matches the Host header against `^([a-z0-9.-]+|\\[[a-f0-9]*:[a-f0-9.:]+\\])(?::[0-9]+)?$`
        and falls back to `scope["server"]` when it does not match. Probed against the
        installed version with `host/path`, `host%2Fpath`, `host:80/path`, `user@host/path`,
        `[::1]/path` and `host#/path`: none of them move the path any more.

        So it is now the inverse tripwire, and the failure message is written for that
        reading: if `URL(scope=...).path` ever stops being `scope["path"]` — a pin rolled
        back, an upstream regression, a proxy or server that populates the scope
        differently — this goes red and names the hazard before an auditor has to find it
        again.

        **Why the defence in `routes/auth_middleware.py` stays**, stated plainly because
        this is exactly the point at which someone deletes it as superstition: with the
        hazard closed upstream, the sibling test above now passes whether the middleware
        reads `scope["path"]` or `request.url.path`, because on starlette 1.6.0 those two
        values are equal. That makes the sibling test the weaker one today, not the defence
        unnecessary. `scope["path"]` is the string the router dispatches on; reading it is
        correct independently of which starlette is installed; and the release that
        reintroduces the difference will not be one anybody notices on the day it lands.
        Rule §10 in CLAUDE.md — middleware branches on `request.scope["path"]`, never on
        `request.url.path` — is unchanged.
        """
        from starlette.datastructures import URL

        scope = {
            "type": "http", "scheme": "http", "path": "/users", "query_string": b"",
            "headers": [(b"host", self.SPOOFED_HOST.encode())], "server": ("t", 80),
        }
        url = URL(scope=scope)
        assert url.path == "/users", (
            f"A forged Host header moved request.url.path to {url.path!r}. The F-14 "
            "path-confusion hazard is live again in the installed starlette "
            "(this test recorded it fixed in 1.6.0 on 2026-08-27). Every middleware that "
            "branches on a path must be reading scope['path'] — check "
            "routes/auth_middleware.py and core/middleware.py, both of which do today."
        )
        # The Host was not sanitised into the path, it was rejected outright and the netloc
        # came from `scope["server"]` instead. Asserted so that a future starlette which
        # keeps the path intact but starts trusting the header again is still caught here.
        assert url.hostname == "t"
        assert scope["path"] == "/users"

    def test_no_request_handling_module_decides_anything_on_url_path(self):
        """Would catch: the F-14 defence deleted as superstition. Added 2026-08-27.

        The two tests above are behavioural and, since starlette 1.6.0 that same day, both
        of them pass with `request.scope.get("path", "")` in
        `routes/auth_middleware.py` swapped back to `request.url.path`. Measured, not
        assumed: `JWTAuthMiddleware.dispatch` was recompiled in process with exactly that
        one expression changed and this class re-run, and both went green. Four middlewares
        make a decision on the path (`JWTAuthMiddleware`, `SecurityHeadersMiddleware`,
        `RateLimitMiddleware`, `RequestContextMiddleware`); on the installed starlette all
        four keep working either way, and the day a pin moves is the day three of them go
        wrong at once. That is the whole reason F-14 was one bug with three symptoms.

        So this one is structural on purpose. It is not the only assertion here that can go
        red — the sibling above still catches authentication disappearing altogether — but
        on the installed starlette it is the only one that can go red for *the reason this
        class exists*. A source scan and not a runtime probe, because there is no longer a
        runtime difference to probe for, which is precisely the condition under which a
        correct defence starts looking like dead code to the next reader.

        **What it sees**: a literal `x.url.path` attribute read anywhere in the directories
        that handle inbound requests, middleware or not — a controller comparing
        `request.url.path` is the same bug one layer down. Comments are invisible to it by
        construction; see `_url_path_reads`.

        **What it does not see**, stated so nobody reads more into a green run than is
        there: `str(request.url)` taken apart by hand, `urlsplit(str(request.url)).path`, a
        `URL(scope=...)` built here and read somewhere else, or a decision made in a module
        outside `PATH_DECISION_DIRS`. It is a tripwire on the one spelling that has actually
        caused an outage in this repository, not a proof that no such decision exists. The
        error direction is a false positive — a legitimate `.url.path` gets reported, and
        the author either rewrites it against `scope["path"]` or writes down why it is safe.
        """
        offenders = []
        sources = [ROOT / "main.py"]
        for directory in PATH_DECISION_DIRS:
            sources += sorted((ROOT / directory).rglob("*.py"))
        for source in sources:
            offenders += [
                f"{source.relative_to(ROOT)}:{line}"
                for line in _url_path_reads(source.read_text())
            ]

        assert not offenders, (
            "These read `.url.path`, which Starlette builds from the caller's Host "
            "header, rather than `request.scope[\"path\"]`, which is the string the router "
            "dispatches on:\n  " + "\n  ".join(offenders)
            + "\n\nIf this is a path decision, it is the F-14 authentication bypass — read "
            "the note above `path = request.scope.get(\"path\", \"\")` in "
            "routes/auth_middleware.py, and CLAUDE.md §10. If it is genuinely not a "
            "decision, it still must not be written this way here: the two spellings agree "
            "on the installed starlette and stop agreeing without warning, so there is no "
            "reading of this line that stays correct across a version bump."
        )


class TestBehaviour:
    @pytest.mark.parametrize("path,method", PRIVATE_SWEEP)
    def test_a_private_route_refuses_an_anonymous_caller(self, client, path, method):
        url = path
        while "{" in url:
            start = url.index("{")
            end = url.index("}", start)
            url = url[:start] + PARAM_VALUE + url[end + 1:]

        response = client.request(method, url)
        assert response.status_code == 401, (
            f"{method} {path} answered {response.status_code} with no token. Either it is "
            "public — add it to PUBLIC with a reason — or its authentication is missing."
        )

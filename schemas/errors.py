"""The refusals every authenticated route can produce, declared once instead of on every route.

Zero routes carried a `responses=` argument before this module existed, so
`docs/openapi-digest.txt` described the payload of an operation and none of the four other
answers a caller is far more likely to have to handle. An integrator reading `/docs` saw a
200 and a 422 and had to discover 401, 403 and 429 from a browser console.

Apply it per route, or per router where every route in it is guarded the same way:

    @router.get(
        "/users/{user_id}",
        response_model=SuccessResponse[UserDetail],
        responses={**COMMON_ERRORS, 404: {"model": ErrorResponse,
                                          "description": "No such user."}},
        ...
    )

**Every description below was checked on 2026-08-28 against the code that produces the
response, not against what the response ought to be**, because a documented error shape the
service does not produce is worse than none. The four sources are
`routes/auth_middleware.py` (401), `dependencies/auth.py` (401 and 403),
`core/middleware.py` (429) and `core/errors.py` (500). All four return a real
`ErrorResponse` envelope; the 500 does so only because `UnhandledExceptionMiddleware`
exists — before it, that status was `text/plain` with no CORS header at all, which is why
this dict could not honestly have been written earlier.

Not included: **404**. It is a per-route answer with a per-route meaning, so "not found"
here would be a claim only the route can make. Declare it on the route. No record in the
base is scoped to an owner, so every 404 it produces is about the id; a product that adds
records owned by one account answers 404, not 403, for someone else's (CLAUDE.md §8) and
says so in that route's own 404 description — never in the shared 403 below, which every
route that spreads this dict publishes.
"""
from typing import Any

from schemas.response import ErrorResponse

#: The four refusals any authenticated route can answer with instead of its payload.
#:
#: Keyed by `int` because that is what FastAPI's `responses=` compares against when it
#: merges these into the generated schema; a `"401"` string key produces a second, separate
#: entry rather than replacing the framework's.
COMMON_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {
        "model": ErrorResponse,
        "description": (
            "Not authenticated: no `Authorization: Bearer` header, a token whose signature "
            "or expiry does not check out, or a session that has been signed out or "
            "revoked server-side. `JWTAuthMiddleware` answers this before the route runs, "
            "so a valid-looking token whose session row is gone lands here too. Exchange "
            "the refresh token at `POST /user/refresh`; if that also answers 401 the "
            "session is finished and the caller has to sign in again. `data` is empty."
        ),
    },
    403: {
        "model": ErrorResponse,
        "description": (
            "Authenticated, but the permission this route requires is not granted to any "
            "of the caller's roles. It is a statement about the *caller*, never about the "
            "resource. Nothing about retrying helps — an administrator has to grant the "
            "permission."
        ),
    },
    429: {
        "model": ErrorResponse,
        "description": (
            "Rate limited. `Retry-After` carries the number of seconds to wait before "
            "trying again, and honouring it is the whole of the correct client behaviour. "
            "The limit is counted per client address per minute, before authentication, "
            "so an unauthenticated flood costs no session lookup; the credential "
            "endpoints get a much smaller budget than everything else. `data` is empty."
        ),
    },
    500: {
        "model": ErrorResponse,
        "description": (
            "Something failed that nobody wrote a handler for. The body carries the "
            "correlation id — `data.request_id`, always present as a key even when empty, "
            "and the same value as the `X-Request-ID` response header — and quoting it in "
            "a support request is what makes the report actionable: it is the key the "
            "traceback was logged under. The body never names the exception, a stack "
            "frame or a connection string, so there is nothing else in it to send on."
        ),
    },
}

#: The 422 FastAPI already documents on any route that has a request body or a validated
#: parameter — spread it in *there and only there*, because a route with neither cannot
#: produce one and declaring it would overstate the surface.
#:
#: This exists because the generated 422 is currently wrong rather than merely undescribed.
#: FastAPI declares `HTTPValidationError` for it — the digest says so — but since
#: `core/errors.validation_exception_handler` was installed the service answers with an
#: `ErrorResponse` whose `data.errors` holds the pydantic error list verbatim. Declaring
#: this replaces a shape the service does not produce with the one it does.
VALIDATION_ERROR: dict[int | str, dict[str, Any]] = {
    422: {
        "model": ErrorResponse,
        "description": (
            "One or more values in the request did not validate. `data.errors` is the "
            "pydantic error list verbatim — each entry names the field by `loc` and the "
            "reason by `msg` — which is what a form needs to highlight the offending "
            "input rather than showing the whole request as bad."
        ),
    },
}

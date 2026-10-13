"""`check_permissions` must refuse by raising, in the one configuration that would expose it.

A FastAPI dependency that *returns* a refusal does not refuse. The return value is injected
as the resolved dependency and the handler runs anyway — so `check_permissions`, whose
unauthenticated branch returned an `ErrorResponse` until 2026-08-27, would have admitted an
anonymous caller and bound that object where the handler expected a `User`.

That branch was never reachable in the shipped app, and the tests below are written for the
configuration in which it *would* be: a route that is exempt from `JWTAuthMiddleware` (so
`request.state.user` is never populated) and permission-guarded at the same time. Nothing in
`routes/` is both today. `tests/unit/test_route_inventory.py` is what keeps the exempt list
honest; this file keeps the dependency honest for the day somebody adds the one route that
list does not stop — an endpoint under `/api/v1/health`, say, that wants to check a
permission when a token happens to be present.

What these would catch: a refusal in `dependencies/auth.py` written as `return` instead of
`raise`. The failure is silent — no error, no log line, a 200 from a handler that believes it
was authorized — which is why it needs a test rather than a review.
"""
import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from db.session import get_db
from dependencies.auth import check_permissions
from routes.auth_middleware import UNAUTHENTICATED_PREFIXES, JWTAuthMiddleware

pytestmark = pytest.mark.unit

#: Under an exempt prefix, so `JWTAuthMiddleware` waves the request through without setting
#: `request.state.user` — and permission-guarded, so `check_permissions` is the only thing
#: between an anonymous caller and the handler. That combination is the whole scenario.
EXEMPT_AND_GUARDED_PATH = "/api/v1/health/rbac-probe"


class _AnonymousRequest:
    """The shape `check_permissions` reads: a `state` with no `user` on it.

    Not a real `Request`, deliberately — building one means a scope, a receive channel and a
    server, none of which this branch touches. The dependency does exactly one thing with the
    argument before it refuses, and that is `getattr(request.state, "user", None)`.
    """

    class _State:
        pass

    def __init__(self):
        self.state = self._State()


def _app_with_an_exempt_guarded_route() -> TestClient:
    app = FastAPI()
    app.add_middleware(JWTAuthMiddleware)

    @app.get(EXEMPT_AND_GUARDED_PATH,
             dependencies=[Depends(check_permissions(["manage_users"]))])
    def probe():
        return {"reached": True}

    # No database is opened. The refusal happens before the session is touched, and the
    # override is what keeps this test in the `unit` set — which matters, because a fail-open
    # in the authorization path should be provable with zero infrastructure.
    app.dependency_overrides[get_db] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


def test_an_exempt_permission_guarded_route_refuses_an_anonymous_caller():
    """The end-to-end proof: 401, and the handler never ran.

    Would catch the fail-open exactly as an attacker would meet it. Before the fix this
    returned **200 `{"reached": true}`** with no `Authorization` header at all, because the
    dependency's `return ErrorResponse(...)` resolved successfully.
    """
    assert EXEMPT_AND_GUARDED_PATH.startswith(UNAUTHENTICATED_PREFIXES), (
        "this test is only meaningful while the path is exempt from JWTAuthMiddleware; "
        "the exempt prefixes moved, so pick another one"
    )

    response = _app_with_an_exempt_guarded_route().get(EXEMPT_AND_GUARDED_PATH)

    assert response.status_code == 401, response.text
    assert "reached" not in response.text, "the handler ran for an unauthenticated caller"


def test_the_unauthenticated_branch_raises_rather_than_returning():
    """The same contract at the dependency, without a transport in the way.

    Would catch a refusal that is merely *constructed* — the distinction the endpoint test
    above proves matters, stated where the next person editing `check_permissions` will read
    it. `db=None` is safe: nothing reaches the session on this path.
    """
    checker = check_permissions(["manage_users"])

    with pytest.raises(HTTPException) as raised:
        checker(_AnonymousRequest(), None)

    assert raised.value.status_code == 401
    assert raised.value.detail["message"] == "Not authenticated"


def test_no_shipped_route_is_both_exempt_and_permission_guarded():
    """Why the fix is a latent-hole fix and not an incident.

    Records the fact the audit rested on, so that a route registration which makes the
    fail-open live cannot happen unnoticed. It is a weaker statement than the two tests above
    — they hold whatever this one says — and it is here because "not reachable today" is a
    claim that rots.
    """
    from main import app
    from routes.auth_middleware import UNAUTHENTICATED_ROUTES
    from tests.route_walk import iter_api_routes

    exempt_and_guarded = []
    for route in iter_api_routes(app.routes):
        path = route.path
        if path not in UNAUTHENTICATED_ROUTES and not path.startswith(UNAUTHENTICATED_PREFIXES):
            continue
        for dependency in route.dependant.dependencies:
            if getattr(dependency.call, "__name__", "") == "role_checker":
                exempt_and_guarded.append(f"{sorted(route.methods)} {path}")

    assert not exempt_and_guarded, (
        "these routes are exempt from JWTAuthMiddleware *and* declare check_permissions, "
        "which is the configuration the fail-open needed: " + ", ".join(exempt_and_guarded)
    )

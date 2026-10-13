"""An error leaves this service in the same envelope, with the same headers, as a success.

Before `core/errors.py` there was no `@app.exception_handler` anywhere, so an unhandled
exception was answered by Starlette's `ServerErrorMiddleware` — `text/plain`, no
`Access-Control-Allow-Origin`, no `Content-Security-Policy`, no `X-Request-ID`. What that
costs is not tidiness: a browser cannot read a cross-origin response with no CORS header at
all, so the SPA saw a server error and a dead network as the same thing, and the user saw a
spinner either way. It is F-03's failure mode (fixed for 401s in `main.py`) still live for
the 500 class.

**The assertion that matters is `test_a_500_carries_the_cors_header_the_csp_and_the_request
_id`.** Registering the handler alone does *not* produce that: Starlette hands the
`Exception` handler to `ServerErrorMiddleware`, which is installed outside every middleware
`main.py` adds, so the response it builds is created above CORS and above both header
middlewares and never passes back through them. `UnhandledExceptionMiddleware` is the half
that fixes it and its position in the stack is the whole mechanism — which is why the order
is asserted here too, against the real app, rather than against a hand-built copy of it that
would keep passing after somebody reordered the real one.
"""
import logging

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from core.errors import UnhandledExceptionMiddleware, install_error_handlers
from core.middleware import (
    BodySizeLimitMiddleware,
    RateLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from main import app
from routes.auth_middleware import JWTAuthMiddleware
from schemas.response import ErrorResponse

pytestmark = pytest.mark.unit

ORIGIN = "http://localhost:3000"

#: Appears in the exception the probe route raises, and must appear in the log and nowhere
#: in the response. Deliberately not secret-shaped: what is being tested here is that the
#: *body* quotes nothing internal, not that the redactor works (see test_log_redaction.py).
MARKER = "kaboom-marker-9f3b21c7"


@pytest.fixture()
def boom():
    """A route on the REAL app that raises, added for one test and taken away again.

    Against `main.app` rather than a mirror of its middleware stack, because the thing under
    test *is* the wiring in `main.py`: a mirror with the same `add_middleware` calls would
    go on passing after somebody reordered the real ones, which is the only way this
    regresses.

    Registered under `/api/v1/health`, the one prefix `JWTAuthMiddleware` exempts, so the
    request reaches the handler with no token and no database — this stays a unit test.

    Removed in `finally` because `tests/unit/test_route_inventory.py` walks the app's routes
    and requires every one of them to be written down with a reason.
    """
    path = "/api/v1/health/__unhandled_exception_probe__"

    def explode():
        raise RuntimeError(MARKER)

    app.add_api_route(path, explode, methods=["GET"])
    try:
        yield path
    finally:
        app.router.routes = [route for route in app.router.routes
                             if getattr(route, "path", None) != path]


@pytest.fixture()
def handlers_client():
    """A minimal app with the handlers and nothing else, for the `detail` shapes.

    Nothing in this service raises `HTTPException` with a list yet, and the point of the
    handler is that it will be ready when something does.
    """
    tiny = FastAPI()
    install_error_handlers(tiny)

    @tiny.get("/enveloped")
    def enveloped():
        # Exactly what `dependencies/auth.py` raises for a 403.
        raise HTTPException(status_code=403, detail=ErrorResponse(
            status_code=403, message="Insufficient permissions").model_dump())

    @tiny.get("/plain")
    def plain():
        raise HTTPException(status_code=404, detail="No such user")

    @tiny.get("/structured")
    def structured():
        raise HTTPException(status_code=409, detail=[{"field": "slug", "why": "taken"}])

    @tiny.get("/bare")
    def bare():
        raise HTTPException(status_code=404)

    @tiny.get("/with-headers")
    def with_headers():
        raise HTTPException(status_code=401, detail="Token has expired",
                            headers={"WWW-Authenticate": "Bearer"})

    return TestClient(tiny)


class TestAnUnhandledExceptionIsAnEnvelope:
    def test_a_500_carries_the_cors_header_the_csp_and_the_request_id(self, client, boom):
        """The one that would catch the whole of SEC-8 coming back.

        A 500 without `Access-Control-Allow-Origin` is unreadable in a browser, so the SPA
        reports it as a network failure; without the CSP it is the one response class that
        drops a security header; without `X-Request-ID` the user has nothing to quote and
        the log line and the failure cannot be connected.
        """
        response = client.get(boom, headers={"Origin": ORIGIN})

        assert response.status_code == 500
        assert response.headers.get("access-control-allow-origin") == ORIGIN
        assert "default-src 'none'" in response.headers.get("content-security-policy", "")
        assert response.headers.get("x-request-id")
        assert response.headers["content-type"].startswith("application/json")

    def test_the_body_is_the_envelope_and_quotes_nothing_internal(self, client, boom):
        """CLAUDE.md §8: never a stack frame, a connection string or the exception text.

        The request id is the whole of the payload, because it is the only thing the reader
        can do something with — quoting it leads an operator straight to the traceback.
        """
        response = client.get(boom)
        body = response.json()

        assert body["status"] == "error" and body["status_code"] == 500
        assert body["data"]["request_id"] == response.headers["X-Request-ID"]
        assert MARKER not in response.text
        assert "Traceback" not in response.text and "RuntimeError" not in response.text

    def test_the_traceback_is_logged_against_the_same_request_id(self, client, boom, caplog):
        """The half of the trade the caller does not see. Hiding the error from the response
        is only acceptable because it is written down somewhere an operator can find it, and
        findable *by the id the caller was given*."""
        with caplog.at_level(logging.ERROR, logger="base_project.error"):
            response = client.get(boom)

        records = [r for r in caplog.records if r.name == "base_project.error"]
        assert len(records) == 1
        record = records[0]
        assert record.exc_info is not None
        assert MARKER in (record.exc_text or "")
        assert record.extra_fields["request_id"] == response.headers["X-Request-ID"]
        assert record.extra_fields["path"] == "/api/v1/health/__unhandled_exception_probe__"

    def test_the_request_line_still_records_the_500(self, client, boom, caplog):
        """`RequestContextMiddleware` is outside the catcher, so it sees the answer the
        catcher produced. If it were inside, every 500 would be missing from the request
        log — the exact class of request an operator greps for."""
        with caplog.at_level(logging.INFO, logger="base_project.request"):
            client.get(boom)

        completed = [r for r in caplog.records if r.getMessage() == "request_completed"]
        assert completed and completed[-1].extra_fields["status_code"] == 500


class TestTheOrderIsWhatMakesThatWork:
    """`main.py`'s stack, read outermost-first. Each assertion below is a header that a 500
    would lose if the catcher moved one position out."""

    @staticmethod
    def _order() -> list[type]:
        # `user_middleware` is outermost-first: `add_middleware` inserts at index 0, so the
        # last one added in main.py is the first one here.
        return [middleware.cls for middleware in app.user_middleware]

    @pytest.mark.parametrize("outer", [CORSMiddleware, RequestContextMiddleware,
                                       SecurityHeadersMiddleware])
    def test_every_header_middleware_is_outside_the_catcher(self, outer):
        order = self._order()
        assert order.index(outer) < order.index(UnhandledExceptionMiddleware), (
            f"{outer.__name__} is inside the exception catcher, so the response it builds "
            f"never reaches it and a 500 loses that header."
        )

    @pytest.mark.parametrize("inner", [BodySizeLimitMiddleware, RateLimitMiddleware,
                                       JWTAuthMiddleware])
    def test_everything_that_can_raise_is_inside_the_catcher(self, inner):
        order = self._order()
        assert order.index(UnhandledExceptionMiddleware) < order.index(inner), (
            f"{inner.__name__} is outside the exception catcher, so an exception raised in "
            f"it lands in ServerErrorMiddleware with no envelope and no headers."
        )

    def test_all_three_handlers_are_registered(self):
        """The net for what the middleware cannot see: anything raised above it, and
        anything raised once the response body is already streaming."""
        from fastapi.exceptions import RequestValidationError
        from starlette.exceptions import HTTPException as StarletteHTTPException

        registered = set(app.exception_handlers)
        assert {Exception, StarletteHTTPException, RequestValidationError} <= registered


class TestHttpExceptionsAreEnvelopedToo:
    def test_a_404_from_the_router_is_an_envelope(self, client):
        """FastAPI's default answers `{"detail": "Not Found"}`, which is not the shape
        CLAUDE.md §8 says every response has, and not a shape `src/types/index.ts`
        describes."""
        response = client.get("/api/v1/health/no-such-endpoint")

        assert response.status_code == 404
        assert response.json() == {"status": "error", "status_code": 404,
                                   "message": "Not Found", "data": {}}

    def test_an_already_enveloped_detail_is_not_wrapped_twice(self, handlers_client):
        """`dependencies/auth.py` raises `HTTPException(403, detail=<an ErrorResponse>)`.
        FastAPI's default nested that under `detail`, so a 403 answered with an envelope
        inside a non-envelope and a client had to know which refusals were which."""
        response = handlers_client.get("/enveloped")

        assert response.status_code == 403
        assert response.json() == {"status": "error", "status_code": 403,
                                   "message": "Insufficient permissions", "data": None}

    def test_a_string_detail_becomes_the_message(self, handlers_client):
        assert handlers_client.get("/plain").json()["message"] == "No such user"

    def test_a_structured_detail_goes_in_data_rather_than_through_str(self, handlers_client):
        """`str()` of a list of reasons is a Python repr, and a Python repr in a `message`
        field is what a user is shown."""
        body = handlers_client.get("/structured").json()

        assert body["message"] == "Conflict"
        assert body["data"] == [{"field": "slug", "why": "taken"}]

    def test_a_detail_free_exception_still_says_something(self, handlers_client):
        assert handlers_client.get("/bare").json()["message"] == "Not Found"

    def test_the_headers_on_the_exception_survive(self, handlers_client):
        """`WWW-Authenticate` on a 401 and `Retry-After` on a 429 are protocol, not
        decoration. An envelope that drops them is right in the body and wrong on the wire."""
        response = handlers_client.get("/with-headers")

        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"


class TestValidationRefusalsLookLikeEveryOtherRefusal:
    def test_a_422_is_the_envelope(self, client):
        response = client.post("/user/login", json={})

        assert response.status_code == 422
        body = response.json()
        assert body["status"] == "error" and body["status_code"] == 422

    def test_the_field_errors_are_kept(self, client):
        """A form highlights the offending input, which needs the field and the reason. The
        envelope is for consistency; throwing the list away to get it would be a downgrade."""
        errors = client.post("/user/login", json={}).json()["data"]["errors"]

        assert {tuple(error["loc"]) for error in errors} >= {("body", "password")}
        assert all(error["msg"] for error in errors)

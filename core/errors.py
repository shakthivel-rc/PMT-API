"""One envelope for every refusal, including the ones nobody wrote a handler for.

Until this module existed there was no `@app.exception_handler` anywhere in the service, so
an exception that reached the top was answered by Starlette's `ServerErrorMiddleware`:
`text/plain`, no `Access-Control-Allow-Origin`, no `Content-Security-Policy`, no
`X-Request-ID`, and not a `SuccessResponse`/`ErrorResponse` envelope (CLAUDE.md §8). In a
browser that is indistinguishable from the network being down, because the SPA cannot read
a cross-origin response with no CORS header at all — so a server error and an outage
produced the same spinner. That is F-03's failure mode, fixed for 401s in `main.py` and
still live for 500s.

## Why registering the handler is not, on its own, the fix

`Starlette.build_middleware_stack` pulls the handler registered for `Exception` (or `500`)
out of the handler map and hands it to `ServerErrorMiddleware`, which it installs
**outside every middleware `main.py` adds**:

    ServerErrorMiddleware → CORS → RequestContext → SecurityHeaders → … → ExceptionMiddleware

So the response that handler builds is created above CORS and above the two middlewares
that set headers, and it is written straight to the ASGI `send` without passing back
through any of them. Measured on fastapi 0.141.1 / starlette 1.6.0 on 2026-08-27: with only
`@app.exception_handler(Exception)` registered, a 500 for a request carrying `Origin` had no
`Access-Control-Allow-Origin`, no `X-Request-ID` and no marker header from a middleware
sitting one layer below CORS. The body changed; nothing else did.

`UnhandledExceptionMiddleware` is what actually fixes it. It sits *inside* the three
header-setting middlewares and *outside* everything that can raise, catches the exception
there, and returns the envelope as an ordinary response — which then travels back out
through the security headers, the request-id line and CORS like any other response. See the
order comment in `main.py`; its position is the whole point of it.

The three handlers stay registered as the last-resort net for what the middleware cannot
see: an exception raised *above* it (inside `RequestContextMiddleware` itself, say) and one
raised after the response body has already started streaming. Those still lose the headers,
which is unavoidable — but they no longer lose the envelope.

## What a 500 body may contain

The request id, and nothing else. Never the exception text, never a frame, never a
connection string (CLAUDE.md §8): the traceback goes to the log at ERROR, keyed by the same
id, so the person reading the error and the person reading the log can meet at one
identifier without the first one being handed the second one's data.
"""
from __future__ import annotations

import http
import logging

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from schemas.response import ErrorResponse


#: Its own channel rather than `base_project.request`, so that an operator can alert on "the
#: service threw" without alerting on "the service answered".
logger = logging.getLogger("base_project.error")

#: What a 500 says. It names no exception, no frame and no connection string, and it tells
#: the reader the one thing that makes their report actionable: the id that ties what they
#: saw to the traceback an operator can find.
SERVER_ERROR_MESSAGE = (
    "Something went wrong on our side. Quote the request id below if you report this."
)

#: The keys `ErrorResponse.model_dump()` always produces. A `detail` carrying all of them is
#: an envelope that some other layer already built — `dependencies/auth.py` raises exactly
#: that for 401 and 403 — and wrapping it again would produce `data.message` nested inside
#: `message`, which no client can read and nothing in `src/types/index.ts` describes.
_ENVELOPE_KEYS = frozenset({"status", "status_code", "message"})


def _request_id(request: Request) -> str:
    """The correlation id for this request, or "" if it never got one.

    `RequestContextMiddleware` sets `state.request_id` before it calls anything below it, so
    every handler reached through the normal path has one. The header fallback covers the
    case this module exists for: an exception raised *above* that middleware, where the
    caller's own `X-Request-ID` is the only id anybody has.
    """
    return getattr(request.state, "request_id", "") or request.headers.get("X-Request-ID", "")


def _phrase(status_code: int) -> str:
    """"Not Found" for 404. A sentence beats a bare number for whoever reads the toast."""
    try:
        return http.HTTPStatus(status_code).phrase
    except ValueError:
        # A handler may raise any integer. Refusing to render one because it is not in the
        # IANA registry would turn a deliberate 499 into a 500.
        return "Request failed"


def server_error_response(request: Request, exc: BaseException) -> JSONResponse:
    """The 500 envelope, and the one place the traceback is written down.

    Shared by the middleware and the handler so that the two cannot drift into telling a
    caller two different stories about the same failure.
    """
    request_id = _request_id(request)
    logger.error(
        "unhandled_exception",
        exc_info=exc,
        extra={"extra_fields": {
            "request_id": request_id,
            "method": request.method,
            # `scope["path"]`, the path the router dispatched on, for the same reason the
            # request log line reads it (CLAUDE.md §10). No route here carries a credential
            # in its path; the structural redaction in `core/logging.py` still runs over it.
            "path": request.scope.get("path", ""),
        }},
    )
    body = ErrorResponse(
        status_code=500,
        message=SERVER_ERROR_MESSAGE,
        # Always the same key, even when the id is empty: a client that renders
        # `data.request_id` must not have to branch on whether the field exists.
        data={"request_id": request_id},
    )
    return JSONResponse(status_code=500, content=body.model_dump())


async def unhandled_exception_handler(request: Request, exc: Exception) -> Response:
    """The last resort, for exceptions `UnhandledExceptionMiddleware` never sees."""
    return server_error_response(request, exc)


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> Response:
    """A raised `HTTPException`, in the envelope, at the status the raiser chose.

    Three shapes of `detail` reach here and each keeps what it is worth:

    * an **already-built envelope dict** (`dependencies/auth.py`) is returned as it stands.
      FastAPI's default handler nested it under `detail`, so a 403 answered
      `{"detail": {"status": "error", …}}` — an envelope inside a non-envelope.
    * a **string** is the message, which is what almost every raiser means by it.
    * anything **structured** — a list of reasons, a dict of field errors — goes in `data`
      rather than through `str()`, because `"['a', 'b']"` in a message field is a Python
      repr rendered to a human.
    """
    detail = exc.detail
    if isinstance(detail, dict) and _ENVELOPE_KEYS <= set(detail):
        content: object = detail
    elif detail is None or isinstance(detail, str):
        content = ErrorResponse(
            status_code=exc.status_code,
            message=detail or _phrase(exc.status_code),
            data={},
        ).model_dump()
    else:
        content = ErrorResponse(
            status_code=exc.status_code,
            message=_phrase(exc.status_code),
            data=jsonable_encoder(detail),
        ).model_dump()
    # `exc.headers` carries `WWW-Authenticate` on a 401 and `Retry-After` on a 429. Dropping
    # them would make the envelope right and the protocol wrong.
    return JSONResponse(status_code=exc.status_code, content=content,
                        headers=exc.headers or None)


async def validation_exception_handler(request: Request,
                                       exc: RequestValidationError) -> Response:
    """A 422 that looks like every other refusal instead of like FastAPI's own.

    The pydantic error list is kept verbatim in `data.errors` — it names the field and the
    reason, and a form that highlights the offending input needs both. It is nested under a
    key rather than being `data` itself so that adding anything else here later does not
    change `data`'s *type* out from under a client.

    `jsonable_encoder` because `exc.errors()` can carry a `ctx` holding the original
    exception object, which `json.dumps` cannot serialise.
    """
    errors = jsonable_encoder(exc.errors())
    message = "Some of the values sent are not valid."
    if errors and isinstance(errors, list):
        first_err = errors[0]
        err_msg = first_err.get("msg")
        if err_msg:
            clean_err = err_msg.replace("Value error, ", "").strip()
            loc = first_err.get("loc", [])
            field = loc[-1] if loc and loc[-1] != "body" else None
            if field and not clean_err.lower().startswith(field.lower()):
                message = f"{field}: {clean_err}"
            else:
                message = clean_err

    body = ErrorResponse(
        status_code=422,
        message=message,
        data={"errors": errors},
    )
    return JSONResponse(status_code=422, content=body.model_dump())


class UnhandledExceptionMiddleware(BaseHTTPMiddleware):
    """Turn an exception into the 500 envelope *inside* the middleware stack.

    Everything this catches would otherwise land in `ServerErrorMiddleware`, outside CORS
    and outside both header middlewares — see this module's docstring for why that is not
    good enough. Placed in `main.py` so that it wraps the router and every middleware that
    can raise, and is wrapped by the three that add headers.

    What it cannot catch: an exception raised while the response body is already streaming,
    because the status line has been sent by then and there is nothing left to replace. The
    registered handler answers those, without the headers.
    """

    async def dispatch(self, request: Request, call_next):
        try:
            return await call_next(request)
        except Exception as exc:  # catching everything is the feature
            # `Exception`, not `BaseException`: a cancelled request must stay cancelled, and
            # answering 500 to a client that has already gone away would only hold the
            # worker open longer.
            return server_error_response(request, exc)


def install_error_handlers(app: FastAPI) -> None:
    """Register the three handlers. `main.py` adds the middleware separately, on purpose.

    Keeping the `add_middleware` call in `main.py` rather than hiding it here is what keeps
    the order comment there true: the stack is read in one place, in one direction, and a
    middleware that appeared from inside an `install_*` helper would be invisible in it.
    """
    # FastAPI's `HTTPException` subclasses Starlette's, so this one registration covers both
    # and replaces the `{"detail": …}` default FastAPI installs for it.
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)

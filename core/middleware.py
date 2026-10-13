from __future__ import annotations

import ipaddress
import logging
import time
import uuid

from fastapi import HTTPException, Request
from starlette.datastructures import Headers
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from core.config import Settings, get_settings
from core.ratelimit import SlidingWindowLimiter
from schemas.response import ErrorResponse


logger = logging.getLogger("base_project.request")

#: Paths where a wrong guess is cheap for the attacker and expensive for us: password and
#: OTP guessing, account enumeration, and the two endpoints that send mail on demand.
#:
#: Exact paths rather than a `/user/` prefix, deliberately, and for the same reason
#: `routes/auth_middleware.py` gives for its own list: a prefix silently covers whatever
#: somebody hangs underneath it later, and the person adding that endpoint has no reason
#: to look here. `tests/unit/test_rate_limit.py` asserts the two lists have not drifted.
CREDENTIAL_PATHS = frozenset({
    "/user/login",
    "/user/register",
    "/user/refresh",
    "/user/forgot-password",
    "/user/reset-password",
    "/user/set-password",
    "/user/set-password-registration",
    "/user/verify-otp",
    "/user/resend-otp",
    "/user/verify-token",
    "/user/verify-forgot-password-token",
})

def _parse_networks(values) -> tuple:
    """CIDRs and bare addresses to `ip_network` objects, skipping anything unparseable.

    Skipping rather than raising: a typo in this variable must not stop the service
    booting, and the safe direction on a bad entry is "trust one fewer proxy".
    """
    networks = []
    for raw in values:
        try:
            networks.append(ipaddress.ip_network(raw, strict=False))
        except ValueError:
            logger.warning("ignoring unparseable TRUSTED_PROXY_IPS entry",
                           extra={"extra_fields": {"value": raw}})
    return tuple(networks)


def _is_trusted(address: str, networks) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(parsed in network for network in networks)


#: The probes, and only the probes. The container healthcheck hits `/ready` every 10 s and
#: a load balancer will hit it more often than that; a liveness probe that trips a rate
#: limit takes the service out of rotation for being alive.
#:
#: Exact paths, not the `/api/v1/health` prefix this used to be — for the same reason
#: `CREDENTIAL_PATHS` above is exact. A prefix exempts whatever somebody hangs underneath
#: it later, and the person adding that endpoint has no reason to look here. It was already
#: exempting `/dependencies`, which reads the database, i.e. the most expensive thing under
#: that prefix was the least limited thing in the service.
UNLIMITED_PATHS = frozenset({
    "/api/v1/health/live",
    "/api/v1/health/ready",
})


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id
        started_at = time.perf_counter()

        response = await call_next(request)
        latency_ms = round((time.perf_counter() - started_at) * 1000, 2)
        response.headers["X-Request-ID"] = request_id

        logger.info(
            "request_completed",
            extra={
                "extra_fields": {
                    "request_id": request_id,
                    "method": request.method,
                    # The routed path, not `request.url.path` — a log line that echoes a
                    # caller-controlled Host header is a log line an attacker can forge. No
                    # route here carries a credential in its path; one that did would need
                    # its segment masked here, because no pattern in `core/redact.py` could
                    # recognise a random token by its shape.
                    "path": request.scope.get("path", ""),
                    "status_code": response.status_code,
                    "latency_ms": latency_ms,
                }
            },
        )

        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """The response headers `nginx.conf` already sets for the SPA, for the API's own port.

    The SPA is served by nginx, which sets a full set — CSP, nosniff, DENY, a referrer
    policy. The API is a separate service on a separate port that anything can reach
    directly, and it was setting none of them. "The reverse proxy does it" is only true
    for the origin the reverse proxy serves.

    The policy here is the API's, not the SPA's: this port returns JSON, so
    `default-src 'none'` is not a restriction on anything it legitimately does. It is a
    restriction on what a browser will do with a response somebody tricked it into
    rendering — the reflected-content and content-sniffing classes, which need a document
    context to matter and get none.

    `/docs`, `/redoc` and `/openapi.json` are exempt: Swagger UI is a real HTML document
    that loads its own script and style, and it exists only outside production (see
    `main.py`). Applying the JSON policy to it would produce a blank page in development
    and teach everyone to ignore the CSP.
    """

    #: A JSON API needs no script, style, image, font, frame or form. Naming them all as
    #: `'none'` rather than relying on `default-src` alone is verbose on purpose: the
    #: fetch directives that do NOT fall back to `default-src` are exactly the ones people
    #: forget, and this way the header says what it means without the reader knowing which
    #: is which.
    API_CSP = (
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    )

    DOCS_PATHS = frozenset({"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"})

    def __init__(self, app, settings: Settings | None = None) -> None:
        super().__init__(app)
        self._settings = settings or get_settings()

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        # `setdefault` semantics: a route that deliberately set one of these — a file
        # download choosing its own disposition, say — keeps its value. Middleware that
        # overwrites a handler's explicit decision is a bug that is very hard to find.
        headers = response.headers
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "no-referrer")
        headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")

        # `scope["path"]`, not `request.url.path` — see the note in
        # `routes/auth_middleware.py`. The Host header is caller-controlled and feeds
        # `request.url`, so reading it here let a caller make any response look like a
        # `/docs` response and drop this header.
        if request.scope.get("path", "") not in self.DOCS_PATHS:
            headers.setdefault("Content-Security-Policy", self.API_CSP)

        # HSTS only in production. Sent over plain HTTP a browser ignores it, so this is
        # not a correctness rule — it is a "do not pin a developer's localhost to HTTPS
        # for the next two years" rule, and that mistake is tedious to undo.
        if self._settings.is_production:
            headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )

        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Refuse too many requests from one client, before they cost anything.

    Two limits, because two very different things are being protected:

    * `credential` — the login, registration, OTP and password-reset endpoints. Small.
      An account lockout already exists (`AUTH_MAX_FAILED_LOGIN_ATTEMPTS`) and covers a
      single account under sustained attack; it does nothing about one client trying one
      password against a thousand accounts, or about the two endpoints that send mail.
    * `general` — everything else, generous enough that no human or SPA reaches it, tight
      enough that a scripted sweep of `/api/v1/...` does.

    Placed OUTSIDE `JWTAuthMiddleware` in `main.py`, so a refused request never reaches
    the session lookup. Placed INSIDE `CORSMiddleware` for the same reason a 401 is: the
    SPA has to be able to read a 429 to show "slow down" rather than an opaque failure.

**Identifying the client.** The peer address, unless the peer is a proxy named in
    `TRUSTED_PROXY_IPS`, in which case the right-most `X-Forwarded-For` entry that is not
    itself a trusted proxy.

    All three parts of that sentence are load-bearing, and the first version of this class
    got it wrong in a way that mattered:

    * *Peer address alone* is wrong for the only topology this product ships. `nginx.conf`
      proxies `/api/` to this service and `docker-compose.yml` exposes only the SPA
      container, so **every browser request arrives from the nginx container's address** —
      one bucket for every user, and any unauthenticated client could exhaust the
      10-per-minute login budget for every user at once. That is a denial of service
      created by the control meant to prevent one.
    * *Believing `X-Forwarded-For` unconditionally* is wrong the other way: the header is
      caller-controlled, so an attacker mints a fresh bucket per request and the limit
      stops existing. A boolean switch cannot avoid both of these; an allow-list can.
    * *Right-most, not left-most.* nginx uses `$proxy_add_x_forwarded_for`, which
      **appends** the peer to whatever the client sent. The left-most entry is therefore
      attacker-supplied; the right-most non-proxy entry is the address nginx actually saw.
    """

    def __init__(self, app, settings: Settings | None = None) -> None:
        super().__init__(app)
        settings = settings or get_settings()
        self._enabled = settings.rate_limit_enabled
        self._trusted = _parse_networks(settings.trusted_proxy_ips)
        self._credential = SlidingWindowLimiter(
            settings.rate_limit_credential_per_minute, 60.0
        )
        self._general = SlidingWindowLimiter(settings.rate_limit_general_per_minute, 60.0)

    def _client_key(self, request: Request) -> str:
        peer = request.client.host if request.client else "unknown"
        if not self._trusted or not _is_trusted(peer, self._trusted):
            return peer
        forwarded = request.headers.get("X-Forwarded-For", "")
        for candidate in reversed([hop.strip() for hop in forwarded.split(",") if hop.strip()]):
            if not _is_trusted(candidate, self._trusted):
                return candidate
        # Every hop is a proxy we trust, or the header is absent. The peer is the best
        # answer available and is at least not attacker-chosen.
        return peer

    async def dispatch(self, request: Request, call_next):
        if not self._enabled or request.method == "OPTIONS":
            return await call_next(request)
        # `scope["path"]`, not `request.url.path`: the latter is built from the
        # caller-controlled Host header, so `Host: x/api/v1/health` made every path look
        # exempt and removed the limit entirely. Same root cause as the authentication
        # bypass documented in `routes/auth_middleware.py`; one bug, three symptoms.
        path = request.scope.get("path", "")
        if path in UNLIMITED_PATHS:
            return await call_next(request)

        credential = path in CREDENTIAL_PATHS
        limiter = self._credential if credential else self._general
        # Separate key spaces. Otherwise a client's ordinary API traffic exhausts the
        # credential budget and one screen refresh locks somebody out of logging in.
        key = f"{'cred' if credential else 'api'}:{self._client_key(request)}"

        retry_after = limiter.check(key)
        if retry_after is None:
            return await call_next(request)

        # Logged at WARNING with the path but WITHOUT the key: the key is a client IP,
        # which is personal data, and the thing an operator needs from this line is
        # "something is hammering /user/login", not who.
        logger.warning(
            "rate_limited",
            extra={"extra_fields": {
                "path": path,
                "limit": limiter.limit,
                "window_seconds": limiter.window,
                "scope": "credential" if credential else "general",
            }},
        )
        body = ErrorResponse(
            status_code=429,
            message="Too many requests. Slow down and try again shortly.",
            data={},
        )
        return JSONResponse(
            status_code=429,
            content=body.model_dump(),
            headers={"Retry-After": str(max(1, int(retry_after) + 1))},
        )


class BodySizeLimitMiddleware:
    """Refuse a request body above `MAX_REQUEST_BODY_BYTES`, before anything buffers it.

    Nothing bounded a request body before this. Starlette buffers the whole body in memory
    before a handler sees it, and the inbound rate limiter counts *requests*, not bytes — so
    one client posting 200 MB of JSON to any endpoint cost the process 200 MB and one unit
    of a 300-per-minute budget.

    **Two checks, because one is not enough.** `Content-Length` is the cheap one and covers
    every real client: it is refused at the door, before a single byte of the body is read.
    But a chunked request carries no `Content-Length` at all, and a caller is free to
    understate the one it does send, so the body is also counted as it streams and refused
    the moment the running total crosses the ceiling. Without the second check the limit
    would be advisory — it would bound only the clients that were honest about their size.

    The streaming refusal is raised as an `HTTPException(413)` rather than written as a
    response, because by then the body is being read from inside the handler: FastAPI turns
    an unrecognised exception during body parsing into a 400 "error parsing the body", and
    re-raises an `HTTPException` untouched. So raising is what keeps the status honest, and
    `core/errors.py` renders it in the same envelope as the header-check refusal.

    **Plain ASGI, not `BaseHTTPMiddleware`**, which is the odd one out in this file.
    `BaseHTTPMiddleware.dispatch` is handed a `Request` and never sees `receive`, and a
    limit that cannot wrap `receive` is a limit that only reads `Content-Length`.

    Starlette 1.6 ships `RequestBodyLimitMiddleware` and `Starlette(max_body_size=…)` doing
    much the same counting. It is not used here because the framework installs it *outside*
    every middleware `main.py` adds and answers `text/plain` "Content Too Large" — no
    envelope, no CORS header, no `X-Request-ID`, which is the exact defect `core/errors.py`
    exists to fix. A 413 the SPA cannot read is a 413 that looks like the network died.
    """

    def __init__(self, app: ASGIApp, max_bytes: int | None = None) -> None:
        self.app = app
        settings_bytes = get_settings().max_request_body_bytes
        self.max_bytes = settings_bytes if max_bytes is None else max_bytes

    def _refusal_body(self) -> dict:
        # The ceiling is in the message on purpose: "too large" without a number leaves the
        # caller bisecting their own payload to find out what this service will take.
        return ErrorResponse(
            status_code=413,
            message=(
                f"The request body is larger than this API accepts "
                f"({self.max_bytes} bytes)."
            ),
            data={"max_bytes": self.max_bytes},
        ).model_dump()

    def _log(self, scope: Scope, measured: int, declared: bool) -> None:
        logger.warning(
            "request_body_too_large",
            extra={"extra_fields": {
                "method": scope.get("method", ""),
                "path": scope.get("path", ""),
                "limit_bytes": self.max_bytes,
                "body_bytes": measured,
                # Which check fired. A stream of `declared=false` refusals is a client
                # sending chunked, which is worth knowing before someone raises the limit.
                "declared": declared,
            }},
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # 0 disables the ceiling, the same way `RATE_LIMIT_*_PER_MINUTE=0` disables a limit.
        if scope["type"] != "http" or self.max_bytes <= 0:
            await self.app(scope, receive, send)
            return

        declared = _declared_body_length(scope)
        if declared is not None and declared > self.max_bytes:
            self._log(scope, declared, declared=True)
            # Answered without reading the body: the point of checking the header is that
            # the bytes never arrive in this process. The connection is left for the server
            # to close, which is what uvicorn does when a body goes unread.
            response = JSONResponse(status_code=413, content=self._refusal_body())
            await response(scope, receive, send)
            return

        counted = 0

        async def limited_receive() -> Message:
            nonlocal counted
            message = await receive()
            if message["type"] == "http.request":
                counted += len(message.get("body", b""))
                if counted > self.max_bytes:
                    self._log(scope, counted, declared=False)
                    raise HTTPException(status_code=413, detail=self._refusal_body())
            return message

        await self.app(scope, limited_receive, send)


def _declared_body_length(scope: Scope) -> int | None:
    """`Content-Length`, or None when it is absent or not a number.

    None means "unknown", never "zero": a chunked upload has no `Content-Length`, and
    treating its absence as an empty body is how a size limit gets bypassed by a header
    nobody sent.
    """
    raw = Headers(scope=scope).get("content-length")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None

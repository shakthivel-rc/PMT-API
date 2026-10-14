import asyncio
import time

import jwt
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from sqlalchemy.orm import Session

from core.config import get_settings
from db.session import SessionLocal
from models.user_session import UserSession, hash_session_token
from schemas.response import ErrorResponse


settings = get_settings()

UNAUTHENTICATED_ROUTES = [
    "/user/login",
    "/user/register",
    "/user/refresh",
    "/user/verify-token",
    "/user/set-password",
    "/user/forgot-password",
    "/user/verify-forgot-password-token",
    "/user/reset-password",
    "/user/verify-otp",
    "/user/resend-otp",
    "/user/set-password-registration",
    # Authenticates itself with an HMAC signature over the raw body; see
    # routes/integrations.py and the PUBLIC entry in tests/unit/test_route_inventory.py.
    "/integrations/github/webhook",
    "/integrations/gitlab/webhook",
    "/integrations/bitbucket/webhook",
]
# Exact paths, not prefixes, for everything except the health probes: a prefix would exempt
# anything anyone later hangs underneath it, and the person adding that endpoint would have no
# reason to look here.
UNAUTHENTICATED_PREFIXES = ("/api/v1/health",)


class TokenRejected(Exception):
    """An access token that cannot authenticate a caller.

    The message is the one reported to the client verbatim, so the wording lives here
    rather than at each call site — two places telling a user different things about the
    same rejected token is how "my session keeps expiring" becomes unreproducible.
    """


def decode_access_token(token: str) -> dict:
    """The token's claims, or `TokenRejected`. Touches no database.

    Split from the session lookup deliberately: an unauthenticated caller with a garbage
    token must not cost a connection checkout, and `user_sessions.access_token` was an
    unindexed VARCHAR(1024) that every authenticated request already full-scanned once.

    That scan is gone as of 2026-08-27: the lookup is now an indexed equality on
    `access_token_hash` (revision `20260821a001`, P-1). The split stays anyway. An index
    makes the query cheap, not free, and the cost this ordering actually avoids — checking a
    connection out of the pool for a caller whose token cannot possibly authenticate — was
    always the larger of the two.
    """
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
    except jwt.ExpiredSignatureError:
        raise TokenRejected("Token has expired") from None
    except jwt.InvalidTokenError:
        raise TokenRejected("Invalid token") from None

    if payload.get("type", "access") != "access":
        raise TokenRejected("Invalid token type")
    return payload


def assert_session_is_live(db: Session, token: str, payload: dict) -> None:
    """Raise `TokenRejected` unless a live `user_sessions` row still backs this token.

    This is what makes logout and revocation take effect: a signed, unexpired JWT whose
    session row is gone must stop working immediately (CLAUDE.md §7 rule 8).

    The row is found by `hash_session_token(token)` and never by the token itself, because
    the token itself is not stored: the column holds a SHA-256 digest of it (SEC-5, revision
    `20260821a001`). That is deliberately the same function `controllers/auth_controller.py`
    writes the column with — two implementations that disagreed about so much as an encoding
    would not raise anywhere, they would match no row, and every signed-in user in the
    service would be logged out at once with nothing in any log saying why.

    Hashing on the read path costs 3.0 µs for a 260-character token (measured 2026-08-27,
    `hashlib.sha256` over 200 000 iterations), against a query that until P-1 scanned
    every session row the deployment had ever issued.
    """
    user_session = db.query(UserSession).filter(
        UserSession.access_token_hash == hash_session_token(token),
        UserSession.revoked_at.is_(None),
    ).first()
    if not user_session:
        raise TokenRejected("Invalid session or token revoked")

    if payload.get("exp", 0) < time.time():
        raise TokenRejected("Token has expired")


class JWTAuthMiddleware(BaseHTTPMiddleware):
    """Authenticates every HTTP request outside the allow-list.

    `BaseHTTPMiddleware` only ever sees `scope["type"] == "http"`, so a WebSocket
    connection would pass straight through it untouched. Nothing in this service speaks
    WebSocket today; anything that starts to must authenticate itself with
    `decode_access_token` and `assert_session_is_live` rather than assuming
    `request.state.user` was populated for it.
    """

    async def dispatch(self, request: Request, call_next):
        # `scope["path"]`, NOT `request.url.path`. They are different values and only one
        # of them is the path the router will dispatch on.
        #
        # Starlette builds `request.url` by re-parsing `f"{scheme}://{host}{path}"` using
        # the **Host header**, which the caller controls. A request for `/users` carrying
        # `Host: x/api/v1/health` yields `request.url.path == "/api/v1/health/users"`,
        # which passes the health-prefix exemption below — while the router still serves
        # `/users`. That was a complete authentication bypass: `GET /users` answered 200
        # with no token at all, disclosing every account. Confirmed against the running app
        # on 2026-08-26 and pinned by `tests/unit/test_route_inventory.py`.
        #
        # `scope["path"]` is set by the server from the request line and is the exact
        # string the router matches, so the decision here and the dispatch below it can no
        # longer disagree. Any middleware in this service that branches on a path must
        # read it the same way.
        path = request.scope.get("path", "")
        if path in UNAUTHENTICATED_ROUTES:
            return await call_next(request)
        if path.startswith(UNAUTHENTICATED_PREFIXES):
            return await call_next(request)
        # Present only when ENVIRONMENT != prod — see main.py. In production these paths
        # do not exist to be authorised against, which is a stronger property than one
        # guarded by a dependency.
        if path in {"/docs", "/redoc", "/openapi.json"}:
            return await call_next(request)
        if request.method == "OPTIONS":
            return await call_next(request)
        auth_header = request.headers.get("Authorization")
        token = None
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]
        elif request.query_params.get("token"):
            token = request.query_params.get("token")

        if not token:
            error_response = ErrorResponse(
                status_code=401, message="Missing or invalid token", data={}
            )
            return JSONResponse(status_code=401, content=error_response.model_dump())
        try:
            payload = decode_access_token(token)
        except TokenRejected as exc:
            error_response = ErrorResponse(status_code=401, message=str(exc), data={})
            return JSONResponse(status_code=401, content=error_response.model_dump())

        # Off the event loop. This is the highest-frequency database call in the service —
        # every authenticated request makes it — and `assert_session_is_live` was, by the
        # note on `decode_access_token` above, a full scan of an unindexed VARCHAR(1024).
        # Running it here blocked the event loop every `async def` handler shares, once per
        # request.
        #
        # It survived the first hardening pass because `tests/unit/test_no_blocking_db_calls.py`
        # could not see it: the query is one call frame away inside a helper, and
        # `db.close()` is not one of the session methods it looks for. The most frequent
        # instance of the defect was invisible to the check written to find it.
        #
        # A thread hop costs tens of microseconds against a table scan, so the trade is not
        # close — and the nested-`def` shape is what brings this inside that check.
        #
        # P-1 removed the table scan on 2026-08-27 and the hop stays. What is left is still a
        # socket round trip to MySQL and a row fetch, which is three orders of magnitude
        # above the hop; the trade did not become close, it only became less lopsided.
        def _verify_session() -> None:
            db: Session = SessionLocal()
            try:
                assert_session_is_live(db, token, payload)
            finally:
                db.close()

        try:
            await asyncio.to_thread(_verify_session)
        except TokenRejected as exc:
            error_response = ErrorResponse(status_code=401, message=str(exc), data={})
            return JSONResponse(status_code=401, content=error_response.model_dump())

        request.state.user = payload  # Attach user info to request
        return await call_next(request)

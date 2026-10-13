"""One route traversal, because fastapi 0.141 stopped flattening `include_router`.

Until fastapi 0.115, `app.include_router(r)` copied `r`'s routes into `app.routes`, so
walking `app.routes` walked the whole service. fastapi 0.141 — in this repository since the
SEC-9 dependency upgrade on 2026-08-27 — instead appends one `fastapi.routing._IncludedRouter`
per include: a plumbing object with no `path` and no `methods` whose real routes hang off
`.original_router.routes`. The route table did not change. The way you enumerate it did.

**What that silently broke is the reason this is a module and not a comment in a test.**
`tests/unit/test_route_inventory.py` is the gate F-02 created: every route exempt from JWT
authentication must be written down with a reason, and every other route, called with no
token, must answer 401. It enumerated with `for route in app.routes`, which after the
upgrade yields 18 objects of which 5 carry an HTTP method — the four `/docs`-family routes
and `GET /`. So, on the same commit:

* the "no stale entries" half failed loudly, because every entry in `PUBLIC` looked gone; and
* the 401 sweep parametrised over **one** route and passed. Green, fast, and covering 1 of
  114.

The second one is the dangerous half. It is F-22 — "two gates I built could not fail" —
arriving from upstream rather than from us, and it is the shape worth remembering: a
security gate does not announce that it has stopped looking. `test_route_inventory.py` now
asserts that this walk reaches every operation named in the app's own OpenAPI document — by
name, not by count, because an unmatched path answers 401 from `JWTAuthMiddleware` exactly
like a matched one, so a walk returning the right *number* of wrong paths would sweep them
all green. The next time enumeration breaks, that difference fails instead of nothing.

One traversal, imported by everything that enumerates routes, rather than a copy per file:
the breakage above was survivable because it was loud in one place. Four hand-written walks
would have to be found four times, and the three that stayed quiet are the ones that matter.

Two things to know before changing this:

* `_IncludedRouter` is private fastapi API and is imported by name deliberately. If a future
  fastapi renames or removes it, this module raises at import, during collection, and every
  gate that depends on it goes red at once — which is the failure we want. Duck-typing on
  `.original_router` instead would walk a truncated table and say nothing, i.e. it would
  reproduce exactly the defect this file exists to close.
* `original_router.routes` carries the router's **own** paths, so a prefix passed at include
  time — `include_router(r, prefix="/x")` — is not on them. `main.py` passes no include-time
  prefixes today. If the paths this yields ever look short by a prefix, that is the first
  thing to check.
"""
from collections.abc import Iterable, Iterator

from fastapi.routing import APIRoute, _IncludedRouter
from starlette.routing import BaseRoute


def iter_routes(routes: Iterable[BaseRoute]) -> Iterator[BaseRoute]:
    """Every route object reachable from `routes`, as fastapi 0.115 used to present them.

    A container is yielded **and then descended**, which is the one non-obvious choice here.
    F-27 fixed a blind spot where the inventory silently skipped route objects carrying no
    HTTP methods — an `APIWebSocketRoute`, a `Mount` — because `JWTAuthMiddleware` is a
    `BaseHTTPMiddleware` and never sees a websocket scope, so such a route would be both
    unauthenticated and uninventoried with every test still green. A walk that descended into
    a `Mount` without yielding it would hand that blind spot straight back. Yielding it makes
    `test_nothing_is_silently_outside_the_inventory` fire on the day one is added, which is
    also the day somebody has to decide what a sub-application's relative child paths mean to
    a caller that only ever knows absolute URLs.

    `_IncludedRouter` is the exception: it is pure plumbing, it has no path of its own, and
    before the upgrade it did not exist to be reported. It is descended and not yielded.
    """
    for route in routes:
        if isinstance(route, _IncludedRouter):
            yield from iter_routes(route.original_router.routes)
            continue
        yield route
        children = getattr(route, "routes", None)
        if children:
            yield from iter_routes(children)


def iter_api_routes(routes: Iterable[BaseRoute]) -> Iterator[APIRoute]:
    """The `APIRoute`s only — this service's own operations, one per OpenAPI operation.

    A filter over `iter_routes`, not a second walk: there is exactly one traversal in this
    repository and this is a view of it. Use this when you mean "the endpoints this service
    declares"; use `iter_routes` when you mean "everything a caller can address", which
    additionally includes Starlette's `/docs`, `/redoc`, `/openapi.json` and
    `/docs/oauth2-redirect` — and those are URLs an anonymous caller can reach, so the
    security sweep wants the wider one.
    """
    for route in iter_routes(routes):
        if isinstance(route, APIRoute):
            yield route

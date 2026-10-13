"""The two envelopes every response in this service travels in — CLAUDE.md §8.

A controller returns `SuccessResponse` on the success path and `ErrorResponse` — usually
wrapped in a `JSONResponse` so it can carry its own status — on a refusal. There is no
third shape, and a controller that formats HTTP by hand is one of the two drifts CLAUDE.md
§2 names.

`SuccessResponse` is generic over `data` so that a route can declare what it actually
returns:

    @router.get("/users", response_model=SuccessResponse[UserPage])

Before the type variable existed, `docs/openapi-digest.txt` reported `200:any` for almost
every operation: a contract that is structurally complete and semantically empty, which is
what Phase 2 of `docs/production-readiness-plan.md` exists to close. How a route uses this
— naming, decorator order, and the validation trap that comes with declaring a
`response_model` — is written down once, in `docs/api-documentation-standard.md`.
"""
from typing import Any

from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


# `[DataT]` is PEP 695 syntax rather than `Generic[DataT]` with a module-level `TypeVar`:
# `target-version = "py312"` makes the older spelling a `UP046` finding, and the ledger only
# goes down (CLAUDE.md §4). Pydantic 2.13.4 renders both identically — verified on
# 2026-08-28 that `SuccessResponse[Thing]` produces the same schema either way.
#
# `DataT` is deliberately **unbound**. An unparameterised `SuccessResponse(...)` then
# validates `data` as `Any`, which is what every construction site in the service relies on:
# counted by AST walk on 2026-08-28, every one of them passes `data=`, and between them they
# pass dicts, lists of dicts and bare scalars. Giving `DataT` a bound — `dict`, say — would
# start rejecting the list payloads on the day it was added: silently, at runtime, on the
# success path, in whichever endpoint nobody happened to call first.
class SuccessResponse[DataT](BaseModel):
    """A 2xx answer: a machine-readable status, a message for a human, and the payload.

    **Construct it bare, declare it parameterised.** Controllers build
    `SuccessResponse(status_code=200, message="...", data=...)` and never name a type;
    routes write `response_model=SuccessResponse[Thing]`. FastAPI serialises the returned
    model to a dict and then validates that dict against the declared parameterisation, so
    the two meet without a controller ever importing the response model.

    **`data` keeps a default, and the cost of that is one line of the schema.** Nothing in
    the service omits `data` today, so the default is not load-bearing for any caller — but
    it is what the convention in `docs/api-documentation-standard.md` was written against
    and what the route groups are being built on, so it stays. The consequence to know
    about: because the field has a default, `data` is absent from the `required` list of
    every generated `SuccessResponse[Thing]` schema, and a generated TypeScript client
    therefore types it `data?: Thing` where the SPA's hand-written `ApiResponse<T>` says
    `data: T`. Dropping the default makes those agree and breaks nothing that exists (every
    construction site passes `data`); it is one line, and it is deliberately not being taken as a
    side effect of a documentation pass. Pydantic does not leak the `{}` itself into the
    schema — measured on pydantic 2.13.4, 2026-08-28 — so `required` is the whole of it.
    """

    status: str = Field(
        default="success",
        description="Always `success` on this envelope. `ErrorResponse` carries `error`.",
    )
    status_code: int = Field(
        description="The HTTP status of this response, repeated in the body so a client "
                    "that only has the parsed JSON still knows what it got.",
    )
    message: str = Field(
        description="One sentence for a human, safe to render in a toast. Never a "
                    "connection string, a token or a stack frame (CLAUDE.md §8).",
    )
    # `# type: ignore` because `Field`'s overloads type `default_factory` against the
    # field's own annotation, and an annotation mypy cannot pin to a concrete type collapses
    # that parameter to `Callable[[], Never]`. The runtime is correct and the stubs are not;
    # the alternative is narrowing `data` to `dict`, which would start rejecting the list
    # payloads some endpoints return.
    #
    # 2026-08-26: first recorded, attributed to the stubs shipped with pydantic 2.10.
    # 2026-08-28: the pin has moved to pydantic 2.13.4 and the ignore was re-measured
    #   rather than assumed. mypy still reports `arg-type` here — "expected
    #   `Callable[[], Never] | Callable[[dict[str, Any]], Never]`" — for the `Any`
    #   annotation *and* for the unbound `DataT` this class now carries, so the cause is
    #   the unpinnable annotation rather than one release's stubs, and the ignore stays.
    #   `warn_unused_ignores = true` in `[tool.mypy]` means an ignore that stops being
    #   needed fails the build instead of quietly lying about why it is there.
    data: DataT = Field(
        default_factory=dict,  # type: ignore[arg-type]
        description="The payload. Parameterise the envelope in the route decorator to say "
                    "what it holds; unparameterised it is whatever the controller passed.",
    )


class ErrorResponse(BaseModel):
    """A refusal: the status, a message written for whoever will read it, and `data`.

    **`data` stays `Any`, on purpose, and must not be narrowed to a model.** Several
    endpoints put the *actionable* part of a refusal there rather than in `message`, and
    the shapes differ per endpoint: the password endpoints (`POST /user/set-password`,
    `/user/reset-password`, `/user/set-password-registration`, `/users/change-password`)
    answer 400 with `{errors: [...]}` listing every password-policy rule the value failed,
    and a 422 carries the pydantic error list in `data.errors`. The consumer —
    `src/api/client.ts` in the SPA repository — keeps `data` on its `ApiError` for exactly
    that reason: keeping only `message` would discard those lists at the boundary, and a form
    written to render them would say what was wrong with no indication of what to fix. It
    types the field `unknown` rather than a union, because every consumer narrows it and a
    type that enumerated today's shapes would be wrong the first time an endpoint added one.

    `Any` here is the Python spelling of that `unknown`. A route that wants to promise one
    of those shapes declares it for that status in `responses=`, which is where a
    per-endpoint promise belongs — not on the type every refusal in the service shares.

    What the four refusals *any* authenticated route can produce actually put here is
    recorded, checked against the code that produces it, in `schemas/errors.py`.
    """

    status: str = Field(
        default="error",
        description="Always `error` on this envelope. `SuccessResponse` carries `success`.",
    )
    status_code: int = Field(
        description="The HTTP status of this refusal, repeated in the body so a client "
                    "that only has the parsed JSON still knows what it got.",
    )
    message: str = Field(
        description="What went wrong, in one sentence, written for the person who will "
                    "read it. Never a connection string, a token or a stack frame.",
    )
    data: Any = Field(
        default=None,
        description="Whatever this particular refusal can act on — the password-policy "
                    "failures of a set/reset/change, the validation errors of a 422, "
                    "`request_id` on a 500 — or nothing. Narrow it per endpoint, never "
                    "here. See the docstring.",
    )


def error_json(status_code: int, message: str, data: Any = None) -> JSONResponse:
    """A refusal, as the `JSONResponse` a controller returns.

    The shape every controller in the base already builds by hand —
    `JSONResponse(status_code=..., content=ErrorResponse(...).model_dump())` — written once
    for the project-management controllers, which refuse far more often (every lookup can be
    a cross-tenant 404). It returns rather than raises, so a controller keeps owning its own
    rollback before it answers.
    """
    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(status_code=status_code, message=message, data=data).model_dump(),
    )

# The API documentation standard

What every route declares, and why — the rule Phase 2 of `docs/production-readiness-plan.md`
applies to every operation. Written 2026-08-28, when most lines of `docs/openapi-digest.txt`
still said `200:any`. The goal in one sentence: **the contract must be true, and specific
enough that the SPA's hand-written `src/types/index.ts` could be generated from it.**

## The envelope

`schemas/response.py` exports `SuccessResponse` and `ErrorResponse`, and nothing else is a
response shape (CLAUDE.md §8). `SuccessResponse` is generic over `data`: **controllers
construct it bare** — `SuccessResponse(status_code=200, message=…, data=…)`, which every
existing call site does and none of them change — and **routes declare it parameterised**,
`response_model=SuccessResponse[Thing]`. FastAPI dumps the returned model to a dict and
validates *that* against the parameterisation, so the two halves meet without a controller
ever importing a response model.

`ErrorResponse.data` stays `Any`; the reason is in its docstring. The four refusals every
authenticated route can produce are `COMMON_ERRORS` in `schemas/errors.py`, alongside
`VALIDATION_ERROR` for the 422 any route with a body or a validated parameter can answer.

Payload models live in `schemas/responses/`, one module per route group, named after the
group. **Name a model for the resource, not the endpoint** — `UserSummary`, not
`ListUsersResponse`: two endpoints return one resource at two depths far more often than two
things, and a name taken from a verb cannot be reused by the second one.

## Worked example — `GET /users`

Before — the route, and the digest line it generated. `200:any`, and an operation id a
generated client would name a method after:

```python
@router.get("")
def get_users(db: Session = Depends(get_db), page: int = Query(1, ge=1), limit: int = Query(0),
              sort_by: str = Query("first_name"), order: str = Query("asc"),
              search: str = Query("")):
    return get_users_info(db, page, limit, sort_by, order, search)
```
```
GET /users | get_users_users_get | … | 200:any,422:HTTPValidationError | body:-
```

After. `controllers/user_controller.get_users_info` really returns
`data={"records": [...], "total": total_users}`, where `total_users` is counted over the
filtered query *before* the page is cut (`services/user.fetch_user`) — read the controller
and the service, never infer from a name:

```python
# schemas/responses/users.py
class UserSummary(BaseModel):
    """One user as the list view needs it: identity, status and effective grants."""

    id: str = Field(description="Opaque user id, and the key for every `/users/{user_id}`.")
    email: str = Field(description="Sign-in address; also where OTP and reset mail goes.")
    first_name: str = Field(description="Given name, as entered.")
    last_name: str = Field(description="Family name, as entered.")
    roles: list[UserRoleSchema] = Field(description="Every role held, by id and name.")
    ...

class UserPage(BaseModel):
    """One page of users, in the requested sort order."""

    records: list[UserSummary] = Field(description="The users on this page.")
    total: int = Field(description="How many users match the search across all pages — not "
                                   "the length of `records`. See P-4 in the readiness plan.")
```
```python
# routes/user.py
@router.get(
    "",
    response_model=SuccessResponse[UserPage],
    responses={**COMMON_ERRORS, **VALIDATION_ERROR},
    summary="List users, paged and searchable",
    operation_id="list_users",
)
def get_users(db: Session = Depends(get_db), page: int = Query(1, ge=1), ...):
    """Sorted by `sort_by`/`order`; `limit=0` returns every match on one page."""
```

The route declares no `dependencies` because the real one declares none today — copy what
the route actually does, and change its behaviour in a separate commit if it is wrong.

Note what `total`'s description does: it says what the field **is** — the size of the result
set, which this endpoint got wrong until P-4 — not what its name suggests. A description that
restates a name is worth nothing; one that pins down the reading a name leaves open is a fix.

## The decorator, in this order

Path, `response_model`, `status_code` (only when it is not 200), `responses`,
`dependencies`, `summary`, `operation_id`. `tags` stay on the router.

* **`summary`** — one imperative line saying what the caller *gets*, not a restatement of the
  function name. It is the line ReDoc renders in its index.
* **`operation_id`** — explicit, `snake_case`, verb then resource, unique across the whole API:
  `list_users`, `delete_role`. The generated ones — say
  `delete_permission_permissions__permission_id__delete` — are what a generated client names its
  methods, which is the whole reason to replace them.
* **`description`** — FastAPI publishes the handler **docstring**. Several routes have good
  ones already; keep those, write the missing ones.
* **Every field gets `Field(description=…)`** — one sentence: what it means, and for a number
  what the extremes do. CLAUDE.md §3 applied to the contract, and the only per-field documentation
  an integrator ever sees. Every model gets a class docstring; every **request** model gets
  `json_schema_extra={"examples": [...]}` from the test fixtures, so an example that stops
  working fails something.
* **Never change a field's name, type or validation while documenting it** — that is a
  contract change wearing a documentation change's clothes, and the SPA is hand-typed to it.

## The validation trap

**A declared `response_model` is enforced on the success path and not on the refusals.**
FastAPI validates the handler's return value when it is a model or a dict, and skips it
entirely when it is a `JSONResponse`. Controllers here return `SuccessResponse` on success and
`JSONResponse` on refusals — so the moment you declare a model the 200 is under enforcement
and the 401/403/404/422 are not. A model that does not match what the controller really
returns turns a working 200 into a 500, and only running the endpoint finds that out. **Run
that endpoint's tests; if it has none, write one that calls it and asserts 200.**

## The digest moves in the same commit

`docs/openapi-digest.txt` is generated from `app.openapi()`, and
`tests/unit/test_openapi_contract.py` fails when it and the app disagree. Regenerate with
`make openapi` (or `python scripts/openapi_digest.py --write`) **in the same commit as the
change**, and read the diff (CLAUDE.md §15). Nothing else connects the two repositories: a field
renamed here compiles fine in the SPA, passes both suites, and breaks a screen in a browser. The
gate is not that updating it is hard — it is one command — but that a reviewer sees both diffs
side by side, and can therefore ask whether `src/types/index.ts` needs the same edit.

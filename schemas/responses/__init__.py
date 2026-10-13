"""Response payload models — the `data` half of `SuccessResponse[...]`, one module per group.

**A module per route group, named after the group it serves**, so that the owner of a
module is derivable from the route being changed and two people are never in the same file.
The groups are the ones the routers already draw — identity (`routes/auth.py`, `user.py`,
`roles.py`, `permissions.py`, `activity_logs.py`) and health — which is a division that
already exists rather than a new one to agree on. Where a group spans several routers, one
module covers the group.

A package rather than one file because it is written by several people at once and a single
module would be one merge conflict per group. **Nothing is re-exported here on purpose**:
an `__init__` that imported every module would break the whole package until the last one
landed, and would put every group's import graph in every other group's blast radius.
Import from the module — `from schemas.responses.users import UserSummary`.

Three rules, stated here because this is the file someone opens before adding a module.
The long form, with a worked before/after, is `docs/api-documentation-standard.md`.

1. **Name the resource, not the endpoint.** `UserSummary`, not `ListUsersResponse`.
   Two endpoints return the same resource at different depths far more often than they
   return two different things, and a name taken from the verb cannot be reused by the
   second one.

2. **Derive the shape from what the controller returns, then check it against the SPA.**
   `src/types/index.ts` in the SPA repository holds these shapes as the only consumer
   actually uses them, and reading it is faster than inferring them from a controller.
   Where the two disagree the controller is the truth and the disagreement is a finding
   worth reporting — it means a screen is rendering a field this service does not send.

3. **A declared model is a promise that has to be run.** FastAPI validates the return value
   against `response_model` when a handler returns a model or a dict, and skips it entirely
   when the handler returns a `JSONResponse`. Controllers here return `SuccessResponse` on
   success and `JSONResponse` on refusals, so declaring a model puts the *success* path
   under enforcement and leaves the refusals alone: a model that does not match what the
   controller really returns turns a working 200 into a 500, and only running the endpoint
   finds that out.
"""

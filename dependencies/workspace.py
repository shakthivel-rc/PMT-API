"""`workspace_access` — the tenancy gate every `/workspaces/{workspace_id}/…` route declares.

It answers two questions, in this order, and the order is the security property:

1. **Is the caller a member of this workspace?** If not, **404**, exactly as if the workspace
   did not exist. A 403 here would confirm to anyone holding a token that the id they guessed
   is a real tenant (CLAUDE.md §8).
2. **Does their role in it reach `min_role`?** If not, 403 — they are a member, so there is
   nothing left to disclose, and "you can see this but not change it" is the useful answer.

What it returns — a `WorkspaceAccess` — is the *only* place a controller gets a workspace id
from. Controllers never read `workspace_id` off the path themselves, so a route cannot be
written that checks membership of one workspace and then queries another.

Refusals are **raised**, never returned (CLAUDE.md §11; `tests/unit/test_authz_fail_closed.py`
explains what a returned refusal does). The dependency is typed and deliberately free of
controller imports so that it stays inside `make typecheck`.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from db.session import get_db
from models.workspace import WORKSPACE_ROLES, Workspace, WorkspaceMember
from schemas.response import ErrorResponse


@dataclass(frozen=True)
class WorkspaceAccess:
    """Who is asking, about which tenant, with what standing in it."""

    workspace_id: str
    user_id: str
    role: str

    def at_least(self, role: str) -> bool:
        return WORKSPACE_ROLES.index(self.role) >= WORKSPACE_ROLES.index(role)


def _refuse(status_code: int, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=ErrorResponse(status_code=status_code, message=message, data={}).model_dump(),
    )


def caller_id(request: Request) -> str:
    """The authenticated account id, or a raised 401.

    `JWTAuthMiddleware` has already rejected anything without a live session by the time a
    route runs, so the raise is unreachable today — and is here for the same reason as the
    one in `check_permissions`: a route registered under an exempt path would otherwise run
    with no caller at all.
    """
    claims = getattr(request.state, "user", None)
    subject = claims.get("sub") if isinstance(claims, dict) else None
    if not subject:
        raise _refuse(401, "Not authenticated")
    return str(subject)


def workspace_access(min_role: str = "guest"):
    """A dependency admitting members of the path's workspace whose role reaches `min_role`."""
    if min_role not in WORKSPACE_ROLES:
        # At import, not per request: a typo in a route declaration fails the boot.
        raise ValueError(f"unknown workspace role {min_role!r}")

    def dependency(
        workspace_id: str, request: Request, db: Session = Depends(get_db)
    ) -> WorkspaceAccess:
        user_id = caller_id(request)
        membership = (
            db.query(WorkspaceMember)
            .join(Workspace, Workspace.id == WorkspaceMember.workspace_id)
            .filter(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == user_id,
                Workspace.deleted == 0,
            )
            .first()
        )
        if membership is None:
            raise _refuse(404, "Workspace not found")
        access = WorkspaceAccess(
            workspace_id=workspace_id, user_id=user_id, role=str(membership.role)
        )
        if not access.at_least(min_role):
            raise _refuse(403, f"This needs the {min_role} role in this workspace, or higher.")
        return access

    return dependency


#: The three gates routes declare, built once at import. A module-level instance rather than
#: `Depends(workspace_access("member"))` inline: the factory call in a default argument is
#: what bugbear's B008 exists to catch, and one instance per role also means FastAPI's
#: per-request dependency cache can recognise the same gate declared twice.
as_guest = workspace_access("guest")
as_member = workspace_access("member")
as_admin = workspace_access("admin")

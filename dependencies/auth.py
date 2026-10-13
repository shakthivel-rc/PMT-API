from fastapi import Depends, HTTPException, Request
from models.user import User
from services.user import get_user_by_id
from sqlalchemy.orm import Session
from db.session import get_db
from schemas.response import ErrorResponse
from core.permissions import permission_is_satisfied

def permission_slugs(user: User) -> set[str]:
    """Every permission slug this user's roles grant.

    The role→permission traversal every permission check uses. Any new transport that needs
    to authorize a caller must call this rather than re-walking the relationships, because
    authorization logic that exists twice eventually disagrees with itself — the one place
    that already does, `routes/activity_logs._has_permission`, is the proof: it matches the
    literal slug and skips `permission_is_satisfied`'s implications (CLAUDE.md §11).
    """
    return {permission.machine_name for role in user.roles for permission in role.permissions}


def check_permissions(required_permissions: list[str]):
    """A dependency that admits a caller holding every one of `required_permissions`.

    One user load per declaration, and deliberately no request-scoped cache. `check_permissions`
    is a factory, so two `Depends(check_permissions([...]))` on one route are two distinct
    closures and FastAPI's per-request dependency cache — which keys on the callable — cannot
    collapse them: the route would load the user twice. Counted on 2026-09-28 by walking
    every `APIRoute` and reading its `dependant`, **no route declares two permission
    checks**, so a cache on `request.state` would save zero queries today while adding a
    window in which a permission change made by the handler is invisible to a later check in
    the same request. Revisit it with a measurement the day a route needs two checks; until
    then the eager load in `get_user_by_id` is the whole of the fix (P-2).
    """
    def role_checker(request: Request, db: Session = Depends(get_db)):
        user = getattr(request.state, "user", None)
        if not user:
            # RAISE. Never return, and never return from any other refusal added below.
            #
            # A FastAPI dependency that *returns* a refusal does not refuse: the return value
            # is injected as the resolved dependency and the handler runs anyway, with this
            # ErrorResponse bound where the `User` should be. This branch returned exactly
            # that until 2026-08-27 while every other refusal in the function raised.
            #
            # It was never reachable — `JWTAuthMiddleware` populates `request.state.user` for
            # everything outside `UNAUTHENTICATED_ROUTES`/`UNAUTHENTICATED_PREFIXES`, and no
            # exempt route declares a permission check — which is the point. One route
            # registered under an exempt prefix with this dependency on it makes the
            # fail-open live, and the person adding that route has no reason to open this
            # file. Same shape as F-02 in docs/hardening-audit.md.
            raise HTTPException(
                status_code=401,
                detail=ErrorResponse(
                    status_code=401,
                    message="Not authenticated",
                    data={},
                ).model_dump(),
            )

        user_id = user.get("sub")
        user = get_user_by_id(db, user_id)
        if not user:
            raise HTTPException(
                status_code=401,
                detail=ErrorResponse(
                    status_code=401,
                    message="Not authenticated",
                    data={},
                ).model_dump(),
            )
        user_permissions = permission_slugs(user)
        if all(permission_is_satisfied(user_permissions, permission)
               for permission in required_permissions):
            return user

        error_response = ErrorResponse(
            status="error",
            status_code=403,
            message="Insufficient permissions",
        )
        raise HTTPException(status_code=403, detail=error_response.model_dump())

    return role_checker

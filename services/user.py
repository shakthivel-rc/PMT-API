from sqlalchemy.orm import Session, selectinload
from models.user import User
from models.roles import Role
from models.user_roles import UserRole
from schemas.user import UserProfileResponse,UserProfileResponseWrapper, UserRoleSchema, UserPermissionSchema
from schemas.response import ErrorResponse
from sqlalchemy import or_, and_, func
from fastapi.responses import JSONResponse
from models.user_session import UserSession

def get_user_by_username(db: Session, username: str):
    return db.query(User).filter(User.username == username, User.deleted != 1).first()

def get_user_by_id(db: Session, user_id: str, include_deleted: bool = False):
    """The user behind an id, with the roles and permissions that authorize them loaded.

    **The soft-delete filter is defence in depth, not a fix for a reachable hole. Do not
    remove it as redundant — redundant is what it is for.** `get_user_by_username` above,
    `get_user_by_email` and `fetch_user` all carry `deleted != 1`; this one, which is the
    lookup `check_permissions` makes on *every authorized request*, was the only sibling that
    did not, and nothing said why. It was not exploitable: `DELETE /users/{id}` revokes the
    sessions and removes the role rows before it sets the flag, so the token fails
    `assert_session_is_live` in the middleware and 401s long before authorization is reached.
    The finding is that the authorization layer's safety was delegated in full to the deletion
    layer doing three things in the right order. Any other route to `deleted = 1` — a bulk
    operation, a migration, a support query, a second delete endpoint written later — leaves a
    live session pointing at a deleted account, and this is the one lookup positioned to catch
    it.

    `include_deleted=True` is for the callers that must *see* a soft-deleted row to answer
    correctly: `update_user_controller` and `delete_user_controller` report "already deleted"
    rather than "not found", and `tests/live/test_identity_rbac.py` pins the second of those
    as a 400. Everything else wants the default.

    **The roles and permissions are eager-loaded** (P-2). `permission_slugs` walks
    `user.roles` and then `role.permissions`, which lazy-loads one SELECT for the collection
    plus one per role. Counted with an `after_cursor_execute` listener around this dependency
    on 2026-08-27: **2 + N statements for a user holding N roles — 3 at one role, 5 at three,
    12 at ten — against a flat 3 with `selectinload`, whatever N is.** Users hold exactly one
    role today (see `assign_role_for_user` below), so the common case is 3 either way; what
    changed is that the cost stopped growing with a data shape the schema permits and the RBAC
    listing already renders. `fetch_user` at the bottom of this module has used these same two
    options since the hardening pass and explains the same trade for the listing path; the
    per-request path simply never adopted it.
    """
    query = db.query(User).options(selectinload(User.roles).selectinload(Role.permissions))
    if not include_deleted:
        query = query.filter(User.deleted != 1)
    return query.filter(User.id == user_id).first()

def fetch_user_profile(user_id: str, db: Session) -> UserProfileResponseWrapper:
    user_record = get_id_by_token(user_id, db)

    if not user_record:
        error_response = ErrorResponse(status_code=404, message="User not found")
        return JSONResponse(status_code=404, content=error_response.model_dump())  

    user_roles = [UserRoleSchema(role_id=role.id, role_name=role.name) for role in user_record.roles]
    user_permissions = [
        UserPermissionSchema(permission_name=permission.name, machine_name=permission.machine_name)
        for role in user_record.roles
        for permission in role.permissions
    ]
    user_profile = UserProfileResponse(
        id=user_record.id,
        username=user_record.username,
        first_name=user_record.first_name,
        last_name=user_record.last_name,
        email=user_record.email,
        organization=user_record.organization,
        department=user_record.department,
        created_at=user_record.created_at,
        updated_at=user_record.updated_at,
        roles=user_roles,
        permissions=user_permissions,
    )
    return UserProfileResponseWrapper(
        status="success",
        status_code=200,
        message="User profile fetched successfully",
        data=user_profile,
    )

def create_new_user(user: User, db: Session, token: str = ""):
    new_user = User(
                username=user.username,
                email=user.email,
                first_name=user.first_name,
                last_name=user.last_name,
                organization = user.organization,
                department = user.department,
                verify_token = token,
                deleted=0,  # Ensure user is active by default
                deleted_at=None 
                )
    db.add(new_user)
    db.flush()
    db.refresh(new_user)
    
    # Convert SQLAlchemy object to dictionary (excluding private attributes)
    user_dict = {key: value for key, value in vars(new_user).items() if not key.startswith("_")}
    return user_dict  
    
def get_user_by_email(db: Session, email: str):
    return db.query(User).filter(User.email == email, User.deleted != 1).first()        

def get_id_by_token(user_id, db: Session):
    """The profile endpoint's lookup. Deliberately the same lookup as `get_user_by_id`.

    It was a second, unfiltered copy of that query under a different name, which is how the
    two came to disagree: one grew a soft-delete filter and the other had no reason to. It
    should filter — `GET /users/profile` is an authenticated read of an identity, and a
    soft-deleted account answering it is the same hole SEC-2 describes — and delegating rather
    than repeating the clause is what stops them drifting again. `fetch_user_profile` above
    already turns the `None` into a 404, so the shape of the refusal is unchanged. It also
    picks up the eager load for free, which this endpoint needs more than most: it renders
    every role and every one of their permissions.

    The `Depends(get_db)` default this signature carried was never used — its one caller
    passes a session — and a FastAPI dependency marker on a service function is the drift
    CLAUDE.md §2 names.
    """
    return get_user_by_id(db, user_id)

def assign_role_for_user(user_id, role_id, db):
    """Replace this user's role. One row in, every previous row out — deliberately.

    `user_roles` is a many-to-many table and this function treats it as one-to-one, which
    reads as a bug and is not. The rest of the system agrees on one role per user:
    `UserSignupReqeust.role_id` and `UserUpdateRequest.role_id` are single strings,
    `services/user_role.add_roles_to_user` writes exactly one row at creation, and
    `controllers/user_controller.get_user` renders `user.roles[0]` as *the* user's role. The
    join table is many-to-many because roles↔permissions needed one and users↔roles was
    modelled the same way, not because a user was ever meant to hold two. Making this append
    instead would silently widen the grants of every account that has ever been edited, so the
    behaviour stays and the decision is written here instead of being rediscovered.
    (Decided 2026-08-27 while implementing Phase 4.4.)

    It no longer swallows failures. It used to `db.rollback()` and return a `JSONResponse`
    from inside a service, with `message="Error Occured : {e}"` — an unformatted f-string
    naming a variable that was never bound, so the message was literally that. The one caller,
    `update_user_controller`, ignores the return value, so the visible effect of a failed
    assignment was: the request's whole transaction rolled back behind the controller's back,
    the controller carried on, committed the remaining field updates in a fresh transaction,
    and answered **200 "User updated successfully"** for a role change that had not happened.
    Letting the exception out lets that controller's own `except` roll back once and report
    it, which is where HTTP shaping belongs (CLAUDE.md §2).
    """
    db.query(UserRole).filter(UserRole.user_id == user_id).delete()
    db.flush()
    db.add(UserRole(user_id=user_id, role_id=role_id))
    db.flush()

def delete_user_session(user_id: str, db: Session):
    return db.query(UserSession).filter(UserSession.user_id == user_id).delete()

def fetch_user(db: Session, skip, limit, order_by, search):
    # The caller renders every user's roles AND every role's permissions. Lazy-loading walks
    # that as one query per user plus one per role — 2N+1 for a page of N. selectinload
    # collapses it to three regardless of page size, which matters because this endpoint
    # defaults to no limit at all.
    query = (
        db.query(User)
        .options(selectinload(User.roles).selectinload(Role.permissions))
        .filter(User.deleted != 1)  # Ensure deleted users are excluded globally
    )

    if search:
        search_terms = search.strip().split()  
        num_words = len(search_terms)
        if num_words == 1:
            query = query.filter(
                or_(
                    User.first_name.ilike(f"%{search}%"),
                    User.last_name.ilike(f"%{search}%"),
                    User.email.ilike(f"%{search}%"),
                    User.username.ilike(f"%{search}%")

                )
            )

        elif num_words == 2:
            query = query.filter(
                and_(
                    User.first_name.ilike(f"%{search_terms[0]}%"),
                    User.last_name.ilike(f"%{search_terms[1]}%")
                )
            )

        elif num_words >= 3:
            first_name_part = f"{search_terms[0]} {search_terms[1]}"
            last_name_part = search_terms[-1]  # Last word maps to last_name

            query = query.filter(
                and_(
                    User.first_name.ilike(f"%{first_name_part}%"),
                    User.last_name.ilike(f"%{last_name_part}%")
                )
            )
    
    # Counted over the *filtered* query and before the limit, because `len(users)` is the size
    # of the page: every client that divided `total` by its page size to draw a pager got one
    # page, whatever the table held. Counting off `query` rather than off a fresh
    # `select(func.count()).select_from(User)` is the load-bearing part — `query` already
    # carries `deleted != 1` and whichever of the three search branches above ran, and a count
    # that misses those is wrong in a quieter direction than the bug it replaces.
    # `with_entities` loads no entity, so the `selectinload` options above do not fire for it:
    # one extra statement per request, measured 2026-08-27.
    total_users = query.with_entities(func.count(User.id)).scalar() or 0

    if limit != 0:
        users = query.order_by(order_by).offset(skip).limit(limit).all()
    else:
        users = query.order_by(order_by).offset(skip).all()
    return users,total_users

def user_role_deletion(db, user_id):
    return db.query(UserRole).filter(UserRole.user_id == user_id).delete()
        
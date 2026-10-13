"""What a soft delete takes away, and what the authorization walk costs to find out.

Four findings share this file because they share one situation: a row that has been marked
deleted, or a page that has been limited, and the decision made *afterwards* by code that
never looked.

* **SEC-2** — `get_user_by_id` is the lookup `check_permissions` makes on every authorized
  request, and it was the only sibling in `services/user.py` with no `deleted != 1` clause.
  The tests here reach the flag **without going through `DELETE /users/{id}`**, because that
  endpoint is not the problem: it revokes the sessions and drops the role rows before setting
  the flag, so its own callers were always safe. Every other route to `deleted = 1` is the
  scenario, and "some other route" is what a test has to imitate by writing the column.
* **SEC-3** — a role soft-deleted by any means kept granting every permission it held,
  because `User.roles` walked the join table without looking at `roles.deleted`.
* **P-2** — the same lookup lazy-loaded roles and then permissions per role, so the cost of
  authorizing a request grew with the number of roles the caller holds.
* **P-4** — `GET /users` reported the size of the page as the total, so a client drawing a
  pager from `total` always drew one page.

The query counts assert a *ceiling and a shape*, not a golden number: what must stay true is
that authorizing a request costs the same whether the caller holds one role or ten.
"""
import time

import jwt
import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

import db.session as db_session
from core.config import get_settings
from dependencies.auth import check_permissions, permission_slugs
from models.permissions import Permission
from models.role_permissions import RolePermission
from models.roles import Role
from models.user import User
from models.user_roles import UserRole
from models.user_session import UserSession, hash_session_token
from services.user import (
    assign_role_for_user,
    fetch_user,
    get_id_by_token,
    get_user_by_id,
)
from utils.password import hash_password

pytestmark = pytest.mark.integration


class _Request:
    """The one attribute `check_permissions` reads off a request before it queries."""

    class _State:
        pass

    def __init__(self, user_id: str | None):
        self.state = self._State()
        if user_id is not None:
            self.state.user = {"sub": user_id}


class _QueryCounter:
    """Statements issued against the test engine, counted with `after_cursor_execute`.

    Deliberately the same mechanism as `tests/integration/test_user_listing_queries.py` — a
    performance claim in this repository is a measurement or it is a decoration
    (CLAUDE.md §0), and one measuring device is easier to trust than two.
    """

    def __init__(self):
        self.statements: list[str] = []

    def __enter__(self):
        self._listener = lambda conn, cursor, statement, *a: self.statements.append(statement)
        event.listen(db_session.engine, "after_cursor_execute", self._listener)
        return self

    def __exit__(self, *exc):
        event.remove(db_session.engine, "after_cursor_execute", self._listener)

    @property
    def count(self) -> int:
        return len(self.statements)


def _make_user(db, user_id: str, first_name: str = "Soft", last_name: str = "Delete") -> User:
    user = User(
        id=user_id,
        username=user_id,
        email=f"{user_id}@example.com",
        first_name=first_name,
        last_name=last_name,
        organization="Acme",
        department="Ops",
        password=hash_password("Sup3r!Secret!Pass"),
        status=True,
        deleted=0,
    )
    db.add(user)
    db.flush()
    return user


def _grant(db, user_id: str, slug: str, role_name: str) -> Role:
    """One role holding one permission, assigned to `user_id`. Returns the role."""
    permission = Permission(name=role_name + " " + slug, machine_name=slug)
    role = Role(name=role_name)
    db.add_all([permission, role])
    db.flush()
    db.add_all([
        RolePermission(role_id=role.id, permission_id=permission.id),
        UserRole(user_id=user_id, role_id=role.id),
    ])
    db.flush()
    return role


def _session_token(db, user_id: str) -> str:
    settings = get_settings()
    token = jwt.encode(
        {"sub": user_id, "type": "access", "exp": int(time.time()) + 3600},
        settings.secret_key,
        algorithm=settings.algorithm,
    )
    # The row stores the digest, never the token (SEC-5) — `hash_session_token` is imported
    # rather than reimplemented so a change to what that column holds fails here loudly
    # instead of quietly never matching a row.
    db.add(UserSession(user_id=user_id, access_token_hash=hash_session_token(token)))
    db.flush()
    return token


# --- SEC-2 -------------------------------------------------------------------------------

def test_the_authorization_lookup_refuses_a_user_soft_deleted_by_any_other_route(db):
    """A `deleted = 1` written **outside** `DELETE /users/{id}` must still deauthorize.

    Would catch the removal of the `deleted != 1` clause from `get_user_by_id` — the lookup
    every permission-guarded request makes. The flag is set here by writing the column
    directly, which is the scenario and not a shortcut: the delete endpoint revokes the
    sessions and drops the role rows first, so a soft delete that arrives through it never
    reaches authorization at all. A bulk operation, a migration, a support query or a second
    delete endpoint written later reaches it every time, and before 2026-08-27 this was the
    one lookup in `services/user.py` positioned to notice and not looking.
    """
    _make_user(db, "sec2-user")
    _grant(db, "sec2-user", "manage_users", "SEC-2 Admin")
    db.commit()
    assert get_user_by_id(db, "sec2-user") is not None, "precondition: the user authorizes"

    db.query(User).filter(User.id == "sec2-user").update({"deleted": 1})
    db.commit()
    db.expire_all()

    assert get_user_by_id(db, "sec2-user") is None


def test_a_soft_deleted_user_is_refused_by_the_permission_dependency(db):
    """The same finding one layer up, stated as the outcome that matters.

    Would catch a `get_user_by_id` that filters but a `check_permissions` that has stopped
    calling it — the refusal, not the query, is the guarantee.
    """
    _make_user(db, "sec2-dep")
    _grant(db, "sec2-dep", "manage_users", "SEC-2 Dep Admin")
    db.query(User).filter(User.id == "sec2-dep").update({"deleted": 1})
    db.commit()
    db.expire_all()

    with pytest.raises(Exception) as raised:
        check_permissions(["manage_users"])(_Request("sec2-dep"), db)
    assert getattr(raised.value, "status_code", None) == 401


def test_the_soft_deleted_row_is_still_reachable_when_a_caller_asks_for_it(db):
    """`include_deleted=True` is the supported way to see a withdrawn account.

    Would catch a filter applied unconditionally. `update_user_controller` and
    `delete_user_controller` have to *see* the row to say "already deleted" rather than "not
    found" — `tests/live/test_identity_rbac.py` pins the second of those as a 400 — so the
    escape hatch has to exist and has to be explicit at the call site.
    """
    _make_user(db, "sec2-visible")
    db.query(User).filter(User.id == "sec2-visible").update({"deleted": 1})
    db.commit()
    db.expire_all()

    assert get_user_by_id(db, "sec2-visible") is None
    still_there = get_user_by_id(db, "sec2-visible", include_deleted=True)
    assert still_there is not None and still_there.deleted == 1


def test_the_profile_lookup_filters_soft_deletes_too(db):
    """`get_id_by_token` was the same query under another name, and it did not filter.

    Would catch the two lookups drifting apart again. It feeds `GET /users/profile`, an
    authenticated read of an identity: a soft-deleted account answering it is the same hole
    SEC-2 describes, reached through a different door. `fetch_user_profile` turns the `None`
    into a 404, so the refusal is already shaped correctly.
    """
    _make_user(db, "sec2-profile")
    db.query(User).filter(User.id == "sec2-profile").update({"deleted": 1})
    db.commit()
    db.expire_all()

    assert get_id_by_token("sec2-profile", db) is None


# --- SEC-3 -------------------------------------------------------------------------------

def test_a_soft_deleted_role_grants_nothing(db):
    """Withdrawing a role must withdraw its permissions, on the next request.

    Would catch `User.roles` walking `user_roles` without looking at `roles.deleted` — which
    it did until 2026-08-27, so a role retired from the RBAC screen kept granting every
    permission it held to every user it was assigned to, forever and silently. As with SEC-2
    the flag is written directly, because `DELETE /roles/{id}` refuses while any user still
    holds the role and then hard-deletes it: the endpoint is not the route by which a role
    becomes `deleted = 1`, and there is no other route today, which is precisely why nothing
    would have noticed.
    """
    _make_user(db, "sec3-user")
    role = _grant(db, "sec3-user", "manage_users", "SEC-3 Admin")
    db.commit()
    db.expire_all()
    assert "manage_users" in permission_slugs(get_user_by_id(db, "sec3-user"))

    db.query(Role).filter(Role.id == role.id).update({"deleted": 1})
    db.commit()
    db.expire_all()

    user = get_user_by_id(db, "sec3-user")
    assert user is not None, "the user is untouched; only the role was withdrawn"
    assert permission_slugs(user) == set()
    with pytest.raises(Exception) as raised:
        check_permissions(["manage_users"])(_Request("sec3-user"), db)
    assert getattr(raised.value, "status_code", None) == 403


def test_a_live_role_still_grants_after_a_sibling_is_withdrawn(db):
    """The other direction, because a filter that refuses everything also passes the test above.

    Would catch a `secondaryjoin` that drops rows it should keep — an `and_` written against
    the wrong column, or a `deleted != 1` that a NULL makes false.
    """
    _make_user(db, "sec3-two")
    withdrawn = _grant(db, "sec3-two", "audit:read", "SEC-3 Withdrawn")
    _grant(db, "sec3-two", "manage_users", "SEC-3 Kept")
    db.query(Role).filter(Role.id == withdrawn.id).update({"deleted": 1})
    db.commit()
    db.expire_all()

    slugs = permission_slugs(get_user_by_id(db, "sec3-two"))
    assert slugs == {"manage_users"}


# --- P-2 ---------------------------------------------------------------------------------

def test_authorizing_a_request_costs_the_same_at_one_role_and_at_many(db):
    """The N+1 guard on the request path every authorized call already pays.

    Would catch the eager load being dropped from `get_user_by_id`. Lazy-loading walks
    `user.roles` as one SELECT and then each role's permissions as one more: measured on
    2026-08-27 with this counter, **2 + N statements — 3 at one role, 5 at three, 12 at ten**
    — against a flat 3 with `selectinload`. The equality below is the assertion that matters;
    the ceiling is there so that "flat" cannot become "flat at eleven".

    `expire_all()` first because the seeding session is the one under test: without it the
    identity map answers from memory and the count measures nothing.
    """
    _make_user(db, "p2-one")
    _grant(db, "p2-one", "p2_one_slug", "P-2 One")
    _make_user(db, "p2-many")
    for index in range(6):
        _grant(db, "p2-many", f"p2_many_slug_{index}", f"P-2 Many {index}")
    db.commit()

    db.expire_all()
    with _QueryCounter() as one_role:
        assert permission_slugs(get_user_by_id(db, "p2-one")) == {"p2_one_slug"}

    db.expire_all()
    with _QueryCounter() as six_roles:
        assert len(permission_slugs(get_user_by_id(db, "p2-many"))) == 6

    assert six_roles.count == one_role.count, (
        f"authorization cost grew with role count: {one_role.count} statements for one role, "
        f"{six_roles.count} for six — the roles/permissions walk is lazy-loading again"
    )
    assert one_role.count <= 3, (
        f"{one_role.count} statements to authorize one request; the measured floor is 3 "
        "(the user, its roles, their permissions)"
    )


# --- P-4 ---------------------------------------------------------------------------------

def test_a_limited_page_reports_the_table_total_not_the_page_size(db):
    """`total` is what a client divides by its page size. It was the page size.

    Would catch `total_users = len(users)` coming back — computed after the limit, so a table
    of any size reported exactly one page and the console's pager never offered a second.
    """
    for index in range(7):
        _make_user(db, f"p4-user-{index}", first_name="Paged", last_name=str(index))
    db.commit()

    users, total = fetch_user(db, skip=0, limit=3, order_by=User.first_name.asc(), search="")

    assert len(users) == 3, "precondition: the page really is limited"
    assert total == 7


def test_the_total_counts_the_search_filter_and_not_the_whole_table(db):
    """A count that ignores the filter is wrong in a quieter direction than the bug it replaced.

    Would catch a `select(func.count())` built from the model instead of from the filtered
    query: the pager would then promise pages that the search can never fill. Two of the four
    users below match; the page holds one.
    """
    _make_user(db, "p4-match-0", first_name="Findable", last_name="Zero")
    _make_user(db, "p4-match-1", first_name="Findable", last_name="One")
    _make_user(db, "p4-other-0", first_name="Hidden", last_name="Zero")
    _make_user(db, "p4-other-1", first_name="Hidden", last_name="One")
    db.commit()

    users, total = fetch_user(db, skip=0, limit=1, order_by=User.first_name.asc(),
                              search="Findable")

    assert len(users) == 1
    assert total == 2


def test_the_total_excludes_soft_deleted_users(db):
    """The count and the page must describe the same set.

    Would catch a count that reaches past `deleted != 1` — the listing hides soft-deleted
    accounts, so a total that includes them is a pager promising rows that do not exist.
    """
    for index in range(4):
        _make_user(db, f"p4-mixed-{index}", first_name="Mixed", last_name=str(index))
    db.query(User).filter(User.id == "p4-mixed-0").update({"deleted": 1})
    db.commit()
    db.expire_all()

    users, total = fetch_user(db, skip=0, limit=2, order_by=User.first_name.asc(), search="")

    assert len(users) == 2
    assert total == 3


def test_the_users_endpoint_reports_the_total_over_the_wire(db, client):
    """The same assertion end to end, because `total` is a contract with the SPA.

    Would catch the count being computed correctly and then not reaching the response.
    """
    _make_user(db, "p4-admin", first_name="Admin", last_name="Zero")
    _grant(db, "p4-admin", "manage_users", "P-4 Admin")
    token = _session_token(db, "p4-admin")
    for index in range(5):
        _make_user(db, f"p4-wire-{index}", first_name="Wire", last_name=str(index))
    db.commit()

    response = client.get("/users?page=1&limit=2&sort_by=first_name&order=asc",
                          headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    assert len(payload["records"]) == 2
    assert payload["total"] == 6, "five seeded users plus the admin making the request"


# --- Phase 4.4 ---------------------------------------------------------------------------

def test_a_failed_role_assignment_is_raised_and_not_swallowed(db):
    """A service must not roll back its caller's transaction and hand back a JSONResponse.

    Would catch the old `except Exception: db.rollback(); return JSONResponse(401, ...)`. Its
    one caller, `update_user_controller`, ignores the return value, so a failed assignment
    rolled the request's whole transaction back behind the controller's back, the controller
    carried on, committed the remaining field updates in a fresh transaction, and answered
    **200 "User updated successfully"** for a role change that had not happened. The message
    it built was `"Error Occured : {e}"` — an unformatted f-string naming a variable that was
    never bound — so even the swallowed error said nothing.
    """
    _make_user(db, "p44-user")
    kept = _grant(db, "p44-user", "manage_users", "Phase 4.4 Kept")
    db.commit()

    with pytest.raises(IntegrityError):
        assign_role_for_user("p44-user", "no-such-role-id", db)

    db.rollback()
    assert [role.id for role in get_user_by_id(db, "p44-user").roles] == [kept.id], (
        "the caller rolled back, so the previous grant must still stand"
    )

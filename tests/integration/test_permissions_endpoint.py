"""Integration test for route-level authorization (check_permissions). An
authenticated user who lacks the required slug must be refused with 403 — a
regression here is a silent privilege escalation, not a crash.

Also the roles a fresh install starts with, because they decide who that user is on day
one: `Admin` holds `manage_users` and the whole slug catalogue, and `Member` — the role
self-registration assigns — holds nothing, so it is refused exactly like the user above.

And that the route dependency resolves a requirement through `permission_is_satisfied`
rather than by literal slug, which `tests/unit/test_permissions.py` cannot show: it tests
that function alone, and a `check_permissions` that stopped calling it would leave it green."""
import datetime
import time

import bcrypt
import jwt
import pytest

from core.config import get_settings
from models.user import User
from models.user_session import UserSession, hash_session_token
from tests.fixtures import auth, seed_user
from utils.password import hash_password

pytestmark = pytest.mark.integration


def _access_token(sub: str) -> str:
    settings = get_settings()
    payload = {"sub": sub, "type": "access", "exp": int(time.time()) + 3600}
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


def _seed_authenticated_user_without_roles(db, user_id="perm-user"):
    db.add(
        User(
            id=user_id,
            email="noperm@example.com",
            username="noperm",
            first_name="No",
            last_name="Perm",
            organization="Org",
            department="Dept",
            password=hash_password("Abcdef123!@#"),
            status=True,
            deleted=0,
        )
    )
    token = _access_token(user_id)
    db.add(UserSession(user_id=user_id, access_token_hash=hash_session_token(token)))
    db.commit()
    return token


def test_manage_users_route_requires_the_permission(client, db):
    token = _seed_authenticated_user_without_roles(db)
    # DELETE /users/{id} is gated by check_permissions(["manage_users"]); the user has no roles.
    resp = client.delete("/users/some-id", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403
    # The envelope is flat now, not nested under `detail`.
    #
    # `dependencies/auth.check_permissions` raises `HTTPException(403, detail=<an ErrorResponse
    # dict>)`, and FastAPI's default handler wraps whatever `detail` holds in ANOTHER object — so
    # the body was `{"detail": {"status": ..., "message": ...}}`. Nothing could read that:
    # `src/api/client.ts` looks at `errorBody.message`, found nothing there, and showed "Request
    # failed with status 403" instead of the sentence the server had gone to the trouble of
    # writing. It also broke the rule in CLAUDE.md §8 that every response is a `SuccessResponse`
    # or an `ErrorResponse`.
    #
    # `core/errors.py` (SEC-8) unwraps a `detail` that is already an envelope rather than
    # enveloping it twice. Asserting the flat shape here is what stops it regressing.
    body = resp.json()
    assert body["message"] == "Insufficient permissions"
    assert body["status"] == "error" and body["status_code"] == 403


def test_manage_users_route_rejects_anonymous_callers(client):
    # No token → stopped at the middleware before authorization even runs.
    resp = client.delete("/users/some-id")
    assert resp.status_code == 401


def _seed_the_default_accounts(db, monkeypatch, capsys) -> tuple[User, User]:
    """Run the real seeder against the test database and return (superadmin, member).

    The seeder opens its own session from a `SessionLocal` it captured at import, so it is
    re-pointed at the one `tests/conftest.py` installed. It also catches its own errors and
    prints them rather than raising, so its output is what a missing row is reported with.
    """
    import seeders.user_seeder as seeder
    from db.session import SessionLocal

    monkeypatch.setattr(seeder, "SessionLocal", SessionLocal)
    monkeypatch.setenv("SUPERADMIN_PASSWORD", "Seed!Passw0rd-7c1e")
    monkeypatch.delenv("SUPERADMIN_EMAIL", raising=False)
    monkeypatch.delenv("SEED_MEMBER_EMAIL", raising=False)
    seeder.seed_database()
    output = capsys.readouterr().out

    admin = db.query(User).filter(User.email == "superadmin@base-project.local").first()
    member = db.query(User).filter(User.email == "member@base-project.local").first()
    assert admin is not None and member is not None, f"the seeder did not seed: {output}"
    return admin, member


class TestTheSeededRoles:
    def test_admin_holds_the_catalogue_and_member_holds_nothing(self, db, monkeypatch, capsys):
        from core.permissions import TARGET_PERMISSION_CATALOG
        from dependencies.auth import permission_slugs

        admin, member = _seed_the_default_accounts(db, monkeypatch, capsys)

        assert [role.name for role in admin.roles] == ["Admin"]
        assert [role.name for role in member.roles] == ["Member"]

        catalogue = {slug for _, slug in TARGET_PERMISSION_CATALOG}
        assert permission_slugs(admin) == catalogue | {"manage_users"}
        assert permission_slugs(member) == set()

    def test_a_member_is_refused_what_manage_users_guards(self, client, db, monkeypatch,
                                                          capsys):
        """The seeded member account holds `Member`, the role self-registration hands out
        (`TestSelfRegistration` below); this is what that role can do."""
        _, member = _seed_the_default_accounts(db, monkeypatch, capsys)
        token = _access_token(member.id)
        db.add(UserSession(user_id=member.id, access_token_hash=hash_session_token(token)))
        db.commit()

        resp = client.delete("/users/some-id", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403


class TestSelfRegistration:
    def test_a_completed_self_registration_holds_member_and_nothing_else(
        self, client, db, monkeypatch, capsys
    ):
        """The last registration step grants `Member` — by name, a string the controller and
        the seeder each spell out, so a rename on either side leaves self-registered accounts
        with no role at all and nothing fails. This is the test that fails instead.

        The pending row is built the way `POST /user/register` and `/user/verify-otp` leave
        it (unverified, no password, a bcrypt-hashed OTP issued just now) rather than by
        driving them, because they send email; this step sends none.
        """
        from dependencies.auth import permission_slugs

        _seed_the_default_accounts(db, monkeypatch, capsys)
        otp = "482913"
        db.add(User(
            id="self-registered",
            email="newcomer@example.com",
            username="newcomer",
            first_name="New",
            last_name="Comer",
            organization="Org",
            department="Dept",
            status=False,
            deleted=0,
            # Cost 4, the minimum: the controller only checks the hash, it never makes one.
            otp_code=bcrypt.hashpw(otp.encode(), bcrypt.gensalt(rounds=4)).decode(),
            otp_created_at=datetime.datetime.now(datetime.UTC),
        ))
        db.commit()

        password = "Newcomer!Passw0rd"
        resp = client.post("/user/set-password-registration", json={
            "email": "newcomer@example.com",
            "otp": otp,
            "new_password": password,
            "confirm_password": password,
        })
        assert resp.status_code == 201, resp.json()

        db.expire_all()
        user = db.query(User).filter(User.id == "self-registered").one()
        assert [role.name for role in user.roles] == ["Member"]
        assert permission_slugs(user) == set()


class TestCoarsePermissionImplications:
    """Every guarded route asks for the coarse `manage_users`, and none asks for a fine slug,
    so of `permission_is_satisfied`'s two implication branches only the reverse one is
    reachable through them: a role holding *every* slug `manage_users` implies satisfies it.
    README, CLAUDE.md §11 and the Permissions tag all promise that at the route level."""

    def test_every_implied_slug_together_satisfies_a_manage_users_route(self, client, db):
        from core.permissions import LEGACY_PERMISSION_IMPLICATIONS

        implied = LEGACY_PERMISSION_IMPLICATIONS["manage_users"]
        token = seed_user(db, "fine-grained-admin", implied)
        seed_user(db, "target", ())

        resp = client.delete("/users/target", headers=auth(token))
        assert resp.status_code == 200, resp.json()

    def test_all_but_one_implied_slug_is_still_a_403(self, client, db):
        from core.permissions import LEGACY_PERMISSION_IMPLICATIONS

        implied = sorted(LEGACY_PERMISSION_IMPLICATIONS["manage_users"])
        token = seed_user(db, "almost-admin", implied[1:])
        seed_user(db, "target", ())

        resp = client.delete("/users/target", headers=auth(token))
        assert resp.status_code == 403

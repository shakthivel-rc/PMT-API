"""The two links the API emails must open a screen the SPA has, and must work when followed.

Both were dead until 2026-09-29. The reset email pointed at `/reset-password/<token>` and the
invite email at `/verify-account/<token>`; the SPA reads a token only from `?token=` and had
no `/verify-account` route at all, so both fell through to the catch-all and landed on the
sign-in screen. Nothing noticed because no test ever followed a link out of an email: the
endpoints behind them were each tested with a token the test minted itself, which proves the
endpoint and says nothing about the link.

So these tests go the whole way round, the way a person does: trigger the email, take the
link out of the HTML body, check it opens the SPA screen, then hand its token back to the API
exactly as that screen does (in the JSON body of the POST) and check the password actually
changes.
"""
import datetime
import itertools
import os
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from models.roles import Role
from models.user import User
from tests.fixtures import auth, seed_user
from utils.token import INVITE_LINK_PATH, RESET_LINK_PATH

pytestmark = pytest.mark.integration

APP_BASE_URL = "https://app.example.com"
HREF = re.compile(r'href="([^"]+)"')
NEW_PASSWORD = "Sup3r!Secret!Pass"
SPA_APP = Path(__file__).resolve().parents[2].parent / "pmt-app" / "src" / "App.tsx"
#: One admin (and one username) per `_invite` call: `seed_user` also creates a role named
#: after the user, and role names are unique.
_ADMINS = itertools.count()


@pytest.fixture()
def outbox(monkeypatch):
    """Every email the controllers send, captured instead of delivered.

    `APP_BASE_URL` carries a trailing slash on purpose: it is the natural way to write a
    URL in an env file, and it must not turn into `//reset-password`.
    """
    sent = []

    def capture(recipient_email, email_template):
        sent.append((recipient_email, email_template.body))
        return {"status": True, "message": "captured"}

    monkeypatch.setattr("controllers.auth_controller.send_email", capture)
    monkeypatch.setattr("controllers.user_controller.send_email", capture)
    monkeypatch.setenv("APP_BASE_URL", APP_BASE_URL + "/")
    return sent


@pytest.fixture()
def far_east_host():
    """A host clock fourteen hours ahead of UTC, for as long as one test runs.

    Code that mixes local and UTC time is correct on a UTC machine, which is every CI
    runner, and wrong everywhere else. Moving the clock makes such a test fail wherever it
    runs, rather than only on a developer laptop in the wrong timezone.
    """
    saved = os.environ.get("TZ")
    os.environ["TZ"] = "Etc/GMT-14"  # POSIX signs are inverted: this is UTC+14
    time.tzset()
    yield
    if saved is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = saved
    time.tzset()


def _invite(client, db, email: str) -> dict:
    """Create a user from the admin console, as `POST /users` does, and return the response."""
    n = next(_ADMINS)
    admin = seed_user(db, f"inviter-{n}", ["manage_users"])
    role = db.query(Role).filter(Role.name == "Member").first()
    if role is None:
        role = Role(name="Member")
        db.add(role)
        db.commit()
    # A distinct username every call, so a duplicate *email* is what a second call tests —
    # the username check runs first and would otherwise answer for it.
    local = f'{email.split("@")[0]}-{n}'
    return client.post(
        "/users",
        json={
            "email": email,
            "first_name": "Invited",
            "last_name": "Person",
            "username": local,
            "organization": "Acme",
            "department": "QA",
            "role_id": role.id,
        },
        headers=auth(admin),
    )


def _token_from_link(outbox, recipient: str, spa_path: str) -> str:
    [body] = [body for to, body in outbox if to == recipient]
    [href] = HREF.findall(body)
    link = urlsplit(href)
    assert f"{link.scheme}://{link.netloc}" == APP_BASE_URL
    assert link.path == spa_path, f"the email links to {link.path}, the SPA screen is {spa_path}"
    query = parse_qs(link.query, strict_parsing=True)
    assert list(query) == ["token"], f"expected only ?token=, got {link.query!r}"
    [token] = query["token"]
    return token


def test_the_reset_link_opens_the_reset_screen_and_its_token_resets_the_password(
        client, db, outbox):
    seed_user(db, "forgetful", [])

    response = client.post("/user/forgot-password", json={"email": "forgetful@example.com"})
    assert response.status_code == 200, response.text

    token = _token_from_link(outbox, "forgetful@example.com", RESET_LINK_PATH)
    checked = client.get("/user/verify-forgot-password-token", params={"token": token})
    assert checked.status_code == 200, checked.text

    reset = client.post(
        "/user/reset-password",
        json={"token": token, "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD},
    )
    assert reset.status_code == 201, reset.text

    login = client.post(
        "/user/login", json={"username": "forgetful@example.com", "password": NEW_PASSWORD})
    assert login.status_code == 200, "the reset reported success but the new password fails"


def test_the_invite_link_opens_the_invite_screen_and_its_token_activates_the_account(
        client, db, outbox):
    created = _invite(client, db, "invitee@example.com")
    assert created.status_code == 201, created.text

    token = _token_from_link(outbox, "invitee@example.com", INVITE_LINK_PATH)
    checked = client.get("/user/verify-token", params={"token": token})
    assert checked.status_code == 200, checked.text

    activated = client.post(
        "/user/set-password",
        json={"token": token, "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD},
    )
    assert activated.status_code == 201, activated.text

    db.expire_all()
    invitee = db.query(User).filter(User.email == "invitee@example.com").one()
    assert invitee.status, "the invite was accepted but the account is still inactive"
    assert invitee.verify_token is None, "an accepted invite link must not work twice"

    login = client.post(
        "/user/login", json={"username": "invitee@example.com", "password": NEW_PASSWORD})
    assert login.status_code == 200


@pytest.mark.parametrize("path", ["/user/set-password", "/user/reset-password"])
def test_the_one_time_token_is_not_accepted_in_the_url(client, path):
    """The POSTs take the token in the body only (SEC-4): a token in a URL is a token in every
    access log and proxy log the request passes through. A client still sending `?token=`
    gets a 422 naming the missing body field, not a silent success that logs a credential."""
    response = client.post(
        path,
        params={"token": "anything"},
        json={"new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD},
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("age_hours, expected", [(47, 200), (49, 400)])
def test_an_invite_lasts_48_hours_whatever_the_host_timezone(
        client, db, outbox, far_east_host, age_hours, expected):
    """`created_at` is UTC; the expiry check compared it with local time until 2026-09-29, so
    on this UTC+14 clock a 47-hour-old invite read as 61 hours old and was refused."""
    assert _invite(client, db, "patient@example.com").status_code == 201
    token = _token_from_link(outbox, "patient@example.com", INVITE_LINK_PATH)

    invitee = db.query(User).filter(User.email == "patient@example.com").one()
    utc_now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    invitee.created_at = utc_now - datetime.timedelta(hours=age_hours)
    db.commit()

    response = client.get("/user/verify-token", params={"token": token})
    assert response.status_code == expected, response.text


def test_inviting_an_email_that_already_exists_is_a_400_not_a_500(client, db, outbox):
    """`users.email` is unique across soft-deleted rows too, so a duplicate used to reach the
    INSERT and surface as an unhandled IntegrityError."""
    assert _invite(client, db, "twice@example.com").status_code == 201
    again = _invite(client, db, "twice@example.com")
    assert again.status_code == 400, again.text
    assert "already exists" in again.json()["message"]


@pytest.mark.parametrize("spa_path", [RESET_LINK_PATH, INVITE_LINK_PATH])
def test_the_spa_routes_the_paths_the_api_emails(spa_path):
    """The other half of the contract, when the SPA is checked out beside this repository.

    Only a cross-repository check can catch one side renaming its half: the API tests above
    would stay green against a path the SPA no longer serves, and the SPA's own tests never
    see what the API sends.
    """
    if not SPA_APP.exists():
        pytest.skip("pmt-app is not checked out beside this repository")
    app_tsx = SPA_APP.read_text()
    route = f'path="{spa_path.lstrip("/")}"'
    assert route in app_tsx, f"src/App.tsx declares no <Route {route}>"
    # And it reads the token from where the link puts it.
    assert "params.get('token')" in app_tsx, "src/App.tsx no longer reads ?token= from the URL"

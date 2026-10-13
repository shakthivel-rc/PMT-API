"""The request-body ceiling, and the production configuration guards added beside it.

Two subjects in one file, and the second one is here under protest: the four guards in
`TestProductionConfigGuards` belong in `tests/unit/test_config.py`, next to
`TestProductionCorsGuard`, which is the test they are modelled on. They are here because
that file was being edited by another change at the same time as this one. **Move them when
the two land**; nothing about them depends on this file.

## The ceiling

Nothing bounded a request body before `BodySizeLimitMiddleware`. Starlette buffers the whole
body before a handler sees it and the rate limiter counts requests rather than bytes, so one
client posting 200 MB of JSON cost the process 200 MB and one unit of a 300-per-minute
budget.

Both checks are pinned below, because a limit that reads only `Content-Length` binds only
the clients that were honest about their size: a chunked request carries no `Content-Length`
at all, and that is one line of code away from being the shape every abusive client uses.
"""
import base64
import logging
import os
from contextlib import contextmanager

import pytest
from fastapi import Body, FastAPI
from fastapi.testclient import TestClient

from core.config import get_settings
from core.errors import install_error_handlers
from core.middleware import BodySizeLimitMiddleware

pytestmark = pytest.mark.unit

ORIGIN = "http://localhost:3000"

#: Small enough that the assertions read as sizes rather than as arithmetic. The shipped
#: default (4 MiB) is exercised against the real app below.
LIMIT = 64


def _limited(max_bytes: int = LIMIT) -> TestClient:
    app = FastAPI()
    install_error_handlers(app)

    @app.post("/echo")
    def echo(payload: dict = Body(default=None)):
        return {"fields": len(payload or {})}

    app.add_middleware(BodySizeLimitMiddleware, max_bytes=max_bytes)
    return TestClient(app)


def _oversized_json(size: int = 200) -> bytes:
    return b'{"a":"' + b"x" * size + b'"}'


def _chunks(size: int = 200):
    """An iterator body, which httpx sends with no `Content-Length` at all."""
    yield b'{"a":"'
    yield b"x" * size
    yield b'"}'


class TestTheDeclaredLengthIsRefusedAtTheDoor:
    def test_a_body_over_the_ceiling_is_refused(self):
        response = _limited().post("/echo", content=_oversized_json(),
                                   headers={"Content-Type": "application/json"})

        assert response.status_code == 413

    def test_the_refusal_is_the_envelope_and_names_the_ceiling(self):
        """"Too large" with no number leaves the caller bisecting their own payload to find
        out what this service will take."""
        body = _limited().post("/echo", content=_oversized_json(),
                               headers={"Content-Type": "application/json"}).json()

        assert body["status"] == "error" and body["status_code"] == 413
        assert str(LIMIT) in body["message"]
        assert body["data"]["max_bytes"] == LIMIT

    def test_a_body_under_the_ceiling_is_untouched(self):
        assert _limited().post("/echo", json={"a": 1}).status_code == 200

    def test_a_ceiling_of_zero_disables_the_check(self):
        """The same convention as `RATE_LIMIT_*_PER_MINUTE=0`: an operator turning a limit
        off should not have to discover that this one spells it differently."""
        response = _limited(max_bytes=0).post("/echo", content=_oversized_json(),
                                              headers={"Content-Type": "application/json"})

        assert response.status_code == 200


class TestABodyWithNoDeclaredLengthIsCountedAsItArrives:
    """The half that makes this a limit rather than a request.

    `Content-Length` is caller-supplied. A chunked upload carries none, and one that carries
    an understated value is a header edit away. If only the header were checked, the ceiling
    would apply to exactly the clients that did not need it.
    """

    def test_a_chunked_body_over_the_ceiling_is_still_refused(self):
        response = _limited().post("/echo", content=_chunks(),
                                   headers={"Content-Type": "application/json"})

        assert response.status_code == 413
        assert response.json()["status_code"] == 413

    def test_a_chunked_body_under_the_ceiling_is_untouched(self):
        response = _limited(max_bytes=4096).post("/echo", content=_chunks(),
                                                 headers={"Content-Type": "application/json"})

        assert response.status_code == 200

    def test_the_log_line_says_which_check_fired(self, caplog):
        """A stream of `declared: false` refusals is a client sending chunked, which is
        worth knowing before somebody raises the limit to make them stop."""
        with caplog.at_level(logging.WARNING, logger="base_project.request"):
            _limited().post("/echo", content=_oversized_json(),
                            headers={"Content-Type": "application/json"})
            _limited().post("/echo", content=_chunks(),
                            headers={"Content-Type": "application/json"})

        refusals = [r for r in caplog.records if r.getMessage() == "request_body_too_large"]
        assert [r.extra_fields["declared"] for r in refusals] == [True, False]
        assert all(r.extra_fields["limit_bytes"] == LIMIT for r in refusals)


class TestTheShippedCeilingIsWiredIntoTheRealApp:
    """Against `main.app`, because a middleware that is correct and unregistered is a
    middleware that does nothing."""

    def test_a_body_one_byte_over_the_default_is_refused(self, client):
        limit = get_settings().max_request_body_bytes
        response = client.post("/user/login", content=b"x" * (limit + 1),
                               headers={"Content-Type": "application/json", "Origin": ORIGIN})

        assert response.status_code == 413

    def test_the_413_carries_the_same_headers_as_every_other_refusal(self, client):
        """A 413 the SPA cannot read is indistinguishable from the upload dying — the same
        reason CORS is outermost for the 401 (`main.py`)."""
        limit = get_settings().max_request_body_bytes
        response = client.post("/user/login", content=b"x" * (limit + 1),
                               headers={"Content-Type": "application/json", "Origin": ORIGIN})

        assert response.headers.get("access-control-allow-origin") == ORIGIN
        assert "default-src 'none'" in response.headers.get("content-security-policy", "")
        assert response.headers.get("x-request-id")


@contextmanager
def _production_env(monkeypatch, **overrides):
    """A production environment in which every guard is satisfied except the one under test.

    Each test overrides exactly one variable, so a failure names the guard that fired rather
    than whichever one happened to be checked first.
    """
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example.com")
    monkeypatch.setenv("DATABASE_URL", "mysql+pymysql://app:pw@db:3306/base_project")
    # Generated, never a literal: `scripts/check_staged_secrets.py` refuses a commit
    # containing anything shaped like a Fernet key, and it is right to.
    monkeypatch.setenv("SECRET_KEY", base64.urlsafe_b64encode(os.urandom(32)).decode())
    monkeypatch.delenv("SUPERADMIN_PASSWORD", raising=False)
    for name, value in overrides.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    try:
        yield
    finally:
        # Restore the cache so later tests rebuild from the (restored) test environment.
        get_settings.cache_clear()


class TestProductionConfigGuards:
    """The four checks added beside the one that already refuses `CORS_ORIGINS=*` in
    production. Each one is a value a deployment can ship today, that starts cleanly, and
    that is wrong in a way nothing reports.

    NOTE: these belong in `tests/unit/test_config.py`. See this module's docstring.
    """

    @pytest.mark.parametrize("weak", ["changeme", "changeme" * 4, "0" * 44])
    def test_a_weak_secret_key_is_refused_in_production(self, monkeypatch, weak):
        """`SECRET_KEY` signs every JWT, so its strength is the strength of every session.

        The three cases are the three ways this ships: too short, long enough but one word
        repeated (which a length-only floor would pass), and one character repeated to the
        exact length of a real Fernet key.
        """
        with _production_env(monkeypatch, SECRET_KEY=weak):
            with pytest.raises(RuntimeError, match="SECRET_KEY"):
                get_settings()

    def test_a_real_fernet_key_is_accepted(self, monkeypatch):
        """The floor is measured against what `scripts/bootstrap.sh` generates. A guard that
        refused the key the documented setup produces would be worse than no guard: the
        first thing it teaches is to work around it."""
        with _production_env(monkeypatch):
            assert get_settings().is_production is True

    def test_the_refusal_does_not_echo_the_key(self, monkeypatch):
        """An error that quotes a secret writes it to the log of every failed start."""
        with _production_env(monkeypatch, SECRET_KEY="changeme-changeme-changeme"):
            with pytest.raises(RuntimeError) as caught:
                get_settings()
        assert "changeme" not in str(caught.value)

    @pytest.mark.parametrize("algorithm", ["none", "None", "RS256", "HS128", ""])
    def test_an_algorithm_outside_the_allow_list_is_refused(self, monkeypatch, algorithm):
        """`none` names the *unsigned* JWT: every caller mints their own claims and nothing
        is verified. It is a legal value of this variable and it is one typo away.

        `RS256` is refused for a different reason — it is asymmetric, `SECRET_KEY` is one
        shared string, and there is nowhere here to put the second half of the pair.
        """
        monkeypatch.setenv("ALGORITHM", algorithm)
        get_settings.cache_clear()
        try:
            with pytest.raises(RuntimeError, match="ALGORITHM"):
                get_settings()
        finally:
            get_settings.cache_clear()

    def test_the_algorithm_guard_fires_outside_production_too(self, monkeypatch):
        """Deliberately not scoped to production, unlike its neighbours. A staging
        deployment with `ALGORITHM=none` is exactly as bypassed as a production one and
        holds real user rows — and a guard that only fires at deploy time is one nobody
        meets until the deploy."""
        monkeypatch.setenv("ENVIRONMENT", "development")
        monkeypatch.setenv("ALGORITHM", "none")
        get_settings.cache_clear()
        try:
            with pytest.raises(RuntimeError, match="ALGORITHM"):
                get_settings()
        finally:
            get_settings.cache_clear()

    @pytest.mark.parametrize("algorithm", ["HS256", "HS384", "HS512"])
    def test_the_symmetric_algorithms_are_accepted(self, monkeypatch, algorithm):
        monkeypatch.setenv("ALGORITHM", algorithm)
        get_settings.cache_clear()
        try:
            assert get_settings().algorithm == algorithm
        finally:
            get_settings.cache_clear()

    @pytest.mark.parametrize("url", ["sqlite://", "sqlite:///./app.db",
                                     "SQLite:///data/app.db"])
    def test_sqlite_is_refused_in_production(self, monkeypatch, url):
        """One file, one writer, no network — against a 40-connection pool. The first thing
        real load produces is `database is locked`, and the migrations are written for MySQL
        besides."""
        with _production_env(monkeypatch, DATABASE_URL=url):
            with pytest.raises(RuntimeError, match="SQLite"):
                get_settings()

    def test_sqlite_is_fine_everywhere_else(self, monkeypatch):
        """The suite runs on in-memory SQLite and must keep doing so with zero
        infrastructure (CLAUDE.md §6). That is why this guard is scoped to production
        rather than enforced at the engine."""
        monkeypatch.setenv("ENVIRONMENT", "development")
        monkeypatch.setenv("DATABASE_URL", "sqlite://")
        get_settings.cache_clear()
        try:
            assert get_settings().database_url == "sqlite://"
        finally:
            get_settings.cache_clear()

    @pytest.mark.parametrize("password", ["changeme", "Password123", "admin",
                                          "<your-password>", "short1!A"])
    def test_a_placeholder_superadmin_password_is_refused_in_production(self, monkeypatch,
                                                                        password):
        """The seeded superadmin holds every permission in the catalogue and its sign-in form
        is public. `seeders/user_seeder.py` has no hard-coded default — it raises without the
        variable — so what "still the default" means in practice is whatever somebody typed
        to get past that error, including anything under `PASSWORD_MIN_LENGTH`, which every
        ordinary account must clear."""
        with _production_env(monkeypatch, SUPERADMIN_PASSWORD=password):
            with pytest.raises(RuntimeError, match="SUPERADMIN_PASSWORD"):
                get_settings()

    def test_a_chosen_superadmin_password_is_accepted(self, monkeypatch):
        with _production_env(monkeypatch, SUPERADMIN_PASSWORD="Nx!7Qw3zTr9pAa1"):
            assert get_settings().is_production is True

    def test_an_absent_superadmin_password_is_not_a_failure(self, monkeypatch):
        """A deployment that does not seed never sets it, and refusing to boot over a
        variable that is only read by `python -m seeders.user_seeder` would break "boots
        degraded, never fails"."""
        with _production_env(monkeypatch, SUPERADMIN_PASSWORD=None):
            assert get_settings().is_production is True

"""Integration tests for the health endpoints — the only machine-checkable
assertions the app exposes. They also validate the TestClient harness + the
{status, status_code, message, data} envelope end-to-end."""
import pytest

pytestmark = pytest.mark.integration


def test_live_returns_the_success_envelope(client):
    resp = client.get("/api/v1/health/live")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["data"] == {"status": "live"}


def test_ready_checks_the_database(client):
    resp = client.get("/api/v1/health/ready")
    assert resp.status_code == 200
    assert resp.json()["data"] == {"database": "ok"}


def test_dependencies_reports_database_ok(client):
    """The healthy half of the degraded flag. A header stuck at "true" would page whoever
    alerts on it forever, and only this test would notice."""
    resp = client.get("/api/v1/health/dependencies")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"] == {"database": "ok", "degraded": False}
    assert resp.headers["X-Health-Degraded"] == "false"
    assert body["message"] == "Dependencies are healthy"


def test_dependencies_is_a_diagnostic_and_answers_even_when_degraded(client):
    """200 with `degraded: true`, not a 5xx. A diagnostic that cannot answer is useless.

    Degraded through the database, the one dependency the base project has: a session
    whose every query raises the error a dropped connection raises.
    """
    from sqlalchemy.exc import OperationalError

    from db.session import get_db
    from main import app

    class _DownSession:
        def execute(self, *_args, **_kwargs):
            raise OperationalError("SELECT 1", {}, Exception("down"))

    app.dependency_overrides[get_db] = lambda: _DownSession()
    try:
        resp = client.get("/api/v1/health/dependencies")
    finally:
        # The app is shared by the whole suite; a leaked override would hand this broken
        # session to every later test that asks for the database.
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    assert resp.headers["X-Health-Degraded"] == "true"
    body = resp.json()
    assert body["data"]["degraded"] is True
    # The exception's class, never its text: a driver error quotes the DSN, password and all.
    assert body["data"]["database"] == "error: OperationalError"
    assert body["message"] == "Dependencies degraded"


def test_readiness_and_dependencies_are_no_longer_the_same_check(client):
    """They answer different questions: 'can this instance serve' and 'what is its state'.
    They were the same `SELECT 1` twice, which meant neither was doing its job."""
    ready = client.get("/api/v1/health/ready").json()["data"]
    deps = client.get("/api/v1/health/dependencies").json()["data"]
    assert ready == {"database": "ok"}
    assert set(deps) > set(ready)


def test_root_is_liveness_only(client):
    # "/" is public in FastAPI but still passes through the middleware stack.
    resp = client.get("/")
    # It requires auth (it is NOT in UNAUTHENTICATED_ROUTES), so an anonymous call is 401.
    assert resp.status_code == 401

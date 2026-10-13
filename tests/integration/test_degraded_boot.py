"""Boots with the minimum.

The constraint this file exists to hold: the service must start with only `DATABASE_URL`
and `SECRET_KEY` set. Anything optional degrades the one capability that needs it, at the
moment it is used — with no SMTP host an email is logged instead of sent, and with no
`SUPERADMIN_PASSWORD` the seeder refuses and names the variable — never at import, and
never as a refusal to start.

That matters because a fresh install is exactly that minimum, and the identity surface —
sign-in, users, roles, the activity log — has to answer before anybody has configured
anything else.
"""
import base64
import os
import subprocess
import sys
import textwrap

import pytest

from tests.fixtures import auth, seed_user

pytestmark = pytest.mark.integration


class TestTheServiceStartsWithNothingConfigured:
    def test_the_app_imports_with_only_the_two_required_variables(self, tmp_path):
        """A subprocess, with an environment stripped to `DATABASE_URL` and `SECRET_KEY`.

        In-process this would prove nothing: `core/config.py` is `lru_cache`d and every
        module has already been imported by the time a test runs. Only a fresh interpreter
        can show that the import chain itself has no hidden requirement.

        The route list is what makes "it imported" mean "it wired up": a service that starts
        with no `/users` has not degraded, it has lost half of itself. It goes through
        `tests.route_walk` because fastapi 0.141 stopped flattening `include_router` into
        `app.routes` — the flat listing this used to do now finds the included routers rather
        than their routes, and not one of the three asserted below, so the check failed here
        rather than passing emptily. That module carries the full account.
        """
        script = textwrap.dedent("""
            import main
            from tests.route_walk import iter_api_routes

            routes = [r.path for r in iter_api_routes(main.app.routes)]
            assert "/user/login" in routes
            assert "/users" in routes
            assert "/roles" in routes
            print("OK", len(routes))
        """)
        env = {
            "PATH": os.environ["PATH"],
            "DATABASE_URL": f"sqlite:///{tmp_path / 'boot.db'}",
            "SECRET_KEY": base64.urlsafe_b64encode(b"0" * 32).decode(),
            # Everything else is deliberately absent.
        }
        result = subprocess.run([sys.executable, "-c", script], env=env, cwd=os.getcwd(),
                                capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stderr[-4000:]
        assert result.stdout.startswith("OK")


class TestEachMissingPieceDegradesOneCapability:
    """The two optional pieces the module docstring names, each shown degrading at the moment
    it is used rather than at import."""

    def test_with_no_smtp_host_an_email_is_logged_instead_of_sent(self, monkeypatch, caplog):
        import smtplib

        from integrations.aws_ses import send_email
        from schemas.email_schema import EmailTemplate

        monkeypatch.delenv("SMTP_HOST", raising=False)
        monkeypatch.delenv("SMTP_FROM_EMAIL", raising=False)

        def _no_socket(*_args, **_kwargs):
            raise AssertionError("an unconfigured SMTP transport opened a connection")

        monkeypatch.setattr(smtplib, "SMTP", _no_socket)

        with caplog.at_level("INFO", logger="integrations.aws_ses"):
            result = send_email(
                "someone@example.com", EmailTemplate(subject="Verify", body="<p>123456</p>")
            )

        # A success, not a failure: registration and password reset must keep working on a
        # fresh install, with the message readable in the log instead of an inbox.
        assert result == {"status": True, "message": "Email logged (SMTP not configured)"}
        assert "Verify" in caplog.text

    def test_with_no_superadmin_password_the_seeder_refuses_and_names_it(
        self, db, monkeypatch, capsys
    ):
        """The seeder catches its own errors and prints them, so its output is the report.
        Nothing is half-seeded: the roles it created before refusing are rolled back."""
        import seeders.user_seeder as seeder
        from db.session import SessionLocal
        from models.roles import Role
        from models.user import User

        monkeypatch.setattr(seeder, "SessionLocal", SessionLocal)
        # After the import, which runs `load_dotenv()` and may bring a developer's value in.
        monkeypatch.delenv("SUPERADMIN_PASSWORD", raising=False)

        seeder.seed_database()

        assert "SUPERADMIN_PASSWORD" in capsys.readouterr().out
        assert db.query(User).count() == 0
        assert db.query(Role).count() == 0


class TestAFreshInstallServesTheIdentitySurface:
    def test_the_identity_surface_answers_on_a_fresh_install(self, client, db):
        token = seed_user(db, "someone", ("users:read",))
        assert client.get("/users", headers=auth(token)).status_code in (200, 403)
        # And the health endpoints, which need nothing but the database.
        assert client.get("/api/v1/health/ready").status_code == 200


class TestOpenApiIsAbsentInProduction:
    """Not guarded — *absent*, which is a stronger property than a dependency check."""

    def test_docs_are_served_outside_production(self, client):
        assert client.get("/openapi.json").status_code == 200
        assert client.get("/docs").status_code == 200

    def test_the_contract_names_the_identity_surface(self, client):
        schema = client.get("/openapi.json").json()
        for path in ("/users", "/roles", "/permissions", "/activity-logs"):
            assert path in schema["paths"], path

    def test_docs_do_not_exist_in_production(self):
        """A fresh interpreter, because the app object is built once at import.

        The environment below is the awkward part and it is awkward for a good reason: this
        is the only test in the file that boots with `ENVIRONMENT=prod`, so it is the only
        one that has to satisfy every production-only guard in `core/config.py` before it
        can say anything at all about `/docs`. Three of those guards landed on 2026-08-27
        and each one turned this test red on a subject that is not its own — a weak
        `SECRET_KEY`, a SQLite `DATABASE_URL`, a placeholder `SUPERADMIN_PASSWORD`. Each
        value carries the guard it exists to clear. When a fourth guard is added, this test
        will fail with that guard's message, which is the correct and self-explaining way
        for it to fail; add the value here rather than weakening the guard.
        """
        script = textwrap.dedent("""
            from fastapi.testclient import TestClient
            import main
            assert main.app.openapi_url is None, main.app.openapi_url
            assert main.app.docs_url is None
            client = TestClient(main.app, raise_server_exceptions=False)
            for path in ("/openapi.json", "/docs", "/redoc"):
                assert client.get(path).status_code == 404, path
            print("OK")
        """)
        env = {
            "PATH": os.environ["PATH"],
            # MySQL and not the `sqlite:///{tmp_path}` the tests above use: production
            # refuses SQLite outright. Port 1 is deliberate — nothing here ever opens a
            # connection (`create_engine` is lazy, the app has no lifespan, and a 404
            # from an exempt path touches no session), so a URL that cannot connect is
            # the honest way to say the database is not part of what this test asserts.
            "DATABASE_URL": "mysql+pymysql://app:app@127.0.0.1:1/base_project",
            # `range(32)` and not `b"0" * 32` like the tests above: production requires
            # SECRET_KEY_MIN_DISTINCT distinct characters as well as a length. Encoded
            # zeros are 44 characters but only 5 distinct ones; these 32 bytes encode to 29.
            # A fixed literal rather than `Fernet.generate_key()`, because a test that
            # invents a new key each run cannot be re-run to reproduce its own failure.
            "SECRET_KEY": base64.urlsafe_b64encode(bytes(range(32))).decode(),
            "ENVIRONMENT": "prod",
            "CORS_ORIGINS": "https://app.example.com",
            # Empty, not absent, and the difference is the whole reason this line exists.
            # `core/config.py` calls `load_dotenv()`, which reads the developer's `.env`
            # from the working directory and fills in every variable this dict does not
            # already set — so the subprocess is not as stripped as it looks. The dev
            # `.env` carries a placeholder `SUPERADMIN_PASSWORD`, which production rejects.
            # Naming it here with an empty value keeps `load_dotenv` (override=False) from
            # supplying one, and empty is exactly the "deployment that does not seed" case
            # the guard's own message points at.
            "SUPERADMIN_PASSWORD": "",
        }
        result = subprocess.run([sys.executable, "-c", script], env=env, cwd=os.getcwd(),
                                capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stderr[-4000:]
        # The app logs JSON to stdout, so the marker is the last line rather than all of it.
        assert result.stdout.strip().splitlines()[-1] == "OK"

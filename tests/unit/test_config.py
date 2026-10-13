"""Unit tests for the config helpers, the production CORS guard, `LOG_EXTRA_SECRETS` and
where the OpenAPI docs are served."""
import pytest
from cryptography.fernet import Fernet

from core.config import _optional_int, _required, _split_csv, get_settings

pytestmark = pytest.mark.unit


class TestRequired:
    def test_returns_stripped_value(self, monkeypatch):
        monkeypatch.setenv("X_REQ", "  hello  ")
        assert _required("X_REQ") == "hello"

    def test_raises_when_missing(self, monkeypatch):
        monkeypatch.delenv("X_REQ_MISSING", raising=False)
        with pytest.raises(RuntimeError, match="Missing required"):
            _required("X_REQ_MISSING")

    def test_raises_when_blank(self, monkeypatch):
        monkeypatch.setenv("X_REQ_BLANK", "   ")
        with pytest.raises(RuntimeError):
            _required("X_REQ_BLANK")


class TestOptionalInt:
    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("X_INT", raising=False)
        assert _optional_int("X_INT", 7) == 7

    def test_parses_a_value(self, monkeypatch):
        monkeypatch.setenv("X_INT", "42")
        assert _optional_int("X_INT", 7) == 42

    def test_raises_on_non_integer(self, monkeypatch):
        monkeypatch.setenv("X_INT", "not-a-number")
        with pytest.raises(RuntimeError, match="must be an integer"):
            _optional_int("X_INT", 7)


class TestSplitCsv:
    def test_splits_and_strips(self, monkeypatch):
        monkeypatch.setenv("X_CSV", "a, b ,c")
        assert _split_csv("X_CSV", "") == ("a", "b", "c")

    def test_raises_when_empty(self, monkeypatch):
        monkeypatch.setenv("X_CSV", " , ,")
        with pytest.raises(RuntimeError):
            _split_csv("X_CSV", "")


class TestProductionCorsGuard:
    def test_wildcard_origin_is_rejected_in_production(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setenv("CORS_ORIGINS", "*")
        get_settings.cache_clear()
        try:
            with pytest.raises(RuntimeError, match="cannot contain"):
                get_settings()
        finally:
            # Restore the cache so later tests rebuild from the (restored) test env.
            get_settings.cache_clear()


class TestLogExtraSecrets:
    def test_log_extra_secrets_is_optional_and_comma_separated(self, monkeypatch):
        """Absent is a valid deployment — the redaction rules still run — and present is a
        comma-separated list, trimmed, with no empty entries."""
        monkeypatch.delenv("LOG_EXTRA_SECRETS", raising=False)
        get_settings.cache_clear()
        try:
            assert get_settings().log_extra_secrets == ()
        finally:
            get_settings.cache_clear()

        monkeypatch.setenv("LOG_EXTRA_SECRETS", "site-secret-one, site-secret-two")
        get_settings.cache_clear()
        try:
            assert get_settings().log_extra_secrets == ("site-secret-one", "site-secret-two")
        finally:
            get_settings.cache_clear()


class TestDocsExposure:
    def test_docs_are_absent_only_in_production(self, monkeypatch):
        """Asserted through the whole constructor, which is what a production boot runs.

        That is also why this one needs more environment than any other test here:
        declaring production arms guards that are inert everywhere else, and any of them
        raises before `docs_enabled` is ever read. Those guards are the CORS wildcard, the
        `SECRET_KEY` strength floors, a SQLite `DATABASE_URL` and a placeholder
        `SUPERADMIN_PASSWORD` — plus the `ALGORITHM` allow-list, which applies in every
        environment.

        So the list below is stated positively rather than stripped negatively: every value
        a production boot validates is set here to something that passes, so that the only
        thing this test can fail on is the thing it is about. When another guard is added
        this test will go red with that guard's message, which is the correct way to find
        out that this list needs a line -- and is why the guards raise distinct, quoted
        messages rather than one generic refusal.
        """
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setenv("CORS_ORIGINS", "https://app.example.com")
        # A real Fernet key: long enough and varied enough for the SECRET_KEY floors, and
        # the same shape `scripts/bootstrap.sh` generates. Not a literal secret -- it is
        # generated here precisely so this file ships none.
        monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode())
        # Not SQLite: production refuses it, for the reasons written at that guard.
        monkeypatch.setenv("DATABASE_URL", "mysql+pymysql://u:p@db:3306/app")
        monkeypatch.setenv("SUPERADMIN_PASSWORD", "Not-The-Seeded-Default-1!")
        monkeypatch.setenv("ALGORITHM", "HS256")
        get_settings.cache_clear()
        try:
            assert get_settings().docs_enabled is False
        finally:
            get_settings.cache_clear()
        monkeypatch.setenv("ENVIRONMENT", "staging")
        get_settings.cache_clear()
        try:
            assert get_settings().docs_enabled is True
        finally:
            get_settings.cache_clear()

"""What reaches stdout: the redaction filter on the root handler.

**SEC-11, the filter.** `core/redact.py` knows the shapes a credential takes, but nothing
stopped a `logger.info(payload)` written next month in a module that never heard of it.
`SecretRedactingFilter` sits on the root handler, so the guarantee holds for code nobody has
written yet — which is what the tests below drive it as: a bare logger call from an
anonymous module, not a caller that knew to redact.

Structural redaction catches *shapes*. A secret that has no shape — a site's shared
password, a key issued as a plain word — is caught only if it is named in `LOG_EXTRA_SECRETS`
(`test_a_site_literal_is_masked_when_it_is_configured`).
"""
import io
import json
import logging
import logging.config

import pytest
import uvicorn.config

from core.logging import JsonFormatter, SecretRedactingFilter, configure_logging
from core.redact import MASK

pytestmark = pytest.mark.unit

#: Three shapes — a pattern-shaped key, a password inside a DSN and a bare site literal — so
#: one over-narrow rule cannot make this pass on its own.
AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
URL_PASSWORD = "hunter2secret"
DSN = f"mysql+pymysql://app_user:{URL_PASSWORD}@db.example.com:3306/app"
SITE_LITERAL = "site-literal-secret-value"


class _Capture:
    """A handler wired exactly as `configure_logging` wires the real one, into a buffer."""

    def __init__(self, extra_literals=()):
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.handler.setFormatter(JsonFormatter())
        self.handler.addFilter(SecretRedactingFilter(extra_literals))
        self.logger = logging.getLogger(f"base_project.test.{id(self)}")
        self.logger.setLevel(logging.DEBUG)
        self.logger.handlers = [self.handler]
        self.logger.propagate = False

    def records(self) -> list[dict]:
        return [json.loads(line) for line in self.stream.getvalue().splitlines() if line]


class TestAModuleThatNeverHeardOfRedactionIsStillRedacted:
    def test_a_credential_in_the_message_is_masked(self):
        """The whole of SEC-11 in one line: `logger.info(payload)`, from a module that never
        imported the redactor, with a live key in it."""
        capture = _Capture()
        capture.logger.info("storage client configured with key %s", AWS_KEY)

        message = capture.records()[0]["message"]
        assert AWS_KEY not in message
        assert MASK in message

    def test_the_interpolation_arguments_go_with_it(self):
        """`args` must be cleared when the message is replaced, or the formatter applies `%`
        a second time to a string whose placeholders are now `[REDACTED]` — which raises
        inside logging and loses the line."""
        capture = _Capture()
        capture.logger.info("connecting to %s", DSN)

        assert URL_PASSWORD not in capture.stream.getvalue()
        assert "db.example.com" in capture.records()[0]["message"]

    def test_structured_fields_are_redacted_too(self):
        """`extra_fields` is where this codebase puts everything worth grepping for, so a
        filter that only cleaned the message would clean the half nobody reads."""
        capture = _Capture()
        capture.logger.info("db_connect_failed", extra={"extra_fields": {"dsn": DSN}})

        assert URL_PASSWORD not in capture.stream.getvalue()
        assert MASK in capture.records()[0]["dsn"]

    def test_it_walks_into_nested_fields(self):
        capture = _Capture()
        capture.logger.info("settings_loaded", extra={"extra_fields": {
            "settings": {"env": ["AWS_SECRET_ACCESS_KEY=" + AWS_KEY, "HOME=/app"]}}})

        assert AWS_KEY not in capture.stream.getvalue()

    def test_non_text_fields_survive_as_themselves(self):
        """A number cannot carry a secret, and turning `status_code` into a string would
        break every dashboard built on these lines."""
        capture = _Capture()
        capture.logger.info("request_completed", extra={"extra_fields": {
            "status_code": 500, "latency_ms": 12.4, "cached": False, "user": None}})

        record = capture.records()[0]
        assert record["status_code"] == 500 and record["latency_ms"] == 12.4
        assert record["cached"] is False and record["user"] is None

    def test_a_traceback_is_redacted(self):
        """A traceback that interpolates the value it choked on is the classic way a
        redacted log leaks anyway: the message is clean and the exception text is not."""
        capture = _Capture()
        try:
            raise RuntimeError(f"could not connect to {DSN}")
        except RuntimeError:
            capture.logger.exception("db_connect_failed")

        text = capture.stream.getvalue()
        assert URL_PASSWORD not in text
        assert "RuntimeError" in capture.records()[0]["exception"]

    def test_a_site_literal_is_masked_when_it_is_configured(self):
        """`LOG_EXTRA_SECRETS` names the secrets that have no shape; this is that list
        reaching the logs."""
        capture = _Capture(extra_literals=(SITE_LITERAL,))
        capture.logger.info("smtp login as %s", SITE_LITERAL)

        assert SITE_LITERAL not in capture.stream.getvalue()

    def test_an_unredactable_record_is_dropped_rather_than_passed_through(self):
        """Fail closed, and never raise.

        `logging.Handler.handle` calls filters outside its own error handling, so an
        exception here would propagate into whatever called `logger.info(...)` — a logging
        bug becoming an application crash. And the record it could not inspect is precisely
        the one that might be carrying the credential, so it is dropped, not forwarded.
        """
        capture = _Capture()
        # "%d" against a string raises inside `record.getMessage()`.
        capture.logger.info("%d", AWS_KEY, extra={"extra_fields": {"dsn": DSN}})

        text = capture.stream.getvalue()
        assert AWS_KEY not in text and URL_PASSWORD not in text
        assert "redaction failed" in capture.records()[0]["message"]


class TestTheFilterIsActuallyInstalled:
    def test_configure_logging_installs_it_on_the_root_handler(self):
        """A filter that is correct and unregistered redacts nothing.

        This asserts that `configure_logging` — the one function that installs it, and the
        one `main.py` calls at import — actually does. It deliberately CALLS that function
        rather than inspecting whatever the root logger happens to carry when this test
        runs: `configure_logging` does `handlers.clear()`, pytest's own logging plugin
        installs and removes handlers around every test, and the suite imports `main` from
        several modules. Reading ambient global state here made the assertion pass or fail
        on test ORDER — green run alone, red in the full suite — which is a test that
        reports on the scheduler rather than on the code.

        The handlers are restored afterwards because this function replaces them wholesale,
        and a unit test that silently reconfigures logging for everything after it is the
        same class of defect one level up.
        """
        root = logging.getLogger()
        saved_handlers, saved_level = root.handlers[:], root.level
        try:
            configure_logging("INFO", ("a-site-literal",))
            installed = [
                handler for handler in root.handlers
                if any(isinstance(f, SecretRedactingFilter) for f in handler.filters)
            ]
            assert installed, "configure_logging left no redacting handler on the root logger"
        finally:
            root.handlers[:] = saved_handlers
            root.setLevel(saved_level)

    def test_uvicorns_access_log_goes_through_it(self, capsys):
        """The request line uvicorn prints for every request, query string included.

        uvicorn configures logging before it imports the app, giving `uvicorn.access` its
        own stdout handler with `propagate=False`, so the filter on the root handler never
        saw it — and `GET /user/verify-token?token=…` carries a live one-time credential in
        exactly that line. This replays the real order: uvicorn's own config first, then
        `configure_logging` as `main.py` runs it at import.
        """
        root = logging.getLogger()
        saved_handlers, saved_level = root.handlers[:], root.level
        saved = {
            name: (logging.getLogger(name).handlers[:], logging.getLogger(name).propagate)
            for name in ("uvicorn", "uvicorn.error", "uvicorn.access")
        }
        try:
            logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)
            configure_logging("INFO")
            token = "Z0FBQUFBQm9fSECRETVALUE%3D%3D"
            logging.getLogger("uvicorn.access").info(
                '%s - "%s %s HTTP/%s" %d',
                "127.0.0.1:41840", "GET", f"/user/verify-token?token={token}", "1.1", 200,
            )
            out = capsys.readouterr().out
            assert "SECRETVALUE" not in out, f"the access log printed the token: {out!r}"
            assert MASK in out
        finally:
            root.handlers[:] = saved_handlers
            root.setLevel(saved_level)
            for name, (handlers, propagate) in saved.items():
                logging.getLogger(name).handlers[:] = handlers
                logging.getLogger(name).propagate = propagate

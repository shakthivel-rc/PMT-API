"""JSON to stdout, and a redaction pass that nobody has to remember to call.

`core/redact.py` holds the secret patterns. Nothing stops a `logger.info(request_body)`
written next month in a module that has never heard of them, so a guarantee that rested on
every author remembering to call `redact()` — CLAUDE.md §7 rule 7, "no secret reaches a
log" — would be behavioural: it would hold only while every author knew about it.

`SecretRedactingFilter` makes it structural. It hangs off the root handler, so every record
from every logger in the process passes through the redactor on its way to stdout, whether
or not the code that wrote it knew this file exists. What it cannot catch is a credential
with no recognisable shape, which is what `LOG_EXTRA_SECRETS` is for.

**It is not free**, and the numbers belong here rather than in a review comment. Measured on
2026-08-27:

* one `request_completed` record — a short message plus five structured fields, three of
  them strings — costs about **110 µs**. There is a fixed floor of roughly 50 µs per string,
  because the work is 18 regex passes and their setup dominates anything a log line is
  likely to contain; length barely matters until the text gets large.
* a **15 kB traceback costs 19.7 ms**, and that one is not a floor — it scales with the
  text. It is paid only on the path that is already failing (`core/errors.py` renders the
  exception into the record), which is why it is acceptable rather than merely tolerated.

Re-measured on 2026-09-28, after the same 18 rules moved into `core/redact.py`: about 33 µs
per `request_completed` record, a floor of about 6 µs per string and 6.1 ms for 15 kB. The
module they moved from measures the same on that machine, so the gap to the figures above
is the measuring setup, not the move. The proportions are what carry over.

So CLAUDE.md §13's "do not log high-frequency chatter at INFO" is now a performance rule as
well as a taste one, and a debug loop that logs a thousand times a second will feel this. If
that ever becomes the constraint, the fix is a screening pass — one combined alternation over
`RULES` that answers "could anything here match" before the 18 substitutions run — not
turning the filter off for some subset of loggers, which is how a structural guarantee
becomes a behavioural one again.
"""
from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterable
from typing import Any

from core.redact import Redactor


#: How deep into a structured `extra_fields` value the redactor will walk. Four is two more
#: than anything in this codebase logs today; the limit exists so that a self-referencing
#: structure cannot turn a log call into a stack overflow, which is a failure mode a logging
#: filter must not have.
_MAX_FIELD_DEPTH = 4

#: What replaces a record the filter could not process. See `SecretRedactingFilter.filter`.
_REDACTION_FAILED = "log record suppressed: redaction failed"

#: Only ever used to render `exc_info` into text. `logging.Formatter` with no format string
#: does nothing else, and building one per record would allocate for no reason.
_EXCEPTION_RENDERER = logging.Formatter()

#: The loggers uvicorn configures with handlers of its own. `uvicorn.error` has none but
#: propagates into `uvicorn`, so it is listed for the day uvicorn's defaults change.
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


class SecretRedactingFilter(logging.Filter):
    """Run `redact()` over every record's message, structured fields and traceback.

    Attached to the *handler*, not to a logger, and that is the only placement that works:
    a filter on a logger runs for records logged directly to it and not for the ones that
    propagate up from its children, so a filter on the root logger would miss every module
    in this service. A filter on the root handler sees everything that reaches stdout.

    It edits the record in place. That is deliberate — any other handler attached later (a
    file handler, a shipper, pytest's `caplog`) then sees the redacted record too, because
    they are all handed the same object.
    """

    def __init__(self, extra_literals: Iterable[str] = ()) -> None:
        super().__init__()
        # One `Redactor`, built once. `redact()` constructs one per call, which re-sorts the
        # site's literal list on every log record; the rules themselves are compiled at
        # import either way.
        self._redactor = Redactor(extra_literals)

    def _redact_value(self, value: Any, depth: int) -> Any:
        if isinstance(value, str):
            return self._redactor.redact(value)[0]
        if depth < _MAX_FIELD_DEPTH:
            if isinstance(value, dict):
                return {key: self._redact_value(item, depth + 1) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [self._redact_value(item, depth + 1) for item in value]
        # Numbers, booleans and None cannot carry a secret and cost nothing to skip. Anything
        # else is left alone and rendered by `json.dumps(default=str)`; a value whose `str()`
        # holds a credential is the one gap here, and the answer to it is to log the field,
        # not the object.
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            redacted, report = self._redactor.redact(message)
            if report.count:
                # `args` must go with it: the message is already interpolated, and leaving
                # them would make the formatter apply `%` a second time to a string whose
                # placeholders have been replaced by `[REDACTED]`.
                record.msg = redacted
                record.args = None

            fields = getattr(record, "extra_fields", None)
            if isinstance(fields, dict):
                record.extra_fields = self._redact_value(fields, 0)

            # Rendered here rather than in the formatter so that the traceback goes through
            # the same pass as everything else. A traceback that interpolates the value it
            # choked on is the classic way a redacted log leaks anyway, and
            # `tests/unit/test_log_redaction.py` pins that exact scenario.
            if record.exc_info and not record.exc_text:
                rendered = _EXCEPTION_RENDERER.formatException(record.exc_info)
                record.exc_text = self._redactor.redact(rendered)[0]
        except Exception:  # deliberately broad - see below
            # Fail closed, and never raise. `logging.Handler.handle` calls filters outside
            # its own error handling, so an exception raised here does not lose a log line —
            # it propagates into whatever called `logger.info(...)`, turning a logging bug
            # into an application crash.
            #
            # Dropping the content rather than passing it through: this is the branch where
            # we could not inspect the record, which is precisely when it may be the one
            # carrying the credential.
            record.msg = _REDACTION_FAILED
            record.args = None
            record.exc_info = None
            record.exc_text = None
            record.extra_fields = {}
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        extra_fields = getattr(record, "extra_fields", None)
        if isinstance(extra_fields, dict):
            payload.update(extra_fields)

        # `exc_text` first, because `SecretRedactingFilter` puts the redacted rendering
        # there. The fallback keeps this formatter correct on a record that never passed
        # through that filter — a second handler, or a test that formats a record by hand.
        if record.exc_text:
            payload["exception"] = record.exc_text
        elif record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", extra_secrets: Iterable[str] = ()) -> None:
    """One JSON handler on the root logger, with redaction in front of it.

    `extra_secrets` is `LOG_EXTRA_SECRETS` — the literal values a site knows are secret
    and no pattern could recognise. Passed in rather than read from `get_settings()` here so
    that this module keeps working for anything that configures logging before, or without,
    a settings object.
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    # On the handler rather than on `root_logger`: a logger's filters do not run for records
    # that propagate up from its children, and every logger in this service is a child.
    handler.addFilter(SecretRedactingFilter(extra_secrets))

    root_logger.handlers.clear()
    root_logger.addHandler(handler)

    # uvicorn's own loggers, through the same handler. uvicorn installs its logging config
    # before it imports this app, and that config gives `uvicorn` and `uvicorn.access` their
    # own stdout handlers with `propagate=False` — so their records never reached the filter
    # above. The access log prints the full request line, query string included, and the
    # emailed-link check endpoints (`GET /user/verify-token?token=…`,
    # `GET /user/verify-forgot-password-token?token=…`) carry a live one-time credential
    # there: before this, it went to stdout in the clear. Measured 2026-09-29 by running
    # `uvicorn main:app` and requesting one. Runs after uvicorn's config because `main.py`
    # calls this at import, which is after uvicorn has configured logging in every mode
    # (plain, --reload, --workers).
    for name in _UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True

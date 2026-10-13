"""Secret redaction for everything this service writes to a log.

`core/logging.py:SecretRedactingFilter` runs a `Redactor` over every record — its message,
its structured fields and its rendered traceback — on the way to stdout, so a credential
that reaches a log call is masked whether or not the code that logged it knew this module
exists. `redact()` is the one-call form, for code that has a single string to clean.

Redaction is deliberately over-eager. A false positive costs a log line a few characters of
context; a false negative writes a live key into a log that is shipped, retained and read by
people who were never meant to hold it.

## Why the mask carries no kind

A mask that named its detector — `[REDACTED:aws-access-key]` — would tell a reader the
*shape* of a value we had just decided was too sensitive to keep, so this module emits a flat
`[REDACTED]`. Which rule fired is not recorded anywhere: the one consumer, the log filter,
needs only to know *that* something was masked (`RedactionReport.count`). A derived project
that wants per-rule counts adds them together with the place an operator reads them, not as
a counter nothing surfaces. The one exception is an armoured private key, which becomes
`[REDACTED]-PRIVATE-KEY`: a multi-line block replaced by a bare token reads like a truncation
bug rather than a deliberate removal.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

MASK = "[REDACTED]"
PRIVATE_KEY_MASK = f"{MASK}-PRIVATE-KEY"

#: `extra_literals` shorter than this are ignored. Masking every occurrence of a
#: 3-character "secret" would shred a log without protecting anything.
MIN_LITERAL_LENGTH = 6


@dataclass(frozen=True)
class Rule:
    #: Identifies the rule to a reader of this table; never emitted (see "Why the mask
    #: carries no kind" above).
    name: str
    pattern: re.Pattern[str]
    #: Which regex group holds the secret: an index, a name, or 0 for the whole match.
    group: int | str = 0
    #: What the secret is replaced with. Defaults to `MASK`.
    mask: str = MASK


def _rx(p: str, flags: int = 0) -> re.Pattern[str]:
    return re.compile(p, flags)


#: Ordered: structural rules (key blocks, URL credentials) run before shape rules, so a
#: token inside a URL is redacted once, not twice.
RULES: tuple[Rule, ...] = (
    Rule("private-key",
         _rx(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
             re.DOTALL),
         mask=PRIVATE_KEY_MASK),
    Rule("ssh-key", _rx(r"\bssh-(?:rsa|ed25519|dss)\s+[A-Za-z0-9+/=]{60,}")),
    Rule("url-credentials",
         _rx(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)"
             r"(?P<user>[^/\s:@]{1,64}):(?P<secret>[^/\s@]{1,256})@"),
         group="secret"),
    Rule("authorization-header",
         _rx(r"(?i)(authorization\s*[:=]\s*(?:bearer|basic|token|apikey)\s+)(\S+)"), group=2),
    Rule("aws-access-key", _rx(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA)[0-9A-Z]{16}\b")),
    Rule("aws-secret-key",
         _rx(r"(?i)\baws_secret_access_key\b\s*[=:]\s*(\S+)"), group=1),
    Rule("github-token", _rx(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b")),
    Rule("github-pat", _rx(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    Rule("gitlab-token", _rx(r"\bglpat-[A-Za-z0-9_\-]{16,}\b")),
    # Added 2026-09-29 with the AI worker, the first process here that holds one. An SDK
    # error or a debug dump can carry the key bare, with no `KEY=` for the assignment rule
    # below to anchor on.
    Rule("anthropic-key", _rx(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    Rule("slack-token", _rx(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b")),
    Rule("google-api-key", _rx(r"\bAIza[0-9A-Za-z\-_]{35}\b")),
    Rule("npm-token", _rx(r"\bnpm_[A-Za-z0-9]{36}\b")),
    Rule("pypi-token", _rx(r"\bpypi-[A-Za-z0-9_\-]{16,}\b")),
    Rule("jwt", _rx(r"\beyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\b")),
    Rule("azure-key", _rx(r"(?i)\b(AccountKey|SharedAccessSignature)\s*=\s*([^;\s\"']+)"), group=2),
    # Plain key=value / key: value assignments of anything secret-shaped — config dumps, env
    # listings, form fields echoed into an error.
    #
    # The surrounding `[A-Za-z0-9_\-.]*` is what makes `DEPLOY_TOKEN=` and `MY_API_KEY=`
    # match: a leading `\b` finds no boundary inside `DEPLOY_TOKEN`, because `_` is a word
    # character — so an anchored pattern silently misses every prefixed variable, which is
    # how almost every real secret variable is named.
    Rule("secret-assignment",
         _rx(r"""(?ix)
             \b(?P<key>[A-Za-z0-9_\-.]*
                 (?:pass(?:word|wd)?|passwd|secret|token|api[_\-]?key|access[_\-]?key|
                    auth[_\-]?key|private[_\-]?key|credential)
                [A-Za-z0-9_\-.]*)
             \s*(?P<sep>[:=])\s*
             (?P<val>"[^"\n]{1,512}"|'[^'\n]{1,512}'|[^\s;,)\]}]{1,512})
         """), group="val"),
    Rule("secret-cli-flag",
         _rx(r"(?i)(--?(?:password|passwd|token|secret|api[-_]?key|access[-_]?key)[= ])(\S+)"),
         group=2),
    Rule("basic-auth-blob", _rx(r"(?i)\bBasic\s+[A-Za-z0-9+/]{20,}={0,2}\b")),
)


@dataclass
class RedactionReport:
    #: How many secrets were masked. Zero means the text came back unchanged.
    count: int = 0


class Redactor:
    """Applies :data:`RULES` plus any site-supplied literal secrets.

    `extra_literals` is how a deployment names secrets that have no shape — a shared
    service password, a key issued as a plain word, a customer passphrase — which no
    generic pattern could ever recognise. It is fed by `LOG_EXTRA_SECRETS`.
    """

    def __init__(self, extra_literals: Iterable[str] = ()) -> None:
        # Longest first, so a literal that contains another is masked as itself rather
        # than being half-eaten by the shorter one.
        self.literals = sorted(
            {s for s in extra_literals if s and len(s) >= MIN_LITERAL_LENGTH},
            key=len, reverse=True,
        )

    def redact(self, text: str) -> tuple[str, RedactionReport]:
        report = RedactionReport()
        if not text:
            return text, report
        out = text
        for rule in RULES:
            def _sub(m: re.Match[str], _rule: Rule = rule) -> str:
                whole = m.group(0)
                try:
                    secret = m.group(_rule.group) if _rule.group else whole
                except (IndexError, KeyError):  # pragma: no cover - defensive
                    secret = whole
                if not secret:
                    return whole
                report.count += 1
                # A group-scoped rule replaces only the secret, so the surrounding
                # context — the scheme, the user, the header name — survives and stays
                # diagnostic. "could not connect to db.example.com" is the useful half.
                return whole.replace(secret, _rule.mask, 1) if _rule.group else _rule.mask

            out = rule.pattern.sub(_sub, out)

        for literal in self.literals:
            if literal in out:
                report.count += out.count(literal)
                out = out.replace(literal, MASK)

        return out, report


def redact(text: str, extra_secrets: Iterable[str] | None = None) -> str:
    """Redact and return the text, for a caller that wants a string and not a report.

    Builds a `Redactor` per call. Code that redacts in a loop — the log filter is the one in
    this service — should hold its own `Redactor` instead, so the literal list is sorted
    once.
    """
    if not text:
        return text
    return Redactor(extra_secrets or ()).redact(text)[0]

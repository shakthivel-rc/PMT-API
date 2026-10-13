from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import os

from dotenv import load_dotenv


load_dotenv()


def _required(name: str) -> str:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value.strip()


def _optional_int(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or raw_value.strip() == "":
        return default
    try:
        return int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be an integer") from exc


def _optional_bool(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None or raw_value.strip() == "":
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def _split_csv(name: str, default: str) -> tuple[str, ...]:
    raw_value = os.getenv(name, default)
    values = tuple(item.strip() for item in raw_value.split(",") if item.strip())
    if not values:
        raise RuntimeError(f"Environment variable {name} must contain at least one value")
    return values


def _optional_csv(name: str) -> tuple[str, ...]:
    """Like `_split_csv`, but an empty list is a legitimate answer."""
    raw_value = os.getenv(name, "")
    return tuple(item.strip() for item in raw_value.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    app_name: str
    environment: str
    database_url: str
    secret_key: str
    algorithm: str
    jwt_expiry_hours: int
    refresh_token_expiry_days: int
    cors_origins: tuple[str, ...]
    log_level: str
    auth_max_failed_login_attempts: int
    auth_lockout_minutes: int
    password_min_length: int

    # --- inbound rate limiting ----------------------------------------------------------
    #: Per client address, per minute, counted in-process. See `core/ratelimit.py` for what
    #: that does and does not buy on a multi-worker deployment. 0 on either limit disables
    #: that limit alone; `rate_limit_enabled=False` disables both.
    rate_limit_enabled: bool
    rate_limit_credential_per_minute: int
    rate_limit_general_per_minute: int
    #: Addresses and CIDRs whose `X-Forwarded-For` header may be believed. Empty means
    #: nobody, and the peer address is used as-is.
    #:
    #: An allow-list rather than a boolean, because a boolean cannot be both correct and
    #: safe here. `X-Forwarded-For` is caller-controlled, so believing it unconditionally
    #: gives every attacker an unlimited supply of fresh rate-limit buckets; not believing
    #: it at all puts every user behind one proxy into a single bucket, where any client
    #: can exhaust the login budget for everyone. Trusting the header only when the
    #: *peer* is a proxy you named is the only shape that is neither.
    trusted_proxy_ips: tuple[str, ...]

    # --- request body ceiling ------------------------------------------------------------
    #: The largest request body this API will read, in bytes. 0 disables the ceiling. See
    #: `core/middleware.py:BodySizeLimitMiddleware` for what enforces it, and why it checks
    #: both the declared length and the bytes that actually arrive.
    max_request_body_bytes: int

    # --- database pool -------------------------------------------------------------------
    #: Ignored for SQLite, whose pool implementation takes none of these. See db/session.py.
    db_pool_size: int
    db_max_overflow: int
    db_pool_recycle_seconds: int

    # --- logging -------------------------------------------------------------------------
    #: Literal secret values the log redaction filter masks on top of the shapes in
    #: `core/redact.py:RULES` — a password or key with no distinctive shape, which no
    #: pattern could ever recognise.
    log_extra_secrets: tuple[str, ...]
    # --- project management (docs/design/) -------------------------------------------
    # Defaulted, and last, so a caller that builds `Settings` by hand keeps working. Both
    # external integrations default to OFF, per CLAUDE.md §12: with no webhook secret the
    # GitHub endpoint refuses every delivery (it has nothing to verify a signature
    # against), and with AI off the summary endpoint answers 503 instead of queueing work
    # no worker will ever take.
    github_webhook_secret: str = ""
    ai_enabled: bool = False
    ai_model: str = "claude-opus-5"
    ai_effort: str = "high"
    ai_max_attempts: int = 3

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"prod", "production"}

    @property
    def docs_enabled(self) -> bool:
        """Whether OpenAPI and Swagger are served.

        Off in production and on everywhere else. The SPA and any integrator need a
        machine-readable contract; a production deployment does not need to publish its
        entire route table, its parameter names and its response shapes to anyone who can
        reach the port. Note this is *absence*, not an auth check — there is no `/docs` in
        production to be authorised against.
        """
        return not self.is_production


#: The JWT algorithms this service can actually use. Checked in every environment — see the
#: guard at the bottom of `get_settings` for why that is not over-caution.
#:
#: HS* only, because `SECRET_KEY` is a single shared string. RS256 and friends need a
#: private key to sign and a public key to verify, and this configuration has nowhere to put
#: the second one; listing them would advertise a capability that does not exist.
ALLOWED_ALGORITHMS = frozenset({"HS256", "HS384", "HS512"})
#: The Messages API's `output_config.effort` levels. Checked at boot because a typo here
#: otherwise surfaces as a 400 on the first job the worker claims, minutes or hours later,
#: in a log nobody is watching.
ALLOWED_AI_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})

#: Production floor for `SECRET_KEY`, as a length and as a count of distinct characters.
#:
#: Both numbers are measured against the thing the key is supposed to be. `.env.example`
#: and `scripts/bootstrap.sh` generate a Fernet key: 32 random bytes, urlsafe-base64, always
#: exactly 44 characters. Over 200,000 generated keys on 2026-08-27 the fewest distinct
#: characters any of them contained was 22, and the 1st percentile was 27 — so a floor of 16
#: clears every real key by six characters and still refuses everything hand-typed:
#: `changeme` is 8 characters and 7 distinct, and padding it out to length (`changeme` four
#: times over) is 32 characters and still 7 distinct.
#:
#: A length floor alone would not do it, which is why there are two numbers.
SECRET_KEY_MIN_LENGTH = 32
SECRET_KEY_MIN_DISTINCT = 16

#: Values of `SUPERADMIN_PASSWORD` that mean "nobody chose this".
#:
#: `seeders/user_seeder.py` has no hard-coded default — it raises when the variable is
#: absent — so what "still the seeded default" means in practice is whatever somebody typed
#: to get past that error, plus the placeholder that ships in `.env.example`'s own
#: `DATABASE_URL` line. `scripts/bootstrap.sh` generates a real one, so a stack stood up by
#: the documented path never trips this.
#:
#: `core/config.py` imports nothing from this application — only the standard library and
#: `dotenv` — and it is imported by `routes/`, `db/`, `core/` and the seeders. The day it
#: imports one of them is the day one of them cannot import it, which is why this list lives
#: here rather than next to the seeder.
_PLACEHOLDER_PASSWORDS = frozenset({
    "changeme", "change_me", "change-me", "changeme123", "password", "passw0rd",
    "password123", "admin", "admin123", "superadmin", "secret", "letmein",
    "your_secure_password", "your-secure-password", "your_password",
})


def _is_placeholder_password(value: str, min_length: int) -> bool:
    """Whether `SUPERADMIN_PASSWORD` is a stand-in rather than a password somebody chose.

    Length is part of the test, not a separate one: this value opens the account that can
    do everything, so a value shorter than the floor every *ordinary* account must clear
    (`PASSWORD_MIN_LENGTH`, CLAUDE.md §7 rule 5) was certainly not chosen for it. The
    seeder itself does not check the policy, so at boot is the only place this is caught.
    """
    text = value.strip()
    if text.startswith("<") and text.endswith(">"):
        return True
    return len(text) < min_length or text.lower() in _PLACEHOLDER_PASSWORDS


@lru_cache
def get_settings() -> Settings:
    settings = Settings(
        app_name=os.getenv("APP_NAME", "PMT"),
        environment=os.getenv("ENVIRONMENT", "development"),
        database_url=_required("DATABASE_URL"),
        secret_key=_required("SECRET_KEY"),
        algorithm=os.getenv("ALGORITHM", "HS256"),
        jwt_expiry_hours=_optional_int("JWT_EXPIRY_VALUE", 1),
        refresh_token_expiry_days=_optional_int("REFRESH_TOKEN_EXPIRY_DAYS", 30),
        cors_origins=_split_csv("CORS_ORIGINS", "http://localhost:5173"),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        auth_max_failed_login_attempts=_optional_int("AUTH_MAX_FAILED_LOGIN_ATTEMPTS", 5),
        auth_lockout_minutes=_optional_int("AUTH_LOCKOUT_MINUTES", 15),
        password_min_length=_optional_int("PASSWORD_MIN_LENGTH", 12),
        rate_limit_enabled=_optional_bool("RATE_LIMIT_ENABLED", True),
        # 10/minute on the credential endpoints. A person signing in mistypes twice and
        # gets it right; the account lockout at 5 failures fires long before this does, so
        # this number is not about one account at all — it is the ceiling on how fast one
        # client can work through *many* accounts, and on how many password-reset mails it
        # can make this service send.
        rate_limit_credential_per_minute=_optional_int("RATE_LIMIT_CREDENTIAL_PER_MINUTE", 10),
        # 300/minute everywhere else — five a second, sustained. The SPA's busiest screen
        # issues well under twenty requests to open, so a limit an order of magnitude higher
        # is one that only a script reaches.
        rate_limit_general_per_minute=_optional_int("RATE_LIMIT_GENERAL_PER_MINUTE", 300),
        # Empty by default: a bare deployment sees real client addresses already, and a
        # deployment behind a proxy must say which one. `docker-compose.yml` sets it to the
        # bridge network the shipped nginx sits on.
        trusted_proxy_ips=_optional_csv("TRUSTED_PROXY_IPS"),
        # 4 MiB, a generous default well above any JSON body the identity and RBAC schemas
        # accept — the largest of those is a few kB. It is a ceiling against abuse, not a
        # fit to any one payload. A derived project that adds uploads or other large bodies
        # should derive its own number from the largest body its schemas declare, and set
        # it here with that reasoning.
        #
        # The ceiling matters because Starlette buffers the whole body before a handler sees
        # it, and the `def` handlers run in a 40-slot threadpool (see DB_POOL_SIZE above), so
        # 40 concurrent oversized posts is the worst case — 160 MiB held at once here,
        # against 4 GB of unbounded before it.
        max_request_body_bytes=_optional_int("MAX_REQUEST_BODY_BYTES", 4 * 1024 * 1024),
        # 20 + 20. Derived, not guessed — the working set is Starlette's threadpool, which
        # runs the `def` request handlers and defaults to 40 slots. A ceiling of 40 covers
        # the threadpool exactly; the 20/20 split means the steady state holds 20 connections
        # open and the burst is borrowed and returned. The previous configuration was
        # SQLAlchemy's default 5 + 10, i.e. 15 against a 40-slot threadpool, which does not
        # fail loudly: it queues, and the symptom is latency nobody can attribute.
        db_pool_size=_optional_int("DB_POOL_SIZE", 20),
        db_max_overflow=_optional_int("DB_MAX_OVERFLOW", 20),
        # Half an hour, comfortably under both MySQL's `wait_timeout` and the idle timeout
        # of every load balancer that sits in front of one.
        db_pool_recycle_seconds=_optional_int("DB_POOL_RECYCLE_SECONDS", 1800),
        # Literal secret values this deployment wants masked on top of the generic
        # patterns — a password or key with no distinctive shape, which no regex could
        # ever recognise. See core/redact.py.
        log_extra_secrets=_optional_csv("LOG_EXTRA_SECRETS"),
        github_webhook_secret=os.getenv("GITHUB_WEBHOOK_SECRET", "").strip(),
        ai_enabled=_optional_bool("AI_ENABLED", False),
        ai_model=os.getenv("AI_MODEL", "claude-opus-5").strip(),
        ai_effort=os.getenv("AI_EFFORT", "high").strip().lower(),
        ai_max_attempts=_optional_int("AI_MAX_ATTEMPTS", 3),
    )

    if settings.is_production and "*" in settings.cors_origins:
        raise RuntimeError("CORS_ORIGINS cannot contain '*' in production")

    # Checked in EVERY environment, unlike the guards around it, and that is deliberate.
    # `ALGORITHM` is handed straight to `jwt.decode(token, key, algorithms=[...])`
    # (`routes/auth_middleware.py:decode_access_token`), and without this guard `none` is a
    # legal value of it: it names the *unsigned* JWT, so every caller mints their own claims
    # and the signature is not checked. That is not a matter of production posture — a
    # staging deployment with `ALGORITHM=none` is exactly as bypassed as a production one,
    # and staging holds real user rows and real password hashes.
    #
    # It is also the guard most worth failing early: a production-only check is one nobody
    # meets until the deploy, and this one costs a developer sixty seconds on their own
    # machine instead.
    if settings.ai_effort not in ALLOWED_AI_EFFORTS:
        raise RuntimeError(
            f"AI_EFFORT must be one of {', '.join(sorted(ALLOWED_AI_EFFORTS))}, not "
            f"{settings.ai_effort!r}."
        )
    if settings.algorithm not in ALLOWED_ALGORITHMS:
        raise RuntimeError(
            f"ALGORITHM must be one of {', '.join(sorted(ALLOWED_ALGORITHMS))}, not "
            f"{settings.algorithm!r}. 'none' means an unsigned token: it does not weaken "
            f"authentication, it removes it."
        )

    if settings.is_production:
        # The strength of this one value is the strength of every session: it signs every
        # JWT and it is the Fernet key for verify and reset links. A deployment can ship
        # `SECRET_KEY=changeme` today — it is a string, nothing rejects it, and the service
        # starts and issues forgeable tokens. Both floors are needed: see the measurements
        # at SECRET_KEY_MIN_LENGTH.
        #
        # The message quotes the floors and never the value. An error that echoed a secret
        # would put it in the log of every failed start (CLAUDE.md §8).
        if (len(settings.secret_key) < SECRET_KEY_MIN_LENGTH
                or len(set(settings.secret_key)) < SECRET_KEY_MIN_DISTINCT):
            raise RuntimeError(
                f"SECRET_KEY is too weak for production: at least {SECRET_KEY_MIN_LENGTH} "
                f"characters and {SECRET_KEY_MIN_DISTINCT} distinct ones are required. "
                "Generate one with: python -c \"from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())\""
            )

        # SQLite in production is one file, one writer, and no network. The service runs
        # `uvicorn` with a pool of 40 (DB_POOL_SIZE + DB_MAX_OVERFLOW), so the first thing a
        # real load produces is `database is locked` — and the migrations are written
        # against MySQL besides (CLAUDE.md §17.5 names two divergences the test harness
        # papers over). The suite runs on SQLite and must keep doing so, which is exactly
        # why this is scoped to production rather than enforced at the engine.
        if settings.database_url.strip().lower().startswith("sqlite"):
            raise RuntimeError(
                "DATABASE_URL points at SQLite, which is not a production database for this "
                "service. Point it at the MySQL 8 the migrations are written for."
            )

        # The seeded superadmin holds every permission in the catalogue, and its password is
        # the one credential a deployment can leave at a documented value without anything
        # noticing: the seeder takes whatever it is given, and the account it creates is
        # reachable from the public sign-in form. Read at boot rather than kept on
        # `Settings` on purpose — it belongs to `seeders/user_seeder.py`, not to the running
        # service, and a live password on the settings object would appear in every
        # `repr(settings)` and every debugger frame that holds one.
        seeded_password = os.getenv("SUPERADMIN_PASSWORD", "")
        if seeded_password.strip() and _is_placeholder_password(
                seeded_password, settings.password_min_length):
            raise RuntimeError(
                "SUPERADMIN_PASSWORD is a placeholder or is shorter than "
                f"PASSWORD_MIN_LENGTH ({settings.password_min_length}). Set the password "
                "the seeded superadmin account should actually have, or unset the variable "
                "on a deployment that does not seed."
            )

    return settings

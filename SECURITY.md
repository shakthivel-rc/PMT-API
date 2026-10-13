# Security

## Reporting a vulnerability

Report privately, not in a public issue. Open a GitHub **security advisory** on this
repository (Security → Report a vulnerability). Please include the version or commit, what
an attacker gains, and the smallest reproduction you have.

A project built on this base inherits this route, not a contact: the advisory form works
only once private vulnerability reporting is enabled on its own repository, and a private
email address, if it wants one, is its own to add here.

Expect an acknowledgement within three working days. We will tell you what we found, what
we plan to do, and when — and we will credit you in the release notes unless you ask us not
to.

## Supported versions

This service is deployed from source. There is no long-term support branch: the supported
version is the default branch, and a fix ships there. If you run a pinned commit,
you are responsible for rebasing onto a fixed one.

## What this service is trusted with

Read this before deciding how serious a finding is. It stores:

- **User accounts**: bcrypt password hashes and personal data (names, emails,
  organisation, department).
- **Session tokens**, stored only as SHA-256 digests (`user_sessions`).
- **JWT signing material and reset-link keys** in `SECRET_KEY`.
- **SMTP credentials**, in the environment only — never in the database.

A finding that lets a caller authenticate as another user, escalate a role or permission,
read another user's data or take over the seeded superadmin is high severity regardless of
how ordinary the bug looks.

## The controls that are meant to hold

If you can defeat one of these, that is a report worth making:

| Control | Where |
|---|---|
| Authentication by default, exemption by written-down list | `routes/auth_middleware.py`, `tests/unit/test_route_inventory.py` |
| Path decisions taken on `scope["path"]`, never the caller-controlled `request.url.path` | `routes/auth_middleware.py`, `core/middleware.py` |
| Authorization dependency fails closed (raises, never returns) | `dependencies/auth.py`, `tests/unit/test_authz_fail_closed.py` |
| Soft-deleted users and roles grant nothing | `services/user.py`, `models/user.py`, `tests/integration/test_soft_delete_authz.py` |
| Session tokens stored as SHA-256 digests; logout and revocation take effect on the next request | `models/user_session.py`, `tests/unit/test_session_token_hashing.py` |
| Per-account lockout and per-client rate limiting | `controllers/auth_controller.py`, `core/ratelimit.py` |
| No account enumeration by message or timing | `controllers/auth_controller.py`, `tests/integration/test_auth_enumeration.py` |
| OTPs drawn from `secrets`, never `random` | `controllers/auth_controller.py`, `tests/unit/test_secret_hygiene.py` |
| Secret redaction on every log record | `core/logging.py` (`SecretRedactingFilter`), `core/redact.py`, `tests/unit/test_log_redaction.py` |
| Production config guards: `SECRET_KEY` strength, `ALGORITHM` allow-list, no SQLite, no placeholder `SUPERADMIN_PASSWORD`, no `*` CORS | `core/config.py`, `tests/unit/test_body_limit.py::TestProductionConfigGuards`, `tests/unit/test_config.py` (CORS) |
| Request-body ceiling | `core/middleware.BodySizeLimitMiddleware`, `tests/unit/test_body_limit.py` |
| Errors never carry a stack frame or connection string | `core/errors.py`, `tests/unit/test_error_handlers.py` |
| No credentials in the image or the repository | `.dockerignore`, `scripts/check_staged_secrets.py`, CI |

## Known limitations, deliberately accepted

These are documented rather than fixed, so please do not report them as new — though a way
to *exploit* one further is very welcome.

- **Rate limiting is in-process.** Two workers means two counters and twice the limit; a
  replica set multiplies it again. A shared store is the real fix and is a dependency this
  service does not otherwise need. See `core/ratelimit.py`.
- **The SPA stores its access and refresh tokens in `localStorage`**
  (`src/api/client.ts`, in the sibling SPA repository). Any script executing on that origin
  can read them. **Accepted in writing for the first production release on 2026-08-27** —
  the risk, the mitigations counted as consideration, what the httpOnly-cookie alternative
  costs, and the events that reopen the decision are in `docs/token-storage-decision.md`,
  which is owned, dated and carries a review date. Read it before reporting this: a way to
  defeat the CSP it relies on is a report worth making, because it voids the acceptance.
- **`SECRET_KEY` does two jobs**: it signs JWTs and it is the Fernet key for verify/reset
  links. Rotating it therefore invalidates outstanding reset links. This is stated in
  `.env.example`.
- **OpenAPI is served outside production only.** In production `/docs`, `/redoc` and
  `/openapi.json` do not exist rather than being unauthorised — absence is the stronger
  property, and it is deliberate.

## Running it safely

- Set `ENVIRONMENT=prod`. It refuses `*` in `CORS_ORIGINS`, a short or low-entropy
  `SECRET_KEY`, a SQLite `DATABASE_URL` and a placeholder `SUPERADMIN_PASSWORD`, turns off
  OpenAPI and turns on HSTS. (An `ALGORITHM` outside the HMAC allow-list, `none` included,
  is refused in every environment.)
- Change the seeded accounts' passwords after first sign-in.
- Set `TRUSTED_PROXY_IPS` to your proxy's address (or the smallest CIDR containing it) if
  anything sits in front of this service. Behind a proxy with this unset, every client
  shares one rate-limit bucket; set to something too wide, a caller can forge its own
  client address. `docker-compose.yml` sets it for the shipped topology.
- List site-specific literal secrets in `LOG_EXTRA_SECRETS` so the log filter masks them
  too.
- Run `make audit` before a deployment.

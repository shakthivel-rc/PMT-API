# Security exceptions

Dependency advisories that this deployment knowingly ships with, each with the reason it
cannot be upgraded and the date the waiver stops being accepted.

**This file is read by a machine.** `scripts/audit_deps.py` parses the table below on every
`make audit` and every nightly run. An advisory listed here does not fail the build until
its expiry date; on that date it fails again, and somebody has to decide a second time.
That expiry is the whole mechanism — a waiver with no end is a `|| true` with extra steps.

**The bar for adding a row is high.** Upgrading is almost always cheaper than arguing, and
"the upgrade is a bit of work" is not a reason. Legitimate reasons look like: the fix is in
a major version that requires a migration we have scheduled; the advisory does not apply to
how we call the library, *demonstrated* rather than asserted; there is no fix published yet.

## Format

One row per advisory. The columns are fixed and the parser depends on them:

```
| ADVISORY-ID | package | YYYY-MM-DD | why it cannot be upgraded, in one sentence |
```

* **ADVISORY-ID** — as `pip-audit` or `npm audit` reports it: `PYSEC-…`, `GHSA-…`, `CVE-…`.
  Copy it exactly; a typo silently waives nothing.
* **package** — the distribution name, for the reader. The parser matches on the id alone.
* **YYYY-MM-DD** — when this waiver expires. Pick the date you actually expect to have done
  the work, not a year out. A malformed date reads as already expired, which fails safe.
* **reason** — one sentence. Written for whoever is looking at a red build in six months
  and has no memory of this decision.

## Current exceptions

| Advisory | Package | Expires | Reason |
|---|---|---|---|

**None.** The runtime dependency set reported zero advisories at the last measurement
(2026-08-27, `pip-audit -r requirements.txt`), and the SPA reported zero (`npm audit`, with
and without dev dependencies). The table is empty because the backlog was cleared rather
than waived — see the upgrade note at the top of `requirements.txt` for what moved and why.

## Development dependencies

`requirements-dev.txt` is **reported, not gated** (`python scripts/audit_deps.py --dev`).
An advisory in a package that only ever runs on a developer's machine or in CI is worth
knowing about and is not worth failing a nightly for. That is a judgement, not an oversight,
and it is written down here so it can be revisited: the moment a dev dependency starts
being installed into the runtime image, it stops being a dev dependency and this paragraph
stops applying.

Known and accepted at 2026-08-27:

* **pytest 8.3.4 — PYSEC-2026-1845**, fixed in 9.0.3. pytest 9 is a major release and the
  upgrade is tracked as its own piece of work rather than smuggled into a security commit;
  pytest never runs anywhere but a developer's machine and CI.

## Known limitations that are not advisories

These are properties of this service's own design, not of a third-party package, so they do
not belong in the table above. They are recorded here so there is one place to look.

* **The inbound rate limiter counts in-process** (`core/ratelimit.py`), so N workers means
  N times the configured limit. Exact at one worker. Documented at the setting, in
  `.env.example`, and in `SECURITY.md`.
* **SPA tokens live in `localStorage`.** Assessed, mitigated by the SPA's CSP, and decided
  rather than left open — see `docs/token-storage-decision.md`.
* **`SECRET_KEY` does two jobs** — it signs JWTs and encrypts verify/reset links, so a
  routine JWT rotation invalidates outstanding reset links. Separating them is a migration
  rather than a hardening edit. Documented at the setting in `.env.example`.

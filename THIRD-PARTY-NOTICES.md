# Third-party notices — Base Project API

Base Project API is licensed under Apache-2.0 (see [LICENSE](LICENSE)).

**This repository redistributes no third-party code.** Dependencies are pinned in
`requirements.txt` and resolved from PyPI at install time; none is vendored into the source
tree. The Docker image built from [Dockerfile](Dockerfile) *does* embed them, so this
catalogue is what accompanies a redistributed image.

Re-verified on 2026-09-28, when this project was split out as a generic base, against the
package metadata in the development virtualenv. That pass also corrected entries the
previous revision had wrong (`click`, `python-dotenv`, `uvicorn` and `idna` are
BSD-3-Clause; `email-validator` is Unlicense; `cffi` is MIT-0; `dnspython` is ISC).
`httptools`, `uvloop` and `watchfiles` — uvicorn's optional speed-ups, pinned here but absent
from that virtualenv — are catalogued from their upstream metadata. Re-check with
`pip-licenses` (or `importlib.metadata`) whenever `requirements.txt` changes.

**There is no GPL, LGPL, AGPL or SSPL in the runtime dependency set, and no MPL either.**

2026-10-05, reports (docs/design/08-reports.md): `openpyxl` and its one dependency
`et-xmlfile` (both MIT) write the XLSX exports; `tzdata` (Apache-2.0) supplies the IANA time
zone database to `zoneinfo` on images that ship without one. PDF exports are written by a
small in-repository writer (`services/reports/exporters/pdf_writer.py`) rather than a
library: `fpdf2` is LGPL-3.0, which the sentence above rules out; WeasyPrint needs Pango and
its system libraries in the image; ReportLab pulls in Pillow. Verified against the package
metadata in the development virtualenv that day.

---

## Runtime dependencies (`requirements.txt`)

Pinned packages, plus the three transitive ones the pins pull in unpinned (`cffi` and
`pycparser` under `cryptography`, `dnspython` under `email-validator`).

| License | Packages |
|---|---|
| **MIT** | openpyxl, et-xmlfile, fastapi, pydantic, pydantic-core, SQLAlchemy, alembic, Mako, PyJWT, PyMySQL, h11, anyio, annotated-types, annotated-doc, typing-inspection, httptools, watchfiles |
| **BSD-3-Clause** | starlette, uvicorn, click, python-dotenv, MarkupSafe, idna, pycparser |
| **Apache-2.0** | bcrypt, tzdata |
| **Apache-2.0 OR BSD-3-Clause** (dual — licensee's choice) | cryptography |
| **MIT OR Apache-2.0** (dual — licensee's choice) | uvloop |
| **MIT AND PSF-2.0** (some files under PSF-2.0) | greenlet |
| **MIT-0** (MIT without the attribution condition) | cffi |
| **ISC** | dnspython |
| **Unlicense** (public-domain dedication) | email-validator |
| **PSF-2.0** | typing_extensions |

There is no HTTP client in the runtime set: nothing in the service makes an outbound HTTP
call, and email goes out over the standard library's `smtplib`. `httpx`, which FastAPI's
`TestClient` needs, is a development dependency only (below).

## Development dependencies (`requirements-dev.txt`)

Never installed into the Docker image — [Dockerfile](Dockerfile) installs only
`requirements.txt` and copies forward only that virtualenv. All obligations in these licenses
are conditioned on distribution, so a dev-only dependency imposes nothing on the distributed
product.

pytest, pytest-cov, httpx (FastAPI's `TestClient` and the live suite), httpcore, ruff, mypy,
pip-audit, pre-commit and their transitive dependencies are MIT, BSD, Apache-2.0, ISC or
PSF-2.0 — with one exception: `certifi`, httpx's CA bundle, is **MPL-2.0**. That obligation
is file-scoped (§1.10, §3.1) — it applies only to a distributed, *modified* `certifi` file —
and it never reaches the image.

## mysqlclient — removed

`mysqlclient==2.2.7` was pinned here and was **GPL-2.0-or-later**: no linking exception, no
FOSS exception, not dual-licensed at the package level. (`MySQLdb/_mysql.c` alone carries a
permissive Comstar.net alternative; the Python wrapper modules do not, so the package as a
whole was GPL.)

No obligation was ever triggered — GPLv2 is distribution-triggered and this service runs
server-side; it is not AGPL, so serving HTTP responses is not a triggering act; and no
first-party module ever imported `MySQLdb`. Every `DATABASE_URL` this project writes uses the
`mysql+pymysql` driver.

It has now been **removed from `requirements.txt`**, as the previous revision of this file
recommended. Apache-2.0 and GPLv2 are one-way incompatible — GPLv2 cannot absorb Apache-2.0
code — so an Apache-2.0 project shipping a GPLv2 pin was a signal reviewers would stop on
even though nothing here actually triggered it. `PyMySQL==1.1.1` (MIT) is pure Python, is
what every configuration already used, and needs no compiler — which also removed
`libmysqlclient-dev` and `pkg-config` from the native install path.

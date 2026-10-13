# Base Project API image.
#
# Two stages so no compiler reaches the running image. Every pinned package ships a
# manylinux wheel on the platforms this is built for, so the builder's toolchain is
# insurance for the one that does not — an unusual architecture, or a yanked wheel — rather
# than a standing requirement. The builder produces a virtualenv; the runtime stage copies
# it and nothing else.
#
# There is no MySQL client library here on purpose. The driver is PyMySQL, which is pure
# Python; the GPL `mysqlclient` C extension that used to need `default-libmysqlclient-dev`,
# `pkg-config` and `libmariadb3` has been removed (see THIRD-PARTY-NOTICES.md).

# ---------------------------------------------------------------------------- builder ---
FROM python:3.12-slim AS builder

# apt over HTTPS, in both stages. The base image's sources say http://deb.debian.org, and on
# 2026-10-01 all three of `make setup`'s build attempts failed on an office network whose
# firewall (Fortinet, going by its block page) inspects cleartext downloads and blocks or cuts
# off some .deb files based on their content. It answered some requests with its own block
# page, so the build died with
#   E: Failed to fetch http://deb.debian.org/debian/pool/main/b/binutils/libctf0_2.44-3_amd64.deb
#      403  Forbidden [IP: 151.101.2.132 80]
# and exit code 100 — which names Debian's CDN, reads exactly like a broken mirror, and is
# neither. (It also reset transfers just short of the end, and one cut-off retry ended in a
# "Hash Sum mismatch".) HTTPS hides what is being fetched from the inspector: on that network
# the 74 packages these two steps need arrived 74 of 74 over HTTPS, against two failures over
# HTTP and one file blocked six times out of six. Trust is unchanged — apt verifies every
# package against the signed index either way — and nothing new is required: the python slim
# images ship ca-certificates, and the pip step below already depends on HTTPS to reach PyPI.
# If that network ever starts intercepting TLS, this step fails with "Certificate verification
# failed" instead. The fix then is the corporate CA in the image (pip will need it too), not a
# return to http://.
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copied on its own so a source-only change does not reinstall every pinned package — and
# without requirements-dev.txt, which this stage never installs, so bumping a test-only pin
# does not either.
COPY requirements.txt ./
# --retries/--timeout are not decoration. pip defaults to 5 retries at a 15 s timeout, and
# one dropped connection anywhere in the pinned set fails the whole build with
#   ERROR: Could not find a version that satisfies the requirement bcrypt==4.3.0
#          (from versions: none)
# which names a package and a pin, reads exactly like a bad requirement, and sends the
# reader to PyPI to check a version that was never the problem. The index page simply did
# not arrive. Two minutes of build are thrown away for a blip that a retry would have
# absorbed, and on a proxied or corporate network that blip is not rare.
RUN pip install --no-cache-dir --upgrade pip --retries 10 --timeout 60 \
    && pip install --no-cache-dir --retries 10 --timeout 60 -r requirements.txt

# ---------------------------------------------------------------------------- runtime ---
FROM python:3.12-slim

# curl is what the compose healthcheck uses to decide the API is actually serving rather
# than merely started. Nothing else is needed: the venv copied below is self-contained.
# Fetched over HTTPS for the reason given in the builder stage.
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# WORKDIR is load-bearing, not cosmetic: `load_dotenv()` reads the *current working
# directory* and `get_settings()` runs at import time, so starting anywhere else makes the
# process abort before uvicorn binds. See CLAUDE.md §14.
WORKDIR /app

COPY . .

# Runs as a non-root user. The application writes nothing to disk — all state lives in the
# database — so /app stays owned by root and read-only to the process.
RUN useradd --create-home --uid 10001 app \
    && chmod -R a+rX /app
USER app

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --start-period=40s --retries=5 \
    CMD curl -fsS http://localhost:8000/api/v1/health/ready || exit 1

ENTRYPOINT ["./scripts/docker-entrypoint.sh"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

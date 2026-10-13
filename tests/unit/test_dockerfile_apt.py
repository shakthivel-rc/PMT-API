"""apt inside the image fetches over HTTPS, in every stage that uses it.

The python slim base images ship their apt sources as `http://deb.debian.org`. On
2026-10-01 all three of `make setup`'s build attempts failed on an office network whose
firewall inspects cleartext downloads: it answered some of the `.deb` requests with its own
block page ("The file "libctf0_2.44-3_amd64.deb" has been blocked due to its file type
and/or properties"), and apt reported

    E: Failed to fetch http://deb.debian.org/debian/pool/main/b/binutils/libctf0_2.44-3_amd64.deb
       403  Forbidden [IP: 151.101.2.132 80]

and exit code 100 — an error that names Debian's CDN and sends the reader to check a mirror
that was never the problem. Over HTTPS the firewall cannot see what is being fetched, and
the same packages came through cleanly.

The repair is one `sed` per stage, which is exactly the kind of line that looks redundant to
anyone who has never been on that network. This keeps it, and keeps it ahead of
`apt-get update` so the index travels over HTTPS as well as the packages; a switch that
drifts below the install it was meant to protect fetches nothing over HTTPS at all.
"""
import pathlib
import re

import pytest

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent

#: Options may sit between the command and `update` (`apt-get -qq update`,
#: `apt-get -o Acquire::Retries=5 update`). A pattern that wanted the two adjacent passes a
#: stage that has dropped the switch but spells its update that way.
APT_UPDATE = re.compile(r"\bapt(?:-get)?\s+(?:[^&|;\n]*?\s)?update\b")
#: Matched loosely on purpose: any rewrite of the sources to https counts, so this pins the
#: decision rather than one spelling of it.
HTTPS_SOURCES = re.compile(r"https://deb\.debian\.org")


def _stages(dockerfile: str) -> list[str]:
    """The Dockerfile split at each FROM, comments dropped and continuations joined.

    Comments go first so that a comment *mentioning* https://deb.debian.org cannot stand in
    for the instruction that does it.
    """
    code = re.sub(r"(?m)^\s*#.*\n", "", dockerfile)
    code = re.sub(r"\\\n", " ", code)
    return re.split(r"(?im)^FROM\s", code)[1:]


def _stages_fetching_over_http(dockerfile: str) -> list[int]:
    """Indexes of the stages whose first `apt-get update` runs before the switch to HTTPS."""
    bad = []
    for index, stage in enumerate(_stages(dockerfile)):
        update = APT_UPDATE.search(stage)
        if update and not HTTPS_SOURCES.search(stage[: update.start()]):
            bad.append(index)
    return bad


class TestAptFetchesOverHttps:
    def test_the_image_still_uses_apt(self):
        """Guards the guard: with no `apt-get update` left to find, the test below passes
        trivially."""
        dockerfile = (ROOT / "Dockerfile").read_text()
        # Two today: the builder's toolchain and the runtime's curl. The floor is one so
        # either can be retired without editing this line, and above zero so a parser that
        # silently stopped matching cannot pass.
        assert sum(bool(APT_UPDATE.search(s)) for s in _stages(dockerfile)) >= 1

    def test_every_stage_switches_to_https_before_apt_get_update(self):
        bad = _stages_fetching_over_http((ROOT / "Dockerfile").read_text())
        assert not bad, (
            f"Dockerfile stage(s) {bad} run apt-get update over plain HTTP. Switch first:\n"
            "  sed -i 's|http://deb.debian.org|https://deb.debian.org|g' "
            "/etc/apt/sources.list.d/debian.sources\n"
            "A firewall that inspects cleartext downloads answers .deb requests with its own "
            "403 page,\nand the build fails naming Debian's CDN. See CLAUDE.md §14."
        )

    def test_the_shapes_it_exists_to_catch_are_caught(self):
        """The negative controls. The builder stage as it stood until 2026-10-01 must fail;
        so must a switch that comes after the update it was meant to protect, a runtime stage
        that leans on the builder's switch, and an update with options in front of it."""
        before = (
            "FROM python:3.12-slim AS builder\n"
            "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
            "        build-essential \\\n"
            "    && rm -rf /var/lib/apt/lists/*\n"
        )
        too_late = (
            "FROM python:3.12-slim\n"
            "# sources are switched to https://deb.debian.org below\n"
            "RUN apt-get update \\\n"
            "    && sed -i 's|http://deb.debian.org|https://deb.debian.org|g' "
            "/etc/apt/sources.list.d/debian.sources\n"
        )
        # Every FROM starts again from the base image's http:// sources, so the builder's
        # switch does nothing for the runtime stage. This is what makes "every stage" a check.
        half_done = (
            "FROM python:3.12-slim AS builder\n"
            "RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' "
            "/etc/apt/sources.list.d/debian.sources && apt-get update\n"
            "FROM python:3.12-slim\n"
            "RUN apt-get update && apt-get install -y --no-install-recommends curl\n"
        )
        options_first = (
            "FROM python:3.12-slim\n"
            "RUN apt-get -o Acquire::Retries=5 update && apt-get install -y curl\n"
        )
        assert _stages_fetching_over_http(before) == [0]
        assert _stages_fetching_over_http(too_late) == [0]
        assert _stages_fetching_over_http(half_done) == [1]
        assert _stages_fetching_over_http(options_first) == [0]

"""A setting the documentation offers must actually reach the process.

`core/config.py` is the only place that reads the environment. `.env.example` is where an
operator learns what they may set. `docker-compose.yml` is the only install path this
project ships — and compose passes **only the keys its `environment:` block names**.

Those three had drifted. Eight settings were readable and documented and not forwarded,
including `AUTH_MAX_FAILED_LOGIN_ATTEMPTS`, `AUTH_LOCKOUT_MINUTES` and
`PASSWORD_MIN_LENGTH`: an operator could tighten the password policy in `.env`, restart,
and get the shipped default with nothing anywhere saying so.

This is not a style rule. A security control that silently keeps its default is worse than
one that is absent, because the absent one is visible.

The failure direction is deliberate: adding a setting to `core/config.py` fails this test
until it is documented *and* delivered. That is three lines of work at the moment the
setting is added, rather than an incident later.
"""
import pathlib
import re

import pytest

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent

#: How `core/config.py` reads the environment.
READERS = r"_(?:required|optional_int|optional_bool|split_csv|optional_csv)\(\s*\"([A-Z_0-9]+)\""
BARE_GETENV = r"os\.getenv\(\s*\"([A-Z_0-9]+)\""

#: Settings that legitimately do not belong in one of the two files, with the reason.
#: Empty today. An entry here is a decision, not a shortcut.
EXEMPT: dict[str, str] = {}


def _settings_read() -> set[str]:
    source = (ROOT / "core" / "config.py").read_text()
    return (set(re.findall(READERS, source)) | set(re.findall(BARE_GETENV, source))) - set(EXEMPT)


class TestEverySettingIsDocumentedAndDelivered:
    def test_the_reader_still_finds_settings(self):
        """Guards the guard: a refactor of `core/config.py` that changed how the
        environment is read would otherwise turn both tests below into no-ops."""
        found = _settings_read()
        # 22 on the day of the base-project split. The floor sits a little under that so a
        # setting can be retired without editing this line, and far enough above zero that
        # a reader which silently stopped matching cannot pass.
        assert len(found) >= 20, f"only found {len(found)} settings — the regex is stale"
        assert {"DATABASE_URL", "SECRET_KEY", "CORS_ORIGINS"} <= found

    def test_every_setting_appears_in_env_example(self):
        """Present, but not necessarily active - a commented `# KEY=` line counts as
        documented.

        Documentation is the requirement here; whether a setting ships switched on is a
        separate decision, made where that setting is defined.
        """
        env_example = (ROOT / ".env.example").read_text()
        missing = sorted(
            name for name in _settings_read()
            if not re.search(rf"^#?\s*{name}=", env_example, re.MULTILINE)
        )
        assert not missing, (
            "read by core/config.py and absent from .env.example:\n  "
            + "\n  ".join(missing)
            + "\n\nA setting an operator cannot discover does not exist to them."
        )

    def test_every_setting_is_forwarded_by_docker_compose(self):
        compose = (ROOT / "docker-compose.yml").read_text()
        missing = sorted(
            name for name in _settings_read()
            if not re.search(rf"^\s+{name}:", compose, re.MULTILINE)
        )
        assert not missing, (
            "read by core/config.py and NOT in the api service's environment block:\n  "
            + "\n  ".join(missing)
            + "\n\ncompose passes only what it names, so these are pinned to their "
            "defaults in\nthe only install path this project ships — however carefully "
            "the operator sets them."
        )

    def test_every_exemption_carries_a_reason(self):
        for name, why in EXEMPT.items():
            assert len(why) > 40, f"{name}: write down why it is exempt"

"""Credential primitives: the OTP generator, and the scanner that guards commits.

Both are small and both are the kind of thing that is wrong for years because nothing
looks at them.
"""
import importlib.util
import pathlib
import statistics
from unittest.mock import patch

import pytest

from controllers.auth_controller import _generate_otp

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent


class TestOtpGeneration:
    """`random.randint` was here until 2026-08-26.

    `random` is the Mersenne Twister: deterministic, and its internal state is recoverable
    from a few hundred outputs. For a dice roll that is fine. For the second factor on a
    sign-in it is the entire attack, because predicting the code is the same as knowing it.
    """

    def test_it_draws_from_the_cryptographic_generator(self):
        """Pinned by patching `secrets`, because the shape of the output cannot prove this.

        A statistical test cannot tell `random` from `secrets` — both are uniform. The only
        honest assertion is about which module is called, so that is what is asserted.
        """
        with patch("controllers.auth_controller.secrets.randbelow", return_value=0) as drawn:
            assert _generate_otp() == "100000"
        drawn.assert_called_once_with(900_000)

    def test_it_is_always_six_digits(self):
        """The range matters: `randbelow(900000) + 100000` is [100000, 999999] inclusive,
        which is exactly what `randint(100000, 999999)` produced. A five-digit code would
        be a silent 10x reduction in the search space."""
        for _ in range(500):
            otp = _generate_otp()
            assert len(otp) == 6 and otp.isdigit()
            assert 100_000 <= int(otp) <= 999_999

    def test_the_range_ends_are_reachable(self):
        with patch("controllers.auth_controller.secrets.randbelow", return_value=899_999):
            assert _generate_otp() == "999999"

    def test_successive_codes_differ(self):
        """Not a randomness test — a "did somebody cache it" test. A generator returning
        one value for a whole process passes every other assertion in this class."""
        codes = {_generate_otp() for _ in range(200)}
        assert len(codes) > 190, "the generator is repeating itself far too often"

    def test_the_distribution_is_not_obviously_degenerate(self):
        """Weak on purpose. This cannot detect a bad PRNG and does not claim to; it
        detects an off-by-a-power-of-ten range bug, which is the mistake that actually
        happens when somebody edits the line above."""
        sample = [int(_generate_otp()) for _ in range(2000)]
        assert 400_000 < statistics.mean(sample) < 700_000


def _load_scanner():
    spec = importlib.util.spec_from_file_location(
        "staged_secrets", ROOT / "scripts" / "check_staged_secrets.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestStagedSecretScanner:
    """The pre-commit gate. A gate that has never been shown to fire is a decoration."""

    @pytest.fixture(scope="class")
    def scanner(self):
        return _load_scanner()

    def _hits(self, scanner, line: str) -> list[str]:
        if (scanner.COMMENT.match(line)
                or scanner.PLACEHOLDER.search(line)
                or scanner.NOT_A_SECRET.search(line)):
            return []
        return [label for label, pattern in scanner.PATTERNS if pattern.search(line)]

    @pytest.mark.parametrize("line", [
        "SECRET_KEY=MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
        # The spaced, quoted forms: a Python settings module and a YAML file.
        "SECRET_KEY = 'MTExMTExMTExMTExMTExMTExMTExMTExMTExMTExMTE='",
        'SECRET_KEY: "MTExMTExMTExMTExMTExMTExMTExMTExMTExMTExMTE="',
        'token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlF"',
        "DATABASE_URL=mysql+pymysql://root:hunter2horse@db:3306/app",
        "-----BEGIN RSA PRIVATE KEY-----",
        "-----BEGIN OPENSSH PRIVATE KEY-----",
    ])
    def test_it_catches_a_real_credential(self, scanner, line):
        assert self._hits(scanner, line), f"not caught: {line[:60]}"

    @pytest.mark.parametrize("line", [
        "# sqlalchemy.url = mysql+pymysql://user:password@localhost:3306/x",
        "SECRET_KEY=",
        "DATABASE_URL=mysql+pymysql://user:password@127.0.0.1:3306/base_project",
        'url = f"mysql+pymysql://{user}:{pw}@{host}"',
        "# `mysql+pymysql://user:PASSWORD@db:3306/name` — a password containing @ breaks it",
        # The convention for a value that genuinely is not one: say so in the value. Used
        # by the MySQL service container in .github/workflows/ci.yml.
        "TEST_DATABASE_URL: mysql+pymysql://base_project:ci-user-not-a-secret@127.0.0.1:3306/db",
    ])
    def test_it_leaves_documentation_alone(self, scanner, line):
        """The two false positives it produced on its first run were both of this shape.
        A scanner whose first run is noise is a scanner somebody deletes."""
        assert not self._hits(scanner, line), f"false positive: {line[:60]}"

    def test_a_private_key_is_not_mistaken_for_a_sql_comment(self, scanner):
        """`--` was in the comment filter for one revision, which silently disabled the
        single most important pattern in the file. This is that regression."""
        assert self._hits(scanner, "-----BEGIN PRIVATE KEY-----")

    def test_env_filenames_are_refused_by_name(self, scanner):
        for path in (".env", ".env.local", "config/.env.prod", ".env.bak.20260826"):
            assert scanner.FORBIDDEN_PATHS.search(path), path
        assert not scanner.FORBIDDEN_PATHS.search(".env.example")

    def test_the_tree_is_clean(self, scanner):
        """Whole-repo, the same call CI makes. If this fails, something is committed that
        should not be — read the output before changing the patterns."""
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "check_staged_secrets.py"), "--all"],
            cwd=ROOT, capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stdout

    @pytest.mark.parametrize(("path", "patterns", "is_dir", "expected"), [
        # A leading slash anchors to the root: `/config` is not every `config` directory.
        ("config/keys.py", ["/config"], False, True),
        ("core/config/keys.py", ["/config"], False, False),
        # `*` stays inside one component, as in git; fnmatch's crosses `/`.
        ("docs/leak.md", ["docs/*.md"], False, True),
        ("docs/sub/leak.md", ["docs/*.md"], False, False),
        # A trailing slash names directories only.
        ("venv/lib/x.py", ["venv/"], False, True),
        ("venv", ["venv/"], False, False),
        # `**` as a whole component spans directories.
        ("a/x/y/b.txt", ["a/**/b.txt"], False, True),
        # Last match wins, but nothing under an excluded directory comes back.
        (".env.example", [".env.*", "!.env.example"], False, False),
        ("build/keep.txt", ["build/", "!build/keep.txt"], False, True),
    ])
    def test_the_fallback_reads_gitignore_as_git_does(
        self, scanner, path, patterns, is_dir, expected
    ):
        """Outside a checkout the scanner walks the tree and skips what `.gitignore` names.
        Skipping MORE than git would is a silent hole — a secret in a path git would commit
        and this scan never read — so the matcher follows git's anchoring, directory-only
        and `*`-within-a-component rules rather than fnmatch's."""
        rules = scanner.parse_ignore_rules(patterns)
        assert scanner.ignored(path, rules, is_dir=is_dir) is expected

    def test_a_tree_an_enclosing_repository_does_not_track_is_still_scanned(self, tmp_path):
        """Copied into a monorepo, not yet added: `git ls-files` succeeds and lists
        nothing, and the scan used to report "OK — 0 tracked files clean" over a live key.
        """
        import base64
        import os
        import shutil
        import subprocess
        import sys

        if not shutil.which("git"):
            pytest.skip("git is not on PATH")
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        project = tmp_path / "project"
        (project / "scripts").mkdir(parents=True)
        shutil.copy(ROOT / "scripts" / "check_staged_secrets.py", project / "scripts")
        (project / "core").mkdir()
        # Generated, never a literal, so this file is not itself a finding.
        key = base64.urlsafe_b64encode(b"1" * 32).decode()
        (project / "core" / "leak.txt").write_text(f"SECRET_KEY={key}\n")

        # Without pytest-cov's variables. Under `--cov` they make the child record coverage
        # with the config it finds in ITS working directory — none, here, so statement-only
        # data — and the parent then refuses to combine that with its own branch data.
        env = {k: v for k, v in os.environ.items() if not k.startswith("COV_CORE_")}
        result = subprocess.run(
            [sys.executable, "scripts/check_staged_secrets.py", "--all"],
            cwd=project, env=env, capture_output=True, text=True,
        )
        assert result.returncode == 1, result.stdout
        assert "core/leak.txt:1" in result.stdout

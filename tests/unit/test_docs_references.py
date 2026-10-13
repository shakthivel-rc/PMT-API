"""A document cited by section number must exist, and have that section.

`Dockerfile`, `Makefile` and `scripts/bootstrap.sh` each cited "CLAUDE.md §N" for months
while `CLAUDE.md` did not exist. The citations were not decoration — one of them points at
the password-generation rule that `bootstrap.sh` implements — so what was actually missing
was the statement of a rule the code enforces, and there was no way to review it against
the code.

The file exists now. This is what stops the pair coming apart again, in either direction:
a new citation to a section nobody wrote fails, and deleting a section that something cites
fails.
"""
import pathlib
import re

import pytest

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent

#: Files that are allowed to cite a design document, and are scanned for citations.
SCANNED = ("Dockerfile", "Makefile", "README.md", "CLAUDE.md", "SECURITY.md")
#: `docs/*.md` was added on 2026-08-28 and it is the reason this list is worth re-reading.
#:
#: Until then this gate scanned the build files, the scripts and the four top-level
#: documents — and NOT the directory where the design documents actually live. Every
#: architecture decision record, every audit and the readiness plan itself could cite
#: `CLAUDE.md §99` and nothing anywhere would notice. That was demonstrated rather than
#: assumed: a citation in `docs/token-storage-decision.md` was deliberately broken to §99
#: and the suite stayed green.
#:
#: The documents in `docs/` are the ones that cite most heavily, because explaining a
#: decision means pointing at the rule it follows from. They were the least covered.
SCANNED_GLOBS = ("scripts/*.sh", "scripts/*.py", "scripts/lib/*.sh", "docs/*.md")

CITATION = re.compile(r"([A-Z][A-Za-z0-9_.-]*\.md)\s*§\s*(\d+)")


def _sources():
    for name in SCANNED:
        path = ROOT / name
        if path.exists():
            yield path
    for pattern in SCANNED_GLOBS:
        yield from sorted(ROOT.glob(pattern))


def _sections(document: pathlib.Path) -> set[int]:
    """Section numbers, taken from `## N. Title` headings."""
    return {
        int(match.group(1))
        for match in re.finditer(r"^##\s+(\d+)\.", document.read_text(), re.MULTILINE)
    }


class TestSectionCitations:
    def test_every_cited_section_exists(self):
        broken: list[str] = []
        cache: dict[str, set[int]] = {}

        for source in _sources():
            text = source.read_text(encoding="utf-8", errors="ignore")
            for line_number, line in enumerate(text.splitlines(), start=1):
                for document, section in CITATION.findall(line):
                    target = ROOT / document
                    if not target.exists():
                        broken.append(
                            f"{source.relative_to(ROOT)}:{line_number} cites {document}, "
                            f"which does not exist"
                        )
                        continue
                    if document not in cache:
                        cache[document] = _sections(target)
                    if int(section) not in cache[document]:
                        broken.append(
                            f"{source.relative_to(ROOT)}:{line_number} cites "
                            f"{document} §{section}, which has no such section"
                        )

        assert not broken, (
            "Citations that do not resolve:\n  "
            + "\n  ".join(broken)
            + "\n\nWrite the section, or change the citation. A pointer to nothing is worse "
            "than no pointer:\nit tells the reader a rule was written down somewhere."
        )

    def test_the_three_original_citations_are_covered(self):
        """Named explicitly, so deleting the file does not merely make this test vacuous.

        Without this, removing CLAUDE.md and every reference to it would leave the check
        above passing — nothing cited, nothing broken — which is not the same as the
        problem being solved.
        """
        claude = ROOT / "CLAUDE.md"
        assert claude.exists(), (
            "CLAUDE.md is cited by the Dockerfile, the Makefile and bootstrap.sh"
        )
        sections = _sections(claude)
        for expected in (7, 14, 17):
            assert expected in sections, f"CLAUDE.md has no §{expected}"

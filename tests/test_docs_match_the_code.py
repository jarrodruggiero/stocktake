"""Docs that name a thing the code has: the thing exists, as the docs say.

A review found seventeen places where the docs and decisions had drifted from
the code. Most were one-off corrections; these are the kinds that come back.
"""

from __future__ import annotations

import re
from pathlib import Path

from app import setupwizard

ROOT = Path(__file__).resolve().parent.parent
NUMBERS = {"seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11}
DOCS = sorted([*ROOT.joinpath("docs").rglob("*.md"), ROOT / "AGENTS.md", ROOT / "README.md",
               *ROOT.joinpath(".github").glob("*.md")])


def test_every_path_the_docs_lint_exists():
    """Three pages told contributors to run `ruff check app appcore tests
    tools`, which fails: appcore left this repository to become a pinned
    dependency. CI and the pull-request template had been corrected."""
    missing = [(doc.name, path) for doc in DOCS
               for command in re.findall(r"ruff check ([^`\n]+)", doc.read_text(encoding="utf-8"))
               for path in command.split()
               if not path.startswith("-") and not (ROOT / path).exists()]

    assert missing == []


def test_no_line_of_the_docs_starts_with_a_number_sign():
    """MkDocs reads `#12 safe rather than…` at the start of a line as a
    heading, so a paragraph that wrapped before a decision's number rendered
    as a heading in the middle of it."""
    found = [f"{doc.relative_to(ROOT)}:{number}" for doc in DOCS
             for number, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1)
             if re.match(r"#\d", line)]

    assert found == []


def test_the_getting_started_guide_walks_the_wizard_as_it_is():
    """It said seven steps when there were nine, and named a step by an old
    title."""
    guide = (ROOT / "docs" / "getting-started" / "index.md").read_text(encoding="utf-8")
    listed = re.findall(r"^\d+\. \*\*(.+?)\*\*", guide.split("## 2.")[0], re.M)
    said = re.search(r"walks through (\w+) steps", guide)

    assert listed == [label for _key, label in setupwizard.STEPS]
    assert said and NUMBERS.get(said.group(1)) == len(setupwizard.STEPS)

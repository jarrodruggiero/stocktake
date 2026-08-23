"""The coverage policy, enforced rather than documented.

Two things keep the gate honest, checked here rather than left to intention:
every exclusion carries a written justification — a bare `# pragma: no cover`
fails, as does an `omit` entry with no "Justification:" above it — and
`fail_under` may only ever go up.

Worth stating plainly, because a percentage invites more confidence than it
earns: line coverage says a line RAN, not that anything checked what it did.
The value in this suite comes from the hand-computed ground truths and the
mutation checks. This gate stops coverage rotting; it does not measure quality.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

APP = Path(__file__).parent.parent / "app"
PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"

# The floor. Raise it when coverage improves; never lower it.
#
# What is left uncovered is mostly async loops and the two functions that reach
# the outside world.
MINIMUM_GATE = 92

# A justification has to actually say something. Twenty characters is enough to
# stop "n/a" and "obvious" while not demanding an essay.
MIN_JUSTIFICATION = 20

PRAGMA = re.compile(r"#\s*pragma:\s*no cover(?P<reason>.*)$")


def python_files() -> list[Path]:
    return sorted(p for p in APP.rglob("*.py") if "migrations" not in p.parts)


def config() -> dict:
    return tomllib.loads(PYPROJECT.read_text())


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #

def test_the_coverage_gate_is_configured():
    report = config()["tool"]["coverage"]["report"]

    assert "fail_under" in report, (
        "Coverage is measured but not enforced — set fail_under in "
        "[tool.coverage.report], or the number is decoration."
    )


def test_the_gate_has_not_been_lowered():
    """The one thing that makes a coverage gate worth having.

    If a change drops coverage, the fix is a test. Editing this number down to
    make a change fit is the exact failure this guards against — so moving it
    requires editing MINIMUM_GATE here too, deliberately, in the same commit.
    """
    gate = config()["tool"]["coverage"]["report"]["fail_under"]

    assert gate >= MINIMUM_GATE, (
        f"The coverage gate is {gate}, below the recorded floor of "
        f"{MINIMUM_GATE}. Raise coverage rather than lowering the gate."
    )


# --------------------------------------------------------------------------- #
# Justifications
# --------------------------------------------------------------------------- #

def test_every_no_cover_pragma_says_why():
    """A bare `# pragma: no cover` is how a coverage target becomes a fiction.

    Write the reason on the same line:
        something_untestable()  # pragma: no cover - needs a real signal
    """
    unjustified: list[str] = []
    for path in python_files():
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            match = PRAGMA.search(line)
            if not match:
                continue
            reason = match.group("reason").strip(" -—:#")
            if len(reason) < MIN_JUSTIFICATION:
                unjustified.append(
                    f"{path.relative_to(APP.parent)}:{number}: {line.strip()}"
                )

    assert not unjustified, (
        "These exclusions have no justification (or too short a one). Say why "
        "the line cannot be tested, on the same line as the pragma:\n  "
        + "\n  ".join(unjustified)
    )


def justifications_in(section: str) -> dict[str, str]:
    """Map each entry of a pyproject list to the comment block above it.

    Walked forwards, line by line: a comment block accumulates and is claimed
    by the next entry it precedes, then resets. Scanning backwards from a
    string index is fiddlier and gets confused by the partial line the index
    lands in.
    """
    found: dict[str, str] = {}
    block: list[str] = []
    inside = False
    for raw in PYPROJECT.read_text().splitlines():
        line = raw.strip()
        if line.startswith(f"{section} = ["):
            inside = True
            continue
        if not inside:
            continue
        if line == "]":
            break
        if line.startswith("#"):
            block.append(line.lstrip("# "))
        elif line.startswith(("'", '"')):
            found[line.strip(",").strip("'\"")] = " ".join(block)
            block = []
    return found


def test_every_omitted_file_says_why():
    """An `omit` entry hides a whole file from the count, which is the largest
    thing anyone can do to this number — so each one is argued in the file."""
    omitted = config()["tool"]["coverage"]["report"].get("omit", [])
    assert omitted, "Nothing omitted — remove this test rather than leaving it vacuous."
    reasons = justifications_in("omit")

    for pattern in omitted:
        justification = reasons.get(pattern, "")
        assert "Justification:" in justification, (
            f"The omit entry {pattern!r} has no 'Justification:' comment above "
            f"it. Omitting a file removes it from the count entirely — say why "
            f"it cannot or should not be tested."
        )
        assert len(justification) > MIN_JUSTIFICATION * 2, (
            f"The justification for omitting {pattern!r} is too thin to review."
        )


def test_every_excluded_pattern_says_why():
    excluded = config()["tool"]["coverage"]["report"].get("exclude_also", [])
    reasons = justifications_in("exclude_also")

    for pattern in excluded:
        justification = reasons.get(pattern, "")
        assert "Justification:" in justification, (
            f"The exclude_also pattern {pattern!r} has no justification above it."
        )


# --------------------------------------------------------------------------- #
# Nothing quietly opts out
# --------------------------------------------------------------------------- #

def test_no_module_disables_coverage_wholesale():
    """A file-level `# coverage: off` would hide a module without appearing in
    the omit list, where it can at least be reviewed."""
    offenders = [
        str(p.relative_to(APP.parent))
        for p in python_files()
        if re.search(r"#\s*coverage:\s*off", p.read_text())
    ]

    assert not offenders, (
        "These files switch coverage off from the inside, which bypasses the "
        f"reviewable omit list: {offenders}"
    )


@pytest.mark.parametrize("module", ["queries", "fyreport", "charts_build", "exports",
                                    "tenancy", "auth"])
def test_the_modules_that_decide_money_and_access_are_never_omitted(module):
    """A belt on the omit list: whatever else gets excluded, the code that
    computes somebody's tax position or decides what they can see does not."""
    omitted = config()["tool"]["coverage"]["report"].get("omit", [])

    assert f"app/{module}.py" not in omitted

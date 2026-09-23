"""The workflows are checked against the repository they run in.

A release failed on `ruff check app appcore tests tools`: `appcore` left this
repository to become a pinned dependency, `ci.yml` was corrected at the time
and `release.yml` was not. Nothing noticed for three merges, because the
release workflow runs **only on a tag push** — so its first run after the move
was the release itself.

That is the shape of the problem worth guarding. A workflow that runs on every
pull request is tested constantly by being used; one that runs on a tag is
exercised at the worst possible moment. These tests read the YAML and hold it
against the tree, so a path that stops existing fails here rather than in the
middle of cutting a version.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))


def _steps(workflow: Path):
    """Every step of every job, with the job name, as (job, step) pairs."""
    document = yaml.safe_load(workflow.read_text())
    for job_name, job in (document.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            yield job_name, step


def _ruff_paths(workflow: Path) -> list[tuple[str, str]]:
    """(job, path) for every path handed to `ruff check`.

    Steps carrying a `working-directory` are skipped: their arguments are
    relative to somewhere that may only exist once the job has checked it out,
    which is exactly how `ci.yml` runs appcore's own suite.
    """
    found = []
    for job_name, step in _steps(workflow):
        if step.get("working-directory"):
            continue
        for line in str(step.get("run") or "").splitlines():
            match = re.search(r"ruff check\s+([^\n|&;]+)", line)
            if match:
                found.extend((job_name, arg) for arg in match.group(1).split()
                             if not arg.startswith("-"))
    return found


def test_there_are_workflows_to_check():
    """An empty sweep proves nothing — the guard's own smoke test."""
    assert len(WORKFLOWS) >= 2
    assert any(_ruff_paths(w) for w in WORKFLOWS)


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda w: w.name)
def test_every_linted_path_exists(workflow: Path):
    """The failure this file was written for.

    `appcore` sat in `release.yml` for three merges after it stopped being a
    directory here, and the first thing to notice was a release.
    """
    missing = [(job, path) for job, path in _ruff_paths(workflow)
               if not (ROOT / path).exists()]

    assert not missing, (
        f"{workflow.name} lints paths that do not exist: {missing}"
    )


def test_the_release_lints_exactly_what_ci_lints():
    """They are the same gate, so they must not drift.

    The release runs its own verify because a tag can point anywhere — CI
    having passed on some commit says nothing about the one being tagged. That
    only holds while the two check the same thing; a path added to one and not
    the other makes the release weaker than the pull request that preceded it,
    in the direction nobody looks.

    If they ever need to differ, change this test and say why in the same
    commit.
    """
    lints = {
        workflow.name: sorted(path for _job, path in _ruff_paths(workflow))
        for workflow in WORKFLOWS
        if _ruff_paths(workflow)
    }

    assert lints["release.yml"] == lints["ci.yml"], lints

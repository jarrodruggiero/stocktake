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

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
RELEASE = ROOT / ".github" / "workflows" / "release.yml"


def _steps(workflow: Path):
    """Every step of every job, with the job name, as (job, step) pairs."""
    document = yaml.safe_load(workflow.read_text(encoding="utf-8"))
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


def _release_steps() -> tuple[list[dict], dict | None]:
    """release.yml's steps in order, and the one that looks for a release."""
    steps = [step for _job, step in _steps(RELEASE)]
    return steps, next((s for s in steps if s.get("id") == "existing"), None)


def test_a_release_written_by_hand_keeps_its_notes():
    """Decision #129. Publishing a release in GitHub's UI pushes its tag, so the
    release already exists when this workflow runs, and the step that writes
    notes replaces whatever it finds. It may only run when there is none yet."""
    steps, check = _release_steps()
    writers = [s for s in steps
               if str(s.get("uses", "")).startswith("softprops/action-gh-release")]

    assert writers, "release.yml no longer writes a release — update this test"
    assert check is not None, "nothing looks for a release that already exists"
    assert steps.index(check) < min(steps.index(w) for w in writers)
    for writer in writers:
        assert writer.get("if") == "steps.existing.outputs.found == 'false'", writer


def test_the_release_verifies_the_commit_it_publishes():
    """Every job checks out the same commit, on a tag push and on a manual run.

    A manual run starts from the default branch and names its tag in
    `inputs.tag`. `verify` checked out without a `ref`, so a manual run tested
    the default branch and then published the tag. Its tests had never run on
    what it published, which is the one thing `verify` is there to prevent.
    """
    refs = {}
    for job_name, step in _steps(RELEASE):
        if str(step.get("uses", "")).startswith("actions/checkout@"):
            refs.setdefault(job_name, []).append((step.get("with") or {}).get("ref"))

    assert {"verify", "publish"} <= set(refs), refs
    assert all(ref == "${{ inputs.tag || github.ref }}"
               for job in refs.values() for ref in job), refs


@pytest.mark.parametrize("listed, gh_exit, expected", [
    ("https://github.com/o/r/releases/tag/v1.0.0", 0, "found=true"),
    ("", 0, "found=false"),
    # The API failed. Stopping is the only safe answer: "not found" would go
    # on to write over the notes this check exists to protect.
    ("", 1, None),
], ids=["exists", "absent", "api-failed"])
def test_the_release_check_says_whether_one_exists(tmp_path, listed, gh_exit, expected):
    """The check's own script, run against a stand-in `gh` as Actions runs it."""
    _steps_, check = _release_steps()
    assert check is not None, "nothing looks for a release that already exists"
    fake = tmp_path / "gh"
    fake.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {tmp_path}/args\n"
                    f"printf '%s' '{listed}'\nexit {gh_exit}\n", encoding="utf-8")
    fake.chmod(0o755)
    output = tmp_path / "github_output"
    output.touch()

    result = subprocess.run(
        ["bash", "-e", "-c", check["run"]], capture_output=True, text=True,
        env={"PATH": f"{tmp_path}:{os.environ['PATH']}", "GITHUB_OUTPUT": str(output),
             "GITHUB_REPOSITORY": "o/r", "TAG": "v1.0.0"})

    # Every release, every page: the lookup by tag skips drafts, and one page
    # stops at 30 releases, so a re-run for an older tag would miss its own.
    asked = (tmp_path / "args").read_text(encoding="utf-8").splitlines()
    assert "repos/o/r/releases" in asked and "--paginate" in asked, asked
    if expected is None:
        assert result.returncode != 0
        assert "found=" not in output.read_text(encoding="utf-8")
    else:
        assert result.returncode == 0, result.stderr
        assert output.read_text(encoding="utf-8").strip() == expected


def _action_refs(workflow: Path) -> list[str]:
    """Every action and reusable workflow a workflow runs, read from the parsed
    YAML, so text that merely says `uses:` (a CodeQL query suite inside an
    input) is not mistaken for one. Local `./` actions are this repository's."""
    jobs = (yaml.safe_load(workflow.read_text(encoding="utf-8")).get("jobs") or {}).values()
    refs = [job["uses"] for job in jobs if "uses" in job]
    refs += [step["uses"] for _job, step in _steps(workflow) if "uses" in step]
    return [ref for ref in refs if not ref.startswith("./")]


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda w: w.name)
def test_every_action_is_pinned_to_a_commit(workflow: Path):
    """A tag can be moved to other code by whoever controls the action's
    repository, and the next run here would run it with this repository's
    tokens. A commit cannot be moved. Dependabot keeps the pins current, and
    the comment says which release each one is, so a pin stays reviewable."""
    text = workflow.read_text(encoding="utf-8")
    # `owner/repo@<40-hex commit>  # vX.Y.Z`, as Dependabot writes and updates them.
    loose = [ref for ref in _action_refs(workflow)
             if not re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", ref)
             or not re.search(rf"uses:\s+{re.escape(ref)}\s+#\s+v\d+\.\d+\.\d+\s*$",
                              text, re.M)]

    assert not loose, f"{workflow.name} uses actions by tag: {loose}"


def _flag(args: list[str], name: str) -> str | None:
    return args[args.index(name) + 1] if name in args else None


def test_the_release_publishes_the_chart_at_the_tags_version(tmp_path):
    """The chart is packaged with the tag's version, after the image it names.

    Its appVersion was a number kept by hand in Chart.yaml, and it said 0.64.0
    for five releases: every Helm install ran 0.64.0. decisions.md #137.
    """
    steps = [step for _job, step in _steps(RELEASE)]
    chart = next((s for s in steps if s.get("id") == "chart"), None)
    assert chart is not None, "release.yml does not publish the Helm chart"
    image = next(i for i, s in enumerate(steps)
                 if str(s.get("uses", "")).startswith("docker/build-push-action"))
    version = next(i for i, s in enumerate(steps) if s.get("id") == "version")
    # A chart published before its image fails every install until the image
    # arrives, and for good if the image push then fails.
    assert steps.index(chart) > max(image, version)
    assert chart.get("env") == {
        "VERSION": "${{ steps.version.outputs.number }}",
        "OWNER": "${{ github.repository_owner }}",
        "ACTOR": "${{ github.actor }}",
        "TOKEN": "${{ secrets.GITHUB_TOKEN }}",
    }

    fake = tmp_path / "helm"
    fake.write_text("#!/bin/sh\n"
                    f"printf '%s\\n' \"$*\" >> {tmp_path}/calls\n"
                    f'[ "$1" = registry ] && cat > {tmp_path}/stdin\n'
                    "exit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    result = subprocess.run(
        ["bash", "-e", "-c", chart["run"]], capture_output=True, text=True,
        stdin=subprocess.DEVNULL,
        env={"PATH": f"{tmp_path}:{os.environ['PATH']}", "RUNNER_TEMP": str(tmp_path),
             "VERSION": "1.2.3", "OWNER": "o", "ACTOR": "a", "TOKEN": "t"})
    assert result.returncode == 0, result.stderr

    calls = [line.split() for line in (tmp_path / "calls").read_text(encoding="utf-8").splitlines()]
    package = next(c for c in calls if c[0] == "package")
    assert package[1] == "deploy/helm/stocktake"
    assert _flag(package, "--version") == "1.2.3"
    assert _flag(package, "--app-version") == "1.2.3"
    assert ["push", f"{tmp_path}/stocktake-1.2.3.tgz", "oci://ghcr.io/o/charts"] in calls
    login = next(c for c in calls if c[:2] == ["registry", "login"])
    # The token on stdin, never in the arguments, where a process list shows it.
    assert "t" not in login and (tmp_path / "stdin").read_text(encoding="utf-8") == "t"
    assert calls.index(login) < calls.index(next(c for c in calls if c[0] == "push"))


def _document(workflow: Path) -> dict:
    return yaml.safe_load(workflow.read_text(encoding="utf-8"))


def _writes(permissions) -> list[str]:
    if isinstance(permissions, str):        # `write-all` or `read-all`
        return ["everything"] if permissions == "write-all" else []
    return sorted(scope for scope, level in (permissions or {}).items() if level == "write")


def test_only_the_jobs_that_publish_hold_a_write_token():
    """release.yml gave the whole workflow `contents: write` and `packages:
    write`, so the job that tests a tag held both while it installed and ran
    code no lock pins. docs.yml gave `pages: write` and `id-token: write` to the
    job that installed mkdocs-material at whatever version was newest. A
    workflow that says nothing gets the repository's default, which can be
    write, so each one says."""
    documents = {w.name: _document(w) for w in WORKFLOWS}
    jobs = {(name, job_name): _writes(job.get("permissions"))
            for name, document in documents.items()
            for job_name, job in (document.get("jobs") or {}).items()}

    assert [name for name, document in documents.items() if "permissions" not in document] == []
    assert {name: _writes(d["permissions"]) for name, d in documents.items()
            if _writes(d.get("permissions"))} == {}
    assert {job: scopes for job, scopes in jobs.items() if scopes} == {
        ("ci.yml", "dev"): ["packages"],
        ("codeql.yml", "analyze"): ["security-events"],
        ("docs.yml", "deploy"): ["id-token", "pages"],
        ("release.yml", "publish"): ["contents", "packages"],
    }


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda w: w.name)
def test_no_checkout_leaves_the_token_behind(workflow: Path):
    """actions/checkout writes the job's token into .git/config unless told not
    to, where every later step, and everything those steps install, can read
    it. Nothing here pushes."""
    kept = [job for job, step in _steps(workflow)
            if str(step.get("uses", "")).startswith("actions/checkout@")
            and (step.get("with") or {}).get("persist-credentials") is not False]

    assert kept == []


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda w: w.name)
def test_what_a_workflow_installs_comes_from_the_lock(workflow: Path):
    """`pip install mkdocs-material` took whatever was newest, in the job whose
    build Pages published. And `uv sync` alone re-resolves a lock that has
    fallen behind pyproject.toml, so a job could run versions nobody reviewed.
    appcore's own suite, run in its own checkout, keeps to appcore's rules."""
    loose = [(job, line.strip()) for job, step in _steps(workflow)
             if not step.get("working-directory")
             for line in str(step.get("run") or "").splitlines()
             if re.search(r"\bpip install\b", line)
             or (re.search(r"\buv sync\b", line) and not re.search(r"--(locked|frozen)\b", line))]

    assert loose == []


SHIPPED_PYTHON = re.search(r"/python:(3\.\d+)-slim@",
                           (ROOT / "Dockerfile").read_text(encoding="utf-8")).group(1)


def test_a_release_is_tested_on_the_python_it_ships():
    """No version was given, so a release's tests ran on whatever the runner
    had, 3.12, while the image runs the version its base pins."""
    verify = _document(RELEASE)["jobs"]["verify"]
    setups = [step.get("with") or {} for step in verify["steps"]
              if str(step.get("uses", "")).startswith("astral-sh/setup-uv@")]

    assert setups
    assert [str(setup.get("python-version")) for setup in setups] == [SHIPPED_PYTHON] * len(setups)


def test_a_release_runs_the_suite_inside_its_image_before_publishing():
    """CI runs the suite inside the image and a release did not, so a tag
    could ship an image whose own test stage failed."""
    jobs = _document(RELEASE)["jobs"]
    in_image = [name for name, job in jobs.items()
                if any("--target test" in str(step.get("run") or "") for step in job["steps"])]
    needs = jobs["publish"]["needs"]

    assert in_image
    assert set(in_image) <= set([needs] if isinstance(needs, str) else needs)


def test_ci_tests_postgres_on_the_python_it_ships():
    """Kept in step by hand, ci.yml says, and it was not: the pin moved to 3.14
    and Postgres stayed on 3.12, the one combination the image runs left
    untested."""
    matrix = _document(ROOT / ".github" / "workflows" / "ci.yml")["jobs"]["test"]["strategy"]["matrix"]

    assert [str(leg["python"]) for leg in matrix["include"] if leg.get("backend") == "postgres"] == [
        SHIPPED_PYTHON]

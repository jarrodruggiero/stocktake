"""The release's tag is the only place a version is written.

Chart.yaml's `appVersion` picks the image every Helm install runs, and it was
kept by hand. It said 0.64.0 for every release from 0.64.0 to 0.67.1, so an
install of any of them ran 0.64.0, without the fixes four advisories told
people to upgrade for. `pyproject.toml` said 0.64.0 alongside it, and the test
that compared the two passed, because neither had moved.

So nothing in the tree carries a release number any more. The release packages
the chart with the tag's version, and the project reads its own from the tag.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
CHART = ROOT / "deploy" / "helm" / "stocktake"
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
HELM = shutil.which("helm")

needs_helm = pytest.mark.skipif(
    HELM is None, reason="helm is not installed here; CI's runners have it")


def test_the_project_takes_its_version_from_the_tag():
    """A number here is one more to forget at a release; this one already was."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    project = pyproject["project"]

    assert "version" not in project, (
        f"pyproject.toml says version {project['version']!r}; the tag is the version")
    assert "version" in project.get("dynamic", [])
    assert pyproject["tool"]["hatch"]["version"]["source"] == "vcs"


def test_the_chart_here_names_no_release():
    """The chart in the tree is the source the release packages, not a release."""
    chart = yaml.safe_load((CHART / "Chart.yaml").read_text())

    assert "appVersion" not in chart, (
        f"Chart.yaml names appVersion {chart['appVersion']!r}; the release sets it")


def _helm_template(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([HELM, "template", "stocktake", *args],
                          capture_output=True, text=True)


def _deployment(rendered: str) -> dict:
    return next(doc for doc in yaml.safe_load_all(rendered)
                if doc and doc.get("kind") == "Deployment")


@needs_helm
def test_the_chart_here_refuses_to_guess_an_image():
    """Installed from a clone, it has no release to run, so it says where the
    published chart is rather than inventing one. A tag set by hand still wins,
    for anyone who means to run the chart from a clone."""
    refused = _helm_template(str(CHART))

    assert refused.returncode != 0, "the chart rendered an image it was not given"
    assert "oci://ghcr.io/jarrodruggiero/charts/stocktake" in refused.stderr

    chosen = _helm_template(str(CHART), "--set", "image.tag=0.67.1")
    assert chosen.returncode == 0, chosen.stderr
    container = _deployment(chosen.stdout)["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "ghcr.io/jarrodruggiero/stocktake:0.67.1"


def _chart_step() -> dict | None:
    document = yaml.safe_load(RELEASE.read_text())
    steps = [step for job in document["jobs"].values() for step in job.get("steps", [])]
    return next((s for s in steps if s.get("id") == "chart"), None)


@needs_helm
def test_the_released_chart_runs_its_own_release(tmp_path):
    """The release's own step, with real Helm doing the packaging and only the
    registry calls stood in for: what it would push deploys the tag's image."""
    step = _chart_step()
    assert step is not None, "release.yml does not publish the Helm chart"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    helm = bin_dir / "helm"
    helm.write_text("#!/bin/sh\n"
                    'case "$1" in push|registry) cat > /dev/null; exit 0;; esac\n'
                    f'exec "{HELM}" "$@"\n')
    helm.chmod(0o755)

    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]], cwd=ROOT, capture_output=True, text=True,
        stdin=subprocess.DEVNULL,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
             "RUNNER_TEMP": str(tmp_path), "VERSION": "1.2.3",
             "OWNER": "o", "ACTOR": "a", "TOKEN": "t"})
    assert result.returncode == 0, result.stderr

    rendered = _helm_template(str(tmp_path / "stocktake-1.2.3.tgz"))
    assert rendered.returncode == 0, rendered.stderr
    deployment = _deployment(rendered.stdout)
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "ghcr.io/jarrodruggiero/stocktake:1.2.3"
    assert deployment["metadata"]["labels"]["app.kubernetes.io/version"] == "1.2.3"

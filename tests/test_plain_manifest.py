"""The plain Kubernetes manifest runs the release you chose, and says how many
things you have to change.

It ran `latest`, which Kubernetes pulls on every pod start: an OOM kill or a
node drain upgraded the app, and migrated its database, with no backup taken
first. A version written here would be a number nobody updates (#137), so the
image is one more thing marked CHANGE ME.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "deploy" / "kubernetes" / "stocktake.yaml"
GUIDE = ROOT / "docs" / "deploy" / "index.md"
NUMBERS = {"five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9}


def _container() -> dict:
    deployment = next(doc for doc in yaml.safe_load_all(MANIFEST.read_text(encoding="utf-8"))
                      if doc and doc.get("kind") == "Deployment")
    return deployment["spec"]["template"]["spec"]["containers"][0]


def test_the_manifest_runs_a_release_you_name():
    image = _container()["image"]
    lines = MANIFEST.read_text(encoding="utf-8").splitlines()
    at = next(i for i, line in enumerate(lines) if line.strip() == f"image: {image}")

    assert image == "ghcr.io/jarrodruggiero/stocktake:CHANGE-ME"
    assert any("CHANGE ME" in line for line in lines[at - 6:at])


def test_the_count_of_changes_is_the_count_of_markers():
    text = MANIFEST.read_text(encoding="utf-8")
    markers = len(re.findall(r"(?<!marked )CHANGE ME", text))
    said_here = re.search(r"change the (\w+) things marked CHANGE ME", text)
    said_there = re.search(r"edit the (\w+) CHANGE ME markers", GUIDE.read_text(encoding="utf-8"))

    assert said_here and said_there
    assert NUMBERS[said_here.group(1)] == NUMBERS[said_there.group(1)] == markers

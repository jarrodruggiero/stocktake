"""One answer to "what do I back up?", and every recipe points at it.

Four places said, in three versions: the database alone, the database and
config.yaml, or /data. None named the installed formats, the configuration
and the stale `-wal` file a restore has to clear, together, and none said
how to back up Postgres. And the setup wizard, given an empty path, put the
database in /data/portfolio.db, a name from before the rename, which the
documented backup did not copy (test_setup_wizard.py holds the default).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GUIDE = ROOT / "docs" / "deploy" / "index.md"
ANCHOR = "#backing-up-and-restoring"
RECIPES = ["deploy/README.md", "deploy/compose/README.md", "deploy/nas/unraid/README.md",
           "deploy/helm/stocktake/templates/NOTES.txt"]


def _section() -> str:
    text = GUIDE.read_text(encoding="utf-8")
    found = re.search(r"^## Backing up and restoring\n(.*?)(?=^## )", text, re.M | re.S)
    assert found, "the deployment guide has no backup section"
    return found.group(1)


@pytest.mark.parametrize("state", ["/data/stocktake.db", "/data/formats", "config.yaml",
                                   "stocktake.db-wal", "pg_dump"])
def test_the_backup_section_names_all_the_state(state):
    assert state in _section()


@pytest.mark.parametrize("recipe", RECIPES)
def test_every_recipe_points_at_the_one_section(recipe):
    assert ANCHOR in (ROOT / recipe).read_text(encoding="utf-8")


def test_nothing_names_the_database_from_before_the_rename():
    found = [f"{path.relative_to(ROOT)}" for path in sorted((ROOT / "app").rglob("*"))
             if path.suffix in (".py", ".html") and "portfolio.db" in path.read_text(encoding="utf-8")]

    assert found == []

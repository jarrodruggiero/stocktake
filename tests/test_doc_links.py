"""Every "More information" link in the app points at a page that exists.

These links are the other half of the house rule about copy: one line at the
control, and the detail behind a link. That trade only holds while the link
lands — a 404 at the end of it is worse than the paragraph it replaced, because
nobody finds out until somebody needs the answer.

Nothing checked this before. The links are written in two places and neither
could notice the docs being renamed around it:

  * `{{ docs_url }}/some/path/` written literally in a template
  * `Feature.doc`, rendered by the settings page and the wizard

So the guard reads both out of the source rather than listing them, and
`test_the_guard_catches_a_broken_link` plants a bad one to prove it can fail —
otherwise it would pass just as happily on a repository with no links at all.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from app.features import FEATURES

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "app" / "templates"
DOCS = ROOT / "docs"

# `href="{{ docs_url }}/guides/market-data/"` — the literal path after the
# placeholder. A `{{` in the path means it is built at runtime (Feature.doc),
# and those arrive through FEATURES instead.
LINK = re.compile(r"\{\{\s*docs_url\s*\}\}/([^\"'{}\s]*)")


def _as_source(url_path: str) -> Path:
    """The markdown file mkdocs serves at this URL path.

    Directory URLs: `guides/market-data/` is built from
    `docs/guides/market-data.md`, and a bare `guides/` from its `index.md`.
    """
    trimmed = url_path.strip("/")
    if not trimmed:
        return DOCS / "index.md"
    return DOCS / f"{trimmed}/index.md" if url_path.endswith("/") and (
        DOCS / trimmed / "index.md"
    ).exists() else DOCS / f"{trimmed}.md"


def _name(template: Path) -> str:
    """A readable label for the failure message. Falls back to the bare
    filename for a template outside the tree, which is how the
    planted-violation test below points this at a temporary directory."""
    try:
        return template.relative_to(ROOT).as_posix()
    except ValueError:
        return template.name


def _links_in_templates() -> list[tuple[str, str]]:
    found = []
    for template in sorted(TEMPLATES.rglob("*.html")):
        for match in LINK.finditer(template.read_text()):
            found.append((_name(template), match.group(1)))
    return found


def _declared_links() -> list[tuple[str, str]]:
    return [("app/features.py", f.doc) for f in FEATURES if f.doc]


def all_doc_links() -> list[tuple[str, str]]:
    return _links_in_templates() + _declared_links()


def test_the_app_has_doc_links_to_check():
    """The guard's own smoke test: an empty sweep proves nothing."""
    assert len(all_doc_links()) >= 5


@pytest.mark.parametrize(
    "where, url_path",
    all_doc_links(),
    ids=[f"{w}:{p or '/'}" for w, p in all_doc_links()],
)
def test_every_doc_link_resolves_to_a_page(where, url_path):
    target = _as_source(url_path)

    assert target.exists(), (
        f"{where} links to {url_path!r}, which would need {target.relative_to(ROOT)}"
    )


@pytest.mark.parametrize("where, url_path", all_doc_links(),
                         ids=[f"{w}:{p or '/'}" for w, p in all_doc_links()])
def test_every_linked_page_is_in_the_nav(where, url_path):
    """A page mkdocs does not list is a page nobody can navigate to, even when
    the direct link works."""
    target = _as_source(url_path)
    relative = target.relative_to(DOCS).as_posix()

    assert relative in _nav_paths(), (
        f"{where} links to {url_path!r} ({relative}), which mkdocs.yml does not list"
    )


def _nav_paths() -> set[str]:
    nav = yaml.safe_load((ROOT / "mkdocs.yml").read_text().replace("!!python/name:", ""))
    found: set[str] = set()

    def walk(node):
        if isinstance(node, str):
            found.add(node)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(nav.get("nav", []))
    return found


# --------------------------------------------------------------------------- #
# The guard can fail
# --------------------------------------------------------------------------- #

def test_the_guard_catches_a_broken_link(tmp_path, monkeypatch):
    """Planted violation: a template linking somewhere that does not exist."""
    fake_templates = tmp_path / "templates"
    fake_templates.mkdir()
    (fake_templates / "bad.html").write_text(
        '<a href="{{ docs_url }}/guides/no-such-page/">More information</a>'
    )
    monkeypatch.setattr("test_doc_links.TEMPLATES", fake_templates)

    found = _links_in_templates()

    assert found == [("bad.html", "guides/no-such-page/")] or any(
        path == "guides/no-such-page/" for _, path in found
    )
    assert not _as_source("guides/no-such-page/").exists()


def test_the_guard_reads_feature_docs_too():
    """`Feature.doc` is rendered by two pages and written in neither, so it has
    to come from the dataclass or it would be missed entirely."""
    assert ("app/features.py", "guides/dca-schedule/") in _declared_links()

"""The brand is drawn once and generated everywhere else.

The mark and the wordmark appear in the top bar, in the favicon, in a 512px
PNG for the Unraid and CasaOS listings, and in two README banners. That was
five copies of the same geometry, which is four chances to change some of them
and ship a logo that disagrees with itself.

`app/branding.py` holds the drawing; `tools/render_brand.py` emits the rest.
These tests fail when a generated file no longer matches what the module would
produce — so a redraw that lands in the top bar and misses the favicon is a red
test rather than something noticed months later in a screenshot.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app import branding

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import render_brand  # noqa: E402


def test_the_included_partial_matches_the_source():
    on_disk = (ROOT / "app/templates/_brand.html").read_text()
    assert on_disk == render_brand.partial(), (
        "app/templates/_brand.html has drifted — run tools/render_brand.py")


def test_the_favicon_matches_the_source():
    on_disk = (ROOT / "app/static/icon.svg").read_text()
    assert on_disk == render_brand.icon_svg(), (
        "app/static/icon.svg has drifted — run tools/render_brand.py")


def test_the_base_template_does_not_carry_its_own_copy():
    """The failure this exists to prevent: someone edits the top bar directly
    and the favicon quietly keeps the old drawing."""
    base = (ROOT / "app/templates/base.html").read_text()
    assert '{% include "_brand.html" %}' in base
    assert "brandmark" not in base.replace('{% include "_brand.html" %}', ""), (
        "base.html should include the partial, not inline the mark")


@pytest.mark.parametrize("name", [
    "docs/assets/icon.png",          # Unraid CA + CasaOS listings
    "docs/assets/wordmark-light.png",
    "docs/assets/wordmark-dark.png",
])
def test_the_rasterised_artefacts_exist(name):
    """These need headless Chrome so they are not regenerated in the test run,
    but their absence is what breaks an app-store submission."""
    path = ROOT / name
    assert path.exists(), f"{name} is missing — run tools/render_brand.py"
    assert path.stat().st_size > 1000, f"{name} looks empty"


def test_the_mark_carries_exactly_one_accent_crate():
    """Six crates, one of which is the accent. If that ever becomes zero the
    mark still renders and nobody notices it went monochrome."""
    assert sum(1 for *_, accent in branding.MARK_RECTS if accent) == 1
    assert len(branding.MARK_RECTS) == 6
    assert branding.mark_svg().count("<rect") == 6


def test_the_wordmark_spells_the_name():
    """Nine letters at ascending offsets — a dropped or reordered glyph is
    invisible in a diff of path data."""
    offsets = [x for x, _ in branding.WORDMARK_LETTERS]
    assert len(offsets) == len("stocktake")
    assert offsets == sorted(offsets), "letters must run left to right"
    assert offsets[0] == 0


def test_the_wordmark_keeps_its_selected_typeface():
    """A geometric redraw at UI size is the regression this brand pass fixes."""
    assert getattr(branding, "WORDMARK_TYPEFACE", None) == (
        "IBM Plex Sans Condensed SemiBold"
    ), "the wordmark should retain IBM Plex Sans Condensed SemiBold outlines"

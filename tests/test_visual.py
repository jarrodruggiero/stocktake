"""Every page, at a desktop AND a phone width, with the fault stated as a number.

Every layout bug this project has had was found by a person looking at a page,
and the ones that took longest only existed at one width — a calendar cropped to
two days, a tab strip that pushed the document sideways, a popup you could read
the page through. All of them rendered perfectly at 1400px, which was the only
width anything was ever checked at.

Marked `visual` and deselected by default (see `addopts` in pyproject.toml)
because a full pass drives a real browser over every page twice and takes
minutes, which is not what `pytest tests` should cost. CI runs them as their own
job:

    uv run pytest tests -m visual

The pages come from `tools/screenshot.py`, run as a subprocess rather than
imported: it rewrites `os.environ` and reads `sys.argv` at import time, so
importing it mid-session would hand it pytest's arguments and leak its
configuration into every test that ran afterwards.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.browser import find_chrome  # noqa: E402

pytestmark = pytest.mark.visual

CHROME = find_chrome()
if CHROME is None:
    pytest.skip("no headless Chrome on this machine", allow_module_level=True)

# 1400 is the layout's design width; 412 is a Pixel-class phone in CSS pixels.
WIDTHS = {"desktop": 1400, "mobile": 412}

# The page inventory, named rather than globbed. Parametrising over a static
# list means the render can stay in a session fixture — and a page added to
# screenshot.py without being added here fails `test_every_rendered_page_is_listed`
# instead of silently never being checked.
PAGES = [
    "1-welcome", "2-database", "2b-database-error", "3-account",
    "4-recovery-codes", "6-portfolio", "7-features", "7b-environment",
    "8-finish", "9-restarting", "10-dashboard", "11-profile", "12-charts",
    "12b-chart-builder", "13-login", "14-recover", "15-instruments",
    "16-dashboard-holdings", "17-dashboard-sorted", "17b-holdings-blocked",
    "17c-dashboard-gain-pct", "18-imports", "18b-imports-templates",
    "19-plan-empty", "20-plan", "21-plan-editing", "22-admin",
    "23-admin-settings",
]

PROBE = (ROOT / "tests" / "visual_probe.js").read_text()

# The page under test goes in an IFRAME of the wanted width, not a window of it.
# `--window-size=412,900` is silently clamped: headless Chrome will not go below
# 500px in any headless mode, so a check that trusted the flag was measuring a
# 500px viewport while claiming to measure a phone. An iframe gets its own
# viewport, so media queries and `100vw` resolve to its width.
#
# Its document cannot write the outer title without
# `--allow-file-access-from-files`: two `file://` documents are otherwise
# different origins.
WRAPPER = """<!doctype html><html><body style="margin:0">
<iframe id="f" src="%s" style="width:%dpx;height:900px;border:0"></iframe>
<script>
var frame = document.getElementById("f");
var tries = 0;
function collect() {
  var title = "";
  try { title = frame.contentDocument.title; } catch (e) { title = "BLOCKED " + e.message; }
  if (title.indexOf("VC") === 0 || title.indexOf("BLOCKED") === 0 || ++tries > 60) {
    document.title = title || "probe silent";
    return;
  }
  setTimeout(collect, 50);
}
frame.addEventListener("load", collect);
</script></body></html>
"""


@pytest.fixture(scope="session")
def shots() -> Path:
    """Every page rendered to standalone HTML, once for the whole session."""
    out = Path(tempfile.mkdtemp(prefix="visual-"))
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "screenshot.py"), str(out)],
        capture_output=True, text=True, timeout=300, cwd=ROOT,
    )
    if result.returncode != 0:
        pytest.fail(f"screenshot.py failed:\n{result.stdout}\n{result.stderr}")
    return out


def probe(page: Path, width: int) -> dict:
    """Render one page at one width and return what the probe measured."""
    with tempfile.TemporaryDirectory() as tmp:
        instrumented = Path(tmp) / "page.html"
        instrumented.write_text(
            page.read_text().replace("</body>", f"<script>{PROBE}</script></body>"))
        wrapper = Path(tmp) / "wrapper.html"
        wrapper.write_text(WRAPPER % (instrumented.name, width))

        out = subprocess.run(
            [CHROME, "--headless", "--disable-gpu", "--dump-dom",
             "--allow-file-access-from-files",
             # Chrome needs no sandbox where it cannot get one (containers,
             # some runners) and refuses to start otherwise.
             "--no-sandbox",
             # Room for the frame plus the browser's own minimum.
             f"--window-size={max(width + 80, 620)},960",
             "--virtual-time-budget=6000",
             wrapper.as_uri()],
            capture_output=True, text=True, timeout=90,
        ).stdout

        marker = out.find("VC{")
        if marker < 0:
            return {"error": "probe did not run"}
        return json.loads(out[marker + 2:out.find("</title>", marker)])


@pytest.mark.parametrize("width_name", sorted(WIDTHS))
@pytest.mark.parametrize("page", PAGES)
def test_the_page_has_no_layout_faults(shots: Path, page: str, width_name: str) -> None:
    html = shots / f"{page}.html"
    assert html.exists(), f"{page} was not rendered — is it still in screenshot.py?"

    report = probe(html, WIDTHS[width_name])
    assert not report.get("error"), f"{page}: {report['error']}"

    faults = []
    if report["overflow"] > 0:
        faults.append(f"the document scrolls sideways by {report['overflow']}px")
    if report["wide"]:
        faults.append("past the edge: " + ", ".join(report["wide"][:4]))
    if report["spill"]:
        faults.append("text outside its box: " + ", ".join(report["spill"][:4]))
    if report.get("panels"):
        faults.append("popups off screen: " + ", ".join(report["panels"][:4]))

    assert not faults, f"{page} at {width_name} ({WIDTHS[width_name]}px):\n  " + \
        "\n  ".join(faults)


def test_every_rendered_page_is_listed(shots: Path) -> None:
    """A page added to screenshot.py must be added to PAGES, or it goes unchecked.

    The failure this prevents is silent: a new page renders, nothing references
    it, and it is never measured at either width.
    """
    rendered = {p.stem for p in shots.glob("*.html") if p.name[0].isdigit()}
    assert rendered == set(PAGES), (
        f"only in screenshot.py: {sorted(rendered - set(PAGES))}\n"
        f"only in PAGES: {sorted(set(PAGES) - rendered)}")

"""Rules about the source itself, checked by reading it.

- **Dates come from `app/clock.py`.** A bare `date.today()` reads the
  machine's zone, which in a container is UTC: from midnight to 10am in
  Melbourne it is still yesterday there. Every test froze at a time when the
  two agree, so swapping `clock.today()` for `date.today()` passed.
- **Text is read and written as UTF-8.** Without an encoding Python uses the
  platform's, which on Windows is cp1252, and the suite could not even be
  collected there.

- **A ticker is looked up in one place.** The two forms that add an
  instrument each had a copy of the lookup, and the copies drifted.
- **An error panel is announced.** A refusal shown in a panel at the top of
  a page said nothing to a screen reader; `role="alert"` makes it speak.

Each scan is shown a planted violation first, so a scan that finds nothing
cannot be a scan that looks at nothing.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The machine's own idea of now: no zone, or the local one. An aware
# `datetime.now(dt.timezone.utc)` is an instant, not a date, and is fine.
LOCAL_CLOCK = re.compile(
    r"\b(?:date|datetime)\.today\(|\bdatetime\.utcnow\(|\bdatetime\.now\(\s*\)"
    r"|\btime\.localtime\(")


def _local_clock_calls(source: str) -> list[int]:
    return [n for n, line in enumerate(source.splitlines(), 1)
            if LOCAL_CLOCK.search(line.split("#", 1)[0])]


def test_the_clock_scan_finds_a_planted_call():
    planted = "x = 1\ny = dt.date.today()\nz = dt.datetime.now()  # and this\n"

    assert _local_clock_calls(planted) == [2, 3]
    assert _local_clock_calls("w = dt.datetime.now(dt.timezone.utc)\n") == []


def test_dates_come_from_the_clock_module():
    found = [f"{path.relative_to(ROOT)}:{line}"
             for path in sorted((ROOT / "app").rglob("*.py")) if path.name != "clock.py"
             for line in _local_clock_calls(path.read_text(encoding="utf-8"))]

    assert found == [], "use app.clock.today() or clock.now()"


def _text_io_without_an_encoding(source: str) -> list[int]:
    lines = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if "encoding" in {keyword.arg for keyword in node.keywords}:
            continue
        if (node.func.attr == "read_text" and not node.args) or (
                node.func.attr == "write_text" and len(node.args) < 2):
            lines.append(node.lineno)
    return lines


def test_the_encoding_scan_finds_a_planted_read_and_write():
    planted = 'a = p.read_text()\nb = p.write_text("x")\nc = p.read_text(encoding="utf-8")\n'

    assert _text_io_without_an_encoding(planted) == [1, 2]


def test_text_is_read_and_written_as_utf8():
    found = [f"{path.relative_to(ROOT)}:{line}"
             for folder in ("app", "tests", "tools")
             for path in sorted((ROOT / folder).rglob("*.py"))
             for line in _text_io_without_an_encoding(path.read_text(encoding="utf-8"))]

    assert found == [], 'pass encoding="utf-8"'


ERROR_PANEL = re.compile(r'<[a-z]+\b[^>]*class="[^"]*\bpanel error\b[^"]*"[^>]*>')


def _unannounced_error_panels(source: str) -> list[str]:
    return [tag for tag in ERROR_PANEL.findall(source) if 'role="alert"' not in tag]


def test_the_error_panel_scan_finds_a_planted_one():
    planted = '<p class="panel error">no</p><p class="panel error" role="alert">yes</p>'

    assert _unannounced_error_panels(planted) == ['<p class="panel error">']


def test_every_error_panel_is_announced():
    found = [f"{path.relative_to(ROOT)}: {tag}"
             for path in sorted((ROOT / "app" / "templates").glob("*.html"))
             for tag in _unannounced_error_panels(path.read_text(encoding="utf-8"))]

    assert found == [], 'give it role="alert"'


def test_a_ticker_is_looked_up_in_one_place():
    asking = [str(path.relative_to(ROOT))
              for folder in ("static", "templates")
              for path in sorted((ROOT / "app" / folder).glob("*.*"))
              if path.suffix in (".js", ".html")
              and "/holdings/lookup?" in path.read_text(encoding="utf-8")]

    assert asking == ["app/static/lookup.js"]

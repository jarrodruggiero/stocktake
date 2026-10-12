"""A link that acts looks like a button, and is one element.

style.md used to say to wrap such a link around a `<button>`. That is invalid
HTML: each control took two tab stops, and the link and the button could be
named differently. The rule is `a.button` now, with the button's variants, and
it has to look like the button beside it: the browser test lays the two side by
side in every place a link drawn as a button sits, and compares what is drawn.
"""

from __future__ import annotations

import html
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "app" / "templates"
STATIC = ROOT / "app" / "static"


def test_no_template_puts_a_button_inside_a_link():
    nested = re.compile(r"<a\b[^>]*>(?:(?!</a>).)*?<button\b", re.S)
    found = [f"{path.name}:{text[:match.start()].count(chr(10)) + 1}"
             for path in sorted(TEMPLATES.glob("*.html"))
             for text in [path.read_text(encoding="utf-8")]
             for match in nested.finditer(text)]

    assert found == [], f"a <button> inside an <a>, which should be an a.button: {found}"


# (name, around, classes, label). `around` has one {} where the control goes:
# every place a link drawn as a button sits, beside a <button> in the same
# place, so a rule written for one and not the other shows up here.
CASES = [
    *[(f"plain {classes}".strip(), "{}", classes, "Go")
      for classes in ("", "secondary", "secondary small", "primary", "danger", "danger small")],
    ("menu actions",
     '<div class="menuactions">{}<form><button class="secondary" type="submit">Log out'
     "</button></form></div>", "secondary", "Settings"),
    ("range bar", '<div class="calhead"><h1>Build a chart</h1><div class="rangebar">{}</div></div>',
     "", "&larr; Charts"),
    ("page actions",
     '<div class="pagehead"><div><h1>Charts</h1></div><div class="headside"><div class="headactions">'
     '<form><button type="submit" class="secondary">Refresh prices</button></form>{}'
     '<button type="button" class="primary">Record trade</button></div></div></div>',
     "secondary", "View charts"),
    ("page primary", '<div class="pagehead"><h1>Charts</h1><div class="headactions">{}</div></div>',
     "primary", "+ Add a chart"),
    ("commit bar", '<div class="commitbar"><button type="submit">Save trade</button>{}</div>',
     "secondary", "Cancel"),
    ("table tools",
     '<section><h2 class="tablehead">Holdings <span class="tabletools">{}</span></h2></section>',
     "secondary small", "Manage holdings"),
    ("section heading", '<h2 class="sectionhead">Ledger {}</h2>', "secondary", "Record a trade"),
    ("table row", '<table><tbody><tr><td>ACME</td><td class="num">{}</td></tr></tbody></table>',
     "secondary small", "Edit"),
    ("calendar",
     '<section class="calpanel"><header class="calbar"><h2>October 2026</h2>'
     '<div class="calnav">{}</div></header></section>', "", "&larr;"),
    ("upcoming", '<div class="upactions">{}<form><button type="submit">Skip</button></form></div>',
     "secondary", "Record buy"),
    ("empty portfolio",
     '<div class="firstrun"><div class="commitbar"><button type="button" class="primary">'
     "Record a trade</button>{}</div></div>", "secondary", "Import from your broker"),
]

DRAWN = ["background-color", "background-image", "color", "border-top-width",
         "border-top-style", "border-top-color", "border-radius", "padding-top",
         "padding-right", "padding-bottom", "padding-left", "font-size", "font-weight",
         "font-family", "line-height", "letter-spacing", "text-decoration-line", "text-align",
         "box-shadow", "cursor", "vertical-align", "filter"]

PROBE = """
<script>
window.addEventListener("load", function () {
  var drawn = DRAWN, out = {};
  document.querySelectorAll(".case").forEach(function (box) {
    var probe = box.querySelector("[data-probe]"), style = getComputedStyle(probe);
    var at = probe.getBoundingClientRect(), from = box.getBoundingClientRect();
    var seen = {};
    drawn.forEach(function (name) { seen[name] = style.getPropertyValue(name); });
    // Where it sits and how much room it takes, inside a box of its own: a
    // margin or a line height that differs moves these.
    seen.box = [Math.round(at.left - from.left), Math.round(at.top - from.top),
                Math.round(at.width), Math.round(at.height), Math.round(from.height)];
    out[box.id] = seen;
  });
  var pre = document.createElement("pre");
  pre.id = "result";
  pre.textContent = JSON.stringify(out);
  document.body.appendChild(pre);
});
</script>
"""


def _page() -> str:
    cases = []
    for i, (_name, around, classes, label) in enumerate(CASES):
        drawn = {"button": f'<button type="button" class="{classes}" data-probe>{label}</button>',
                 "link": f'<a class="button {classes}" href="#" data-probe>{label}</a>'}
        cases += [f'<div class="case" id="{kind}-{i}">{around.format(markup)}</div>'
                  for kind, markup in drawn.items()]
    style = (STATIC / "style.css").read_text(encoding="utf-8")
    return ("<!doctype html><html><head><meta charset='utf-8'><style>" + style
            + "</style></head><body>" + "".join(cases)
            + PROBE.replace("DRAWN", json.dumps(DRAWN)) + "</body></html>")


def _drawn(tmp_path, page: str, width: int) -> dict:
    sys.path.insert(0, str(ROOT))
    from tools.browser import find_chrome

    chrome = find_chrome()
    if chrome is None:
        pytest.skip("no headless Chrome on this machine")
    target = tmp_path / "links.html"
    target.write_text(page, encoding="utf-8")
    dom = subprocess.run(
        [chrome, "--headless", "--disable-gpu", "--dump-dom", "--no-sandbox",
         f"--window-size={width},2000", "--virtual-time-budget=4000", target.as_uri()],
        capture_output=True, text=True, timeout=90).stdout
    found = re.search(r'<pre id="result">(.*?)</pre>', dom, re.S)
    assert found, "the probe did not finish"
    return json.loads(html.unescape(found.group(1)))


@pytest.mark.visual
@pytest.mark.parametrize("width", [1400, 412])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_a_link_drawn_as_a_button_looks_like_a_button(tmp_path, width, theme):
    drawn = _drawn(tmp_path, _page().replace("<body>", f'<body data-theme="{theme}">', 1), width)

    differences = {
        name: {key: (drawn[f"button-{i}"][key], drawn[f"link-{i}"][key])
               for key in drawn[f"button-{i}"]
               if drawn[f"button-{i}"][key] != drawn[f"link-{i}"][key]}
        for i, (name, *_rest) in enumerate(CASES)}

    assert {name: diff for name, diff in differences.items() if diff} == {}


@pytest.mark.visual
@pytest.mark.parametrize("width", [1400, 412])
def test_the_account_menus_two_actions_are_one_width(tmp_path, width):
    """Settings is a link and Log out a form's button. In a flex row the link's
    padding grew along with it, and it came out wider."""
    style = (STATIC / "style.css").read_text(encoding="utf-8")
    page = ("<!doctype html><html><head><meta charset='utf-8'><style>" + style
            + "</style></head><body><div class='menuactions'>"
            + '<a class="button secondary" href="#">Settings</a>'
            + '<form><button class="secondary" type="submit">Log out</button></form></div>'
            + "<script>window.addEventListener('load', function () {"
            + "var pre = document.createElement('pre'); pre.id = 'result';"
            + "pre.textContent = JSON.stringify(Array.from("
            + "document.querySelectorAll('.menuactions > *'), function (el) {"
            + "return Math.round(el.getBoundingClientRect().width); }));"
            + "document.body.appendChild(pre); });</script></body></html>")

    settings, log_out = _drawn(tmp_path, page, width)

    assert settings == log_out

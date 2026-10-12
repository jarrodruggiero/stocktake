"""Theme colours per mode, in a real browser.

A colour that reads on the light card rarely reads on the dark one, so each
mode has its own (decisions.md #89). Two things only a browser can show: the
rule for each mode applies in that mode and no other, `auto` following the
device; and the profile page opens the tab for the mode in use, with the other
a click away.
"""

from __future__ import annotations

import json
import re
import subprocess

import pytest
from sqlalchemy import select
from test_trade_dialog_in_the_browser import CHROME, HTML, _standalone

from app.models import User
from test_routes import make_login

pytestmark = pytest.mark.visual

DEVICE = {"light": "--blink-settings=preferredColorScheme=1",
          "dark": "--blink-settings=preferredColorScheme=0"}


def _appearance(session_factory, **values) -> None:
    with session_factory() as s:
        user = s.scalar(select(User))
        for name, value in values.items():
            setattr(user, name, value)
        s.commit()


def _run(page: str, tmp_path, driver: str, device: str) -> dict:
    target = tmp_path / "page.html"
    target.write_text(_standalone(page, driver), encoding="utf-8")
    dom = subprocess.run(
        [CHROME, "--headless", "--disable-gpu", "--dump-dom", "--no-sandbox", DEVICE[device],
         "--allow-file-access-from-files", "--virtual-time-budget=4000", target.as_uri()],
        capture_output=True, text=True, timeout=90).stdout
    found = re.search(r'<pre id="result">(.*?)</pre>', dom, re.S)
    assert found, "the driver did not finish"
    return json.loads(found.group(1).replace("&quot;", '"'))


TOKENS = """
<script>
window.addEventListener("load", function () {
  var style = getComputedStyle(document.body), out = {};
  ["--up", "--down", "--accent", "--viz-1"].forEach(function (name) {
    out[name] = style.getPropertyValue(name).trim();
  });
  var pre = document.createElement("pre");
  pre.id = "result";
  pre.textContent = JSON.stringify(out);
  document.body.appendChild(pre);
});
</script>
"""

# Loss is chosen for light alone and series 1 for dark alone: each must stay
# out of the other mode, `auto` included.
COLOURS = {"light": {"up": "#111111", "accent": "#225522", "down": "#aa0000"},
           "dark": {"up": "#eeeeee", "accent": "#ffcc00", "viz-1": "#00ccff"}}
BUILT_IN = {"light": {"down": "#c62828", "viz-1": "#8959ba"},
            "dark": {"down": "#f25050", "viz-1": "#a37ecd"}}


@pytest.mark.parametrize("theme, device, mode", [
    ("light", "dark", "light"), ("dark", "light", "dark"),
    ("auto", "light", "light"), ("auto", "dark", "dark"),
], ids=["light", "dark", "auto-on-a-light-device", "auto-on-a-dark-device"])
def test_each_modes_colours_apply_in_that_mode_only(client, session_factory, tmp_path,
                                                    theme, device, mode):
    """The teal preset sets the accent two attributes deep, in each mode, and
    a chosen accent still wins over it."""
    make_login(client, session_factory)
    _appearance(session_factory, theme=theme, accent="teal", theme_colors=COLOURS)

    drawn = _run(client.get("/", headers=HTML).text, tmp_path, TOKENS, device)

    expected = {f"--{token}": value for token, value in {**BUILT_IN[mode], **COLOURS[mode]}.items()}
    assert drawn == expected


def test_a_dark_colour_alone_leaves_light_as_it_was(client, session_factory, tmp_path):
    make_login(client, session_factory)
    _appearance(session_factory, theme="light", theme_colors={"dark": {"up": "#eeeeee"}})

    drawn = _run(client.get("/", headers=HTML).text, tmp_path, TOKENS, "light")

    assert drawn["--up"] == "#1a7f37"


TABS = """
<script>
window.addEventListener("load", function () {
  var open = function () {
    var tab = document.querySelector('.modetabs [aria-selected="true"]');
    return {tab: tab.textContent.trim(), focusable: tab.tabIndex,
            light: !document.getElementById("colours-light").hidden,
            dark: !document.getElementById("colours-dark").hidden,
            panel: document.getElementById("colours-" + tab.textContent.trim().toLowerCase())
                     .getAttribute("aria-labelledby")};
  };
  var out = {strip: !document.querySelector(".modetabs").hidden, first: open()};
  var other = document.querySelector('.modetabs [aria-selected="false"]');
  other.click();
  out.clicked = open();
  document.querySelector('.modetabs [aria-selected="true"]')
    .dispatchEvent(new KeyboardEvent("keydown", {key: "ArrowRight", bubbles: true}));
  out.arrowed = open();
  out.focused = document.activeElement.textContent.trim();
  // The other mode's box is in the form whichever tab is showing.
  out.fields = Array.from(new FormData(document.querySelector('form[action="/profile/colors"]'))
                            .keys()).filter(function (k) { return k !== "_csrf"; }).length;
  var pre = document.createElement("pre");
  pre.id = "result";
  pre.textContent = JSON.stringify(out);
  document.body.appendChild(pre);
});
</script>
"""


@pytest.mark.parametrize("theme, device, mode", [
    ("light", "dark", "Light"), ("dark", "light", "Dark"),
    ("auto", "light", "Light"), ("auto", "dark", "Dark"),
], ids=["light", "dark", "auto-on-a-light-device", "auto-on-a-dark-device"])
def test_the_tab_that_opens_is_the_mode_in_use(client, session_factory, tmp_path,
                                               theme, device, mode):
    make_login(client, session_factory)
    _appearance(session_factory, theme=theme)

    out = _run(client.get("/profile", headers=HTML).text, tmp_path, TABS, device)

    other = "Dark" if mode == "Light" else "Light"
    assert out["strip"] is True
    assert out["first"] == {"tab": mode, "focusable": 0, "light": mode == "Light",
                            "dark": mode == "Dark", "panel": f"tab-{mode.lower()}"}
    assert out["clicked"]["tab"] == other
    assert (out["clicked"]["light"], out["clicked"]["dark"]) == (other == "Light", other == "Dark")
    assert out["arrowed"]["tab"] == mode and out["focused"] == mode
    assert out["fields"] == 12

"""Money on the charts page, written the way the rest of the app writes it.

The page's script wrote a loss as `$-1,203`, sign after the symbol, and
grouped the digits in whatever locale the browser was set to: `$1.203` in a
German one. The server writes `-$1,203.00` everywhere else (money.text).
"""

from __future__ import annotations

import re
import subprocess

import pytest
from test_trade_dialog_in_the_browser import CHROME, HTML, _standalone

import factories as fac
from test_routes import bind_to_only_portfolio, make_login, session_csrf

pytestmark = pytest.mark.visual

# A browser set to German. Headless Chrome takes its number locale from the
# machine whatever its flags say, so the default is swapped instead.
GERMAN = """
<script>
var asWritten = Number.prototype.toLocaleString;
Number.prototype.toLocaleString = function (locales, options) {
  return asWritten.call(this, locales === undefined ? "de-DE" : locales, options);
};
</script>
"""


def test_a_loss_is_signed_first_and_grouped_the_same_in_any_locale(
        client, session_factory, tmp_path):
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        acme = fac.make_instrument(s, "ACME", asset_class="share")
        fac.add_trade(s, acme, "2026-01-05", "buy", 100, "20.00")
        fac.add_prices(s, acme, [("2026-01-05", "7.97")])
        s.commit()
    saved = client.post("/charts/save", headers={"X-CSRF-Token": session_csrf(session_factory)},
                        json={"name": "Gains", "width": "half", "spec": {
                            "grain": "positions", "x": "ticker", "measures": ["pos_gain"],
                            "type": "table"}})
    assert saved.status_code == 200
    page = client.get("/charts", headers=HTML).text
    target = tmp_path / "charts.html"
    target.write_text(_standalone(page, "").replace("<head>", "<head>" + GERMAN, 1),
                      encoding="utf-8")

    dom = subprocess.run(
        [CHROME, "--headless", "--disable-gpu", "--dump-dom", "--no-sandbox",
         "--allow-file-access-from-files", "--virtual-time-budget=4000", target.as_uri()],
        capture_output=True, text=True, timeout=90).stdout
    table = re.search(rf'id="t-{saved.json()["id"]}"[^>]*>(.*?)</table>', dom, re.S)

    assert table, "the chart's table did not render"
    assert '<span class="m">-$1,203</span>' in table.group(1)

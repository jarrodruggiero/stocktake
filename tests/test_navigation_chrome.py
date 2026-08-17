"""The top bar: what is a destination, and what is an app-level control.

Four things are deliberately NOT in the nav — Charts, Record trade, Holdings and
Admin — on one principle: **a control belongs beside the thing it acts on**,
not in a list of destinations repeated at the top of every page. What is left
is the three things that are genuinely places.

These tests pin the *shape*, not the styling, because the shape is the part
that carries the reasoning and the part a later edit undoes without noticing.
A test that asserted colours would fail on every visual change and teach us to
stop reading it.
"""

from __future__ import annotations

import re

from app import navigation


def test_the_nav_holds_only_the_three_destinations():
    """Anything added back here should be a *place*, not an action. If a fourth
    genuinely is one, change this test deliberately — that is the point of it."""
    assert [item.key for item in navigation.NAV] == ["portfolio", "plan", "imports"]


def test_the_four_moved_items_are_not_in_the_nav():
    """Named individually so a failure says which one crept back."""
    keys = {item.key for item in navigation.NAV}
    for gone in ("charts", "trade", "holdings", "admin"):
        assert gone not in keys, (
            f"{gone!r} is back in the top nav. It belongs next to the thing it "
            "acts on; putting it back makes the bar a toolbar again.")


def test_the_plan_nav_entry_says_dca_schedule():
    """'Plan' on its own did not say what the page was for — you edit a plan,
    you follow a schedule."""
    plan = next(item for item in navigation.NAV if item.key == "plan")
    assert plan.label == "DCA Schedule"
    assert plan.href == "/schedule", "the route stays; it is in bookmarks"


def test_admin_is_a_top_bar_control_with_an_icon():
    assert navigation.ADMIN_ITEM.key == "admin"
    assert navigation.ADMIN_ITEM.href == "/admin"
    assert navigation.ADMIN_ITEM.path, "the cog needs an icon — it renders icon-only"


def _page(client, session_factory) -> str:
    """Signed in, on the dashboard. `make_login` lives in test_routes because
    that is where the login flow is exercised; importing it beats a second
    implementation that could drift from the real form."""
    from test_routes import make_login

    make_login(client, session_factory)
    response = client.get("/", headers={"accept": "text/html"})
    assert response.status_code == 200
    return response.text


def test_the_top_bar_carries_the_admin_cog_and_the_account_menu(client, session_factory):
    page = _page(client, session_factory)

    assert 'class="iconbtn"' in page, "no admin cog in the top bar"
    assert "usermenu" in page, "no account menu in the top bar"


def test_the_account_menu_holds_the_switcher_account_link_and_log_out(client, session_factory):
    """Exactly these three belong behind the profile icon. Asserted
    against the menu's own markup rather than the whole page, or a stray
    'Log out' anywhere would satisfy it."""
    page = _page(client, session_factory)

    found = re.search(r'<div class="panel usermenupanel">(.*?)</details>', page, re.S)
    assert found, "the account menu panel is missing"
    panel = found.group(1)

    assert 'action="/portfolio/switch"' in panel, "no portfolio switcher"
    assert 'href="/profile"' in panel, "no account settings link"
    assert 'action="/logout"' in panel, "no log out"


def test_the_switcher_carries_no_inline_handler(client, session_factory):
    """It must not carry an `onchange` — an inline script is what holds
    `'unsafe-inline'` in the CSP. The behaviour lives in topbar.js now, and
    this is what stops it drifting back."""
    page = _page(client, session_factory)

    found = re.search(r'<select name="portfolio_id"[^>]*>', page)
    assert found, "the portfolio switcher is gone"
    assert "onchange" not in found.group(0), (
        "the switcher has an inline handler again — put it in topbar.js")


def test_the_switcher_still_tells_the_script_where_to_go_back_to(client, session_factory):
    """topbar.js resets the select to the current portfolio when somebody picks
    '+ New portfolio…', which needs the current id in the markup. Without it
    the box sits there naming a portfolio you are not in."""
    page = _page(client, session_factory)

    found = re.search(r'<select name="portfolio_id"[^>]*data-current="(\d+)"', page)
    assert found, "the switcher lost data-current, so topbar.js cannot restore it"


def test_the_dashboard_always_offers_a_way_to_the_charts(client, session_factory):
    """Charts is not in the nav, which makes the Portfolio page the only door
    to them — and the link then went missing during a later edit with nothing
    to catch it. Removing something from the nav means the replacement route
    has to be pinned, or it is one careless rewrite from being unreachable.
    """
    page = _page(client, session_factory)

    assert 'href="/charts"' in page, "no way to reach the charts page"


def test_the_charts_page_offers_a_way_back(client, session_factory):
    """Same argument in reverse: a spoke page reached by bookmark has no
    browser history to go back through."""
    from test_routes import make_login

    make_login(client, session_factory)
    page = client.get("/charts", headers={"accept": "text/html"})

    assert page.status_code == 200
    assert 'class="backlink"' in page.text


# --------------------------------------------------------------------------- #
# Choosing which entries the bar carries
# --------------------------------------------------------------------------- #

def _hide(client, session_factory, *keys):
    """Save the appearance form with `keys` unticked."""
    from test_routes import session_csrf

    shown = [k for k in navigation.HIDEABLE if k not in keys]
    return client.post("/profile/appearance",
                       data={"_csrf": session_csrf(session_factory),
                             "theme": "auto", "accent": "blue",
                             "nav_style": "both", "nav_show": shown},
                       headers={"accept": "text/html"}, follow_redirects=True)


def test_every_nav_entry_can_be_hidden(client, session_factory):
    """Including Portfolio: the brand mark already goes home, so hiding it
    loses nothing, and a bar somebody cannot empty is not really theirs."""
    assert set(navigation.HIDEABLE) == {item.key for item in navigation.NAV}


def test_the_account_page_offers_the_choice(client, session_factory):
    page = _page(client, session_factory)
    assert "/profile" in page

    from test_routes import make_login  # noqa: F401  (already signed in)

    account = client.get("/profile", headers={"accept": "text/html"}).text
    assert 'name="nav_show"' in account, "no way to choose which entries show"
    for item in navigation.NAV:
        assert f'value="{item.key}"' in account, f"{item.key} cannot be toggled"


def test_hiding_an_entry_takes_it_out_of_the_bar(client, session_factory):
    _page(client, session_factory)

    _hide(client, session_factory, "imports")

    page = client.get("/", headers={"accept": "text/html"}).text
    bar = page[page.index("<nav"):page.index("</nav>")]
    assert "Imports/Exports" not in bar
    assert "DCA Schedule" in bar, "only the unticked entry should go"


def test_hiding_everything_still_leaves_a_way_home(client, session_factory):
    """The reason Portfolio is allowed to go. If this ever fails, somebody has
    an app with no navigation at all."""
    _page(client, session_factory)

    _hide(client, session_factory, *navigation.HIDEABLE)

    page = client.get("/", headers={"accept": "text/html"}).text
    assert 'class="brand" href="/"' in page, "no home link with every entry hidden"


def test_the_choice_is_stored_as_what_is_HIDDEN(client, session_factory):
    """Not as what is shown. A nav entry added to the app later must appear for
    everybody rather than staying invisible until they revisit this page."""
    from sqlalchemy import select

    from app.models import User

    _page(client, session_factory)
    _hide(client, session_factory, "imports")

    with session_factory() as s:
        assert s.scalars(select(User)).first().nav_hidden == ["imports"]


def test_showing_everything_stores_nothing(client, session_factory):
    """NULL means "hide nothing" — an account that never opened the setting and
    one that ticked every box should look the same."""
    from sqlalchemy import select

    from app.models import User

    _page(client, session_factory)
    _hide(client, session_factory)

    with session_factory() as s:
        assert s.scalars(select(User)).first().nav_hidden is None


def test_the_account_menu_calls_it_settings(client, session_factory):
    """Just "Settings", so it sits level with "Log out" beside it — two words
    of similar weight rather than one long label and one short."""
    page = _page(client, session_factory)

    found = re.search(r'<div class="menuactions">(.*?)</div>', page, re.S)
    assert found, "the menu's two actions are not on one row"
    assert ">Settings<" in found.group(1)
    assert "Log out" in found.group(1)


def test_the_profile_control_shows_when_you_are_on_its_page(client, session_factory):
    """Every nav entry marks itself active on its own page; the profile control
    was the one destination that never did."""
    from test_routes import make_login  # noqa: F401 — already signed in

    _page(client, session_factory)
    account = client.get("/profile", headers={"accept": "text/html"}).text

    assert re.search(r'<details class="usermenu menu on"', account), (
        "the profile control does not look selected on the account page")

"""What a viewer is shown: nothing they would be refused.

The server refuses every write from a viewer (test_viewers_change_nothing),
but the pages still offered them all: the Record trade dialog, Manage
holdings' fields, the import forms and the plan editor. A viewer could fill
in a whole trade before the 403. Each page a viewer reads is held here to
offering no form that posts to a route a viewer is refused, and no link to a
page that refuses them.
"""

from __future__ import annotations

import ast
import inspect
import re
import textwrap
from html.parser import HTMLParser

import pytest
from sqlalchemy import select
from test_hostile_input import HTML, WHEN, _all_routes, _method, _signed_in
from test_viewers_change_nothing import WRITES

import factories as fac
from app import fyreport
from app.clock import today
from app.models import PortfolioMember, User

# The pages a viewer reads. Charts are each person's own, so theirs to change.
READS = ("/", "/?fy={fy}", "/holding/ACME", "/holdings", "/imports-exports", "/schedule",
         "/admin")
GATES = ("_require_write", "_require_admin", "_require_owner")


def _gated(route) -> bool:
    tree = ast.parse(textwrap.dedent(inspect.getsource(route.endpoint)))
    return any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
               and node.func.id in GATES for node in ast.walk(tree))


def _address(path: str) -> re.Pattern:
    """A route's path as the addresses a page can write for it."""
    return re.compile("^" + re.sub(r"\\\{[^}]+\\\}", "[^/?#]+", re.escape(path)) + "(?:[?#].*)?$")


REFUSED_POSTS = [_address(path) for path in WRITES]
REFUSED_PAGES = [_address(r.path) for r in _all_routes()
                 if _method(r) == "GET" and _gated(r)]


class _Offers(HTMLParser):
    """The forms that post, by where they post, and the links, by address."""

    def __init__(self):
        super().__init__()
        self.posts: list[str] = []
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form" and (a.get("method") or "").lower() == "post":
            self.posts.append(a.get("action") or "")
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])


def _refused_offers(page: str) -> list[str]:
    offers = _Offers()
    offers.feed(page)
    return ([f"form {action}" for action in offers.posts
             if any(p.match(action) for p in REFUSED_POSTS)]
            + [f"link {href}" for href in offers.links
               if any(p.match(href) for p in REFUSED_PAGES)])


def _furnished(client, session_factory, role: str):
    """Signed in, with a holding, a distribution and a plan, as `role`."""
    world = _signed_in(client, session_factory)
    with world.bound() as s:
        acme = s.get(fac.Instrument, world.instrument_id)
        fac.add_dividend(s, acme, WHEN, "4.00")
        pref = fac.hold(s, acme)
        pref.drp, pref.note = True, "Held for the income"
        s.commit()
    client.post("/schedule/save", headers=HTML, follow_redirects=False, data={
        "name": "Regular buys", "interval_days": "28", "amount": "500",
        "brokerage": "9.50", "start_date": WHEN, "tickers": "ACME", "_csrf": world.csrf()})
    with session_factory() as s:
        s.scalar(select(PortfolioMember).where(
            PortfolioMember.user_id == world.user_id,
            PortfolioMember.portfolio_id == world.portfolio_id)).role = role
        s.get(User, world.user_id).is_admin = False
        s.commit()
    return world


def _read(client, page: str) -> str:
    resp = client.get(page.format(fy=fyreport.current_fy(today())), headers=HTML)
    assert resp.status_code == 200, (page, resp.status_code)
    return resp.text


@pytest.mark.parametrize("page", READS)
def test_a_viewer_is_offered_nothing_they_would_be_refused(client, session_factory, page):
    _furnished(client, session_factory, "viewer")

    assert _refused_offers(_read(client, page)) == []


def test_the_same_pages_offer_a_member_their_writes(client, session_factory):
    """The mirror, so the scan above is shown to find what it looks for."""
    _furnished(client, session_factory, "member")

    offered = {page: _refused_offers(_read(client, page)) for page in READS}

    assert all(offered[page] for page in ("/", "/holding/ACME", "/holdings",
                                          "/imports-exports", "/schedule")), offered


def test_a_viewer_still_reads_a_holdings_settings(client, session_factory):
    _furnished(client, session_factory, "viewer")

    page = _read(client, "/holdings")

    assert '<td class="sub">reinvest</td>' in page
    assert '<td class="notecell">Held for the income</td>' in page


# The buttons that open a write's dialog: the scan above sees the dialog's
# form, and a button left behind without one would be a dead button.
OPENERS = [("/", "Record trade"), ("/holding/ACME", "Record a trade"),
           ("/holdings", "Add an instrument"), ("/schedule", "Edit plan"),
           ("/imports-exports", "Map a new broker CSV")]


@pytest.mark.parametrize("role", ["viewer", "member"])
def test_only_a_writer_is_shown_the_buttons_that_open_them(client, session_factory, role):
    _furnished(client, session_factory, role)

    shown = [label for page, label in OPENERS if label in _read(client, page)]

    assert shown == ([] if role == "viewer" else [label for _page, label in OPENERS])


@pytest.mark.parametrize("page", ["/trade/new", "/schedule/complete"])
def test_a_page_that_is_only_a_write_is_refused_to_a_viewer(client, session_factory, page):
    """Opened from a bookmark, they took a whole trade before refusing it."""
    _furnished(client, session_factory, "viewer")

    resp = client.get(page, headers=HTML)

    assert resp.status_code == 403
    assert "read-only access" in resp.text

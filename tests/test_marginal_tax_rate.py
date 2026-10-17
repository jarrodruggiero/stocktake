"""A portfolio records the marginal tax rate its gains are taxed at.

The sale simulator taxes what a sale adds to the year's net capital gain at
this rate (decisions.md #147). It is the portfolio's, as its brokerage is: the
owner sets it in portfolio settings, and another app can set it through the
API, which is otherwise append-only (#88), because a setting is not a record.
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest
from test_api import bearer, issue_key

import factories as fac
from app.models import Portfolio
from test_routes import bind_to_only_portfolio, make_login, session_csrf

HTML = {"accept": "text/html"}
RATE = "/api/v1/portfolio/marginal-tax-rate"


@pytest.fixture
def owner(client, session_factory):
    """Signed in as the portfolio's owner (as test_brokerage_fees.owner, copied
    rather than imported: testing.md)."""
    make_login(client, session_factory)
    with session_factory() as s:
        portfolio = bind_to_only_portfolio(s)
        s.commit()
        return {"portfolio": portfolio.id, "user": portfolio.members[0].user_id}


def _settings(client, session_factory, **fields):
    data = {"_csrf": session_csrf(session_factory), "name": "Main",
            "reporting_currency": "AUD", "jurisdiction": "AU", **fields}
    return client.post("/portfolio/settings", data=data, headers=HTML,
                       follow_redirects=False)


def _stored(session_factory) -> Decimal | None:
    with session_factory() as s:
        return s.query(Portfolio).one().marginal_tax_rate


def _rate_field(page: str) -> str:
    """The rate's own input, so a match is never another field's value."""
    found = re.search(r'<input[^>]*name="marginal_tax_rate"[^>]*>', page)
    assert found, "the settings form has no marginal tax rate field"
    return found.group(0)


def test_the_owner_sets_it_in_portfolio_settings(client, session_factory, owner):
    resp = _settings(client, session_factory, marginal_tax_rate="32.5")

    assert resp.status_code == 303, resp.text
    assert _stored(session_factory) == Decimal("32.50")
    assert 'value="32.5"' in _rate_field(client.get("/members", headers=HTML).text)


def test_left_blank_it_is_not_set(client, session_factory, owner):
    _settings(client, session_factory, marginal_tax_rate="32")
    assert _stored(session_factory) == Decimal("32.00")

    _settings(client, session_factory, marginal_tax_rate="")

    assert _stored(session_factory) is None


@pytest.mark.parametrize("typed, message", [
    ("abc", "Marginal tax rate: &#39;abc&#39; is not a number."),
    ("-1", "Marginal tax rate can&#39;t be negative."),
    ("100.01", "Marginal tax rate can&#39;t be more than 100."),
])
def test_a_rate_that_is_not_one_is_refused_and_comes_back(client, session_factory, owner,
                                                          typed, message):
    resp = _settings(client, session_factory, marginal_tax_rate=typed)

    assert resp.status_code == 400
    assert message in resp.text
    assert f'value="{typed}"' in _rate_field(resp.text)
    assert _stored(session_factory) is None


def test_the_api_reads_it(client, session_factory, owner):
    key = bearer(issue_key(session_factory, owner["portfolio"], created_by=owner["user"]))
    unset = client.get("/api/v1/portfolio", headers=key).json()["portfolio"]
    _settings(client, session_factory, marginal_tax_rate="47")

    assert unset["marginal_tax_rate"] is None
    assert client.get("/api/v1/portfolio", headers=key).json()["portfolio"][
        "marginal_tax_rate"] == 47.0


def test_the_owners_write_key_sets_and_clears_it(client, session_factory, owner):
    key = bearer(issue_key(session_factory, owner["portfolio"], created_by=owner["user"],
                           scopes="read,write"))

    set_ = client.put(RATE, headers=key, json={"marginal_tax_rate": 32.5})
    assert (set_.status_code, set_.json()) == (200, {"marginal_tax_rate": 32.5})
    assert _stored(session_factory) == Decimal("32.50")

    cleared = client.put(RATE, headers=key, json={"marginal_tax_rate": None})
    assert (cleared.status_code, _stored(session_factory)) == (200, None)


@pytest.mark.parametrize("rate", [-1, 100.01, "abc"])
def test_the_api_refuses_a_rate_that_is_not_one(client, session_factory, owner, rate):
    key = bearer(issue_key(session_factory, owner["portfolio"], created_by=owner["user"],
                           scopes="read,write"))

    resp = client.put(RATE, headers=key, json={"marginal_tax_rate": rate})

    assert resp.status_code == 422
    assert _stored(session_factory) is None


def test_a_read_key_cannot_set_it(client, session_factory, owner):
    key = bearer(issue_key(session_factory, owner["portfolio"], created_by=owner["user"]))

    resp = client.put(RATE, headers=key, json={"marginal_tax_rate": 30})

    assert resp.status_code == 403
    assert _stored(session_factory) is None


def test_only_the_owner_sets_it_through_the_api(client, session_factory, owner):
    """A portfolio's settings are its owner's (decisions.md #34), and a key does
    no more than its creator can now (#128)."""
    with session_factory() as s:
        portfolio = s.get(Portfolio, owner["portfolio"])
        member = fac.make_user(s, "member@example.test")
        fac.add_member(s, portfolio, member, role="member")
        s.commit()
        member_id = member.id
    key = bearer(issue_key(session_factory, owner["portfolio"], created_by=member_id,
                           scopes="read,write"))

    resp = client.put(RATE, headers=key, json={"marginal_tax_rate": 30})

    assert resp.status_code == 403
    assert _stored(session_factory) is None

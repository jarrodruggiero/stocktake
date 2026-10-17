"""A portfolio records the rates its gains are taxed at: an income tax rate and
the Medicare levy, apart.

The sale simulator taxes what a sale adds to the year's net capital gain at
both (decisions.md #147). They are kept apart because from 1 July 2027 the
minimum tax on capital gains compares 30% with income tax alone. They are the
portfolio's, as its brokerage is: the owner sets them in portfolio settings,
and another app can set them through the API, which is otherwise append-only
(#88), because a setting is not a record.
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
RATES = "/api/v1/portfolio/tax-rates"
FIELDS = ("income_tax_rate", "medicare_levy")


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


def _stored(session_factory) -> tuple:
    with session_factory() as s:
        portfolio = s.query(Portfolio).one()
        return portfolio.income_tax_rate, portfolio.medicare_levy


def _field(page: str, name: str) -> str:
    """One field's own input, so a match is never another field's value."""
    found = re.search(rf'<input[^>]*name="{name}"[^>]*>', page)
    assert found, f"the settings form has no {name} field"
    return found.group(0)


def test_the_owner_sets_both_in_portfolio_settings(client, session_factory, owner):
    resp = _settings(client, session_factory, income_tax_rate="30", medicare_levy="2")

    assert resp.status_code == 303, resp.text
    assert _stored(session_factory) == (Decimal("30.00"), Decimal("2.00"))
    page = client.get("/members", headers=HTML).text
    assert 'value="30"' in _field(page, "income_tax_rate")
    assert 'value="2"' in _field(page, "medicare_levy")


def test_left_blank_each_is_not_set(client, session_factory, owner):
    _settings(client, session_factory, income_tax_rate="30", medicare_levy="2")
    assert _stored(session_factory) == (Decimal("30.00"), Decimal("2.00"))

    _settings(client, session_factory, income_tax_rate="", medicare_levy="")

    assert _stored(session_factory) == (None, None)


@pytest.mark.parametrize("field, label", [("income_tax_rate", "Income tax rate"),
                                          ("medicare_levy", "Medicare levy")])
@pytest.mark.parametrize("typed, message", [
    ("abc", "{label}: &#39;abc&#39; is not a number."),
    ("-1", "{label} can&#39;t be negative."),
    ("100.01", "{label} can&#39;t be more than 100."),
])
def test_a_rate_that_is_not_one_is_refused_and_comes_back(client, session_factory, owner,
                                                          field, label, typed, message):
    resp = _settings(client, session_factory, **{field: typed})

    assert resp.status_code == 400
    assert message.format(label=label) in resp.text
    assert f'value="{typed}"' in _field(resp.text, field)
    assert _stored(session_factory) == (None, None)


def test_the_api_reads_them(client, session_factory, owner):
    key = bearer(issue_key(session_factory, owner["portfolio"], created_by=owner["user"]))
    unset = client.get("/api/v1/portfolio", headers=key).json()["portfolio"]
    _settings(client, session_factory, income_tax_rate="45", medicare_levy="2")
    set_ = client.get("/api/v1/portfolio", headers=key).json()["portfolio"]

    assert [unset[f] for f in FIELDS] == [None, None]
    assert [set_[f] for f in FIELDS] == [45.0, 2.0]


def test_the_owners_write_key_sets_either_and_leaves_the_other(client, session_factory,
                                                                owner):
    key = bearer(issue_key(session_factory, owner["portfolio"], created_by=owner["user"],
                           scopes="read,write"))

    both = client.put(RATES, headers=key, json={"income_tax_rate": 30, "medicare_levy": 2})
    assert (both.status_code, both.json()) == (
        200, {"income_tax_rate": 30.0, "medicare_levy": 2.0, "income_support": False})

    one = client.put(RATES, headers=key, json={"income_tax_rate": 37})
    assert one.json() == {"income_tax_rate": 37.0, "medicare_levy": 2.0,
                          "income_support": False}

    cleared = client.put(RATES, headers=key, json={"medicare_levy": None})
    assert (cleared.status_code, _stored(session_factory)) == (200, (Decimal("37.00"), None))


@pytest.mark.parametrize("body", [{"income_tax_rate": -1}, {"income_tax_rate": 100.01},
                                  {"medicare_levy": "abc"}, {"medicare_levy": 101}])
def test_the_api_refuses_a_rate_that_is_not_one(client, session_factory, owner, body):
    key = bearer(issue_key(session_factory, owner["portfolio"], created_by=owner["user"],
                           scopes="read,write"))

    resp = client.put(RATES, headers=key, json=body)

    assert resp.status_code == 422
    assert _stored(session_factory) == (None, None)


def test_a_read_key_cannot_set_them(client, session_factory, owner):
    key = bearer(issue_key(session_factory, owner["portfolio"], created_by=owner["user"]))

    resp = client.put(RATES, headers=key, json={"income_tax_rate": 30})

    assert resp.status_code == 403
    assert _stored(session_factory) == (None, None)


def test_only_the_owner_sets_them_through_the_api(client, session_factory, owner):
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

    resp = client.put(RATES, headers=key, json={"income_tax_rate": 30})

    assert resp.status_code == 403
    assert _stored(session_factory) == (None, None)

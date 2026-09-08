"""What the users page tells an administrator before they disable somebody.

Disabling is instant and total: it flips `is_active` and destroys every session
that account holds. Two facts make that decision safe to take, and neither was
on the page.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

import factories as fac
from app import federation
from app.models import Portfolio, PortfolioMember, User
from appcore import oidc
from test_routes import make_login

HTML = {"accept": "text/html"}


@pytest.fixture
def owner(client, session_factory):
    make_login(client, session_factory)
    return client


def test_disabling_the_only_member_of_a_portfolio_warns_first(owner, session_factory):
    """A portfolio is reachable only through membership, so disabling its only
    member takes it out of everybody's view — administrators included. The
    users page is a long way from anything that says so.
    """
    with session_factory() as db:
        second = fac.make_user(db, "second@example.test")
        fac.make_portfolio(db, "Only theirs", owner=second)
        db.commit()

    page = owner.get("/users", headers=HTML).text

    assert "Only theirs" in page
    assert "out of everyone" in page


def test_no_warning_when_nothing_would_be_stranded(owner, session_factory):
    """A warning on every row is a warning nobody reads."""
    with session_factory() as db:
        second = fac.make_user(db, "second@example.test")
        db.add(PortfolioMember(portfolio_id=db.scalar(select(Portfolio)).id,
                               user_id=second.id, role="member"))
        db.commit()

    assert "out of everyone" not in owner.get("/users", headers=HTML).text


def test_an_account_that_signs_in_through_a_provider_says_so(owner, session_factory):
    """Which accounts the directory does not control. Removing somebody there
    stops them signing in; it does not end the session they have."""
    with session_factory() as db:
        user = db.scalar(select(User))
        federation.link(db, user, oidc.Identity(
            subject="s", issuer="https://idp.example.test", email=user.email,
            email_verified=True, name=user.name))
        db.commit()

    assert ">SSO<" in owner.get("/users", headers=HTML).text

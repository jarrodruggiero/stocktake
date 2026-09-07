"""The invitation flow through the endpoints a browser actually uses.

`test_invites.py` covers the rules; this covers reaching them — that the
landing page is public, that signing up needs an invite, and that a spent link
stops working for the next person to open it.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import select

from app import invites
from app.models import PortfolioInvite, PortfolioMember, User
from test_routes import PASSWORD, make_login, pre_auth_csrf, session_csrf

HTML = {"accept": "text/html"}


def _issue(client, session_factory, role: str = "member") -> str:
    """Create an invite through the real endpoint and return its token."""
    resp = client.post("/members/invite", follow_redirects=False,
                       data={"_csrf": session_csrf(session_factory), "role": role})
    return re.search(r"invite=([^&]+)", resp.headers["location"]).group(1)


@pytest.fixture
def owner(client, session_factory):
    make_login(client, session_factory)
    return client


# --------------------------------------------------------------------------- #
# Issuing
# --------------------------------------------------------------------------- #

def test_an_owner_can_issue_a_link_and_it_is_shown_once(owner, session_factory):
    token = _issue(owner, session_factory)
    page = owner.get("/members?invite=" + token, headers=HTML).text

    assert f"/invite/{token}" in page
    # Gone from the page as soon as it is not in the URL: the database keeps
    # only the hash, so there is nothing to render a second time.
    assert token not in owner.get("/members", headers=HTML).text


def test_only_an_owner_can_issue_one(client, session_factory):
    make_login(client, session_factory, email="member@example.test", admin=False)
    with session_factory() as db:
        member = db.scalar(select(User).where(User.email == "member@example.test"))
        db.scalar(select(PortfolioMember).where(
            PortfolioMember.user_id == member.id)).role = "member"
        db.commit()

    resp = client.post("/members/invite", follow_redirects=False,
                       data={"_csrf": session_csrf(session_factory), "role": "owner"})

    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# Accepting without an account
# --------------------------------------------------------------------------- #

def test_the_landing_page_needs_no_account(client, owner, session_factory):
    """The whole point is somebody who has not signed in. The middleware must
    let them through, and the page must name what they are joining."""
    token = _issue(owner, session_factory)
    owner.post("/logout", data={"_csrf": session_csrf(session_factory)},
               follow_redirects=False)

    page = client.get(f"/invite/{token}", headers=HTML)

    assert page.status_code == 200
    assert "Test portfolio" in page.text


def test_signing_up_through_an_invite_creates_the_account_and_the_membership(
    client, owner, session_factory
):
    token = _issue(owner, session_factory, role="viewer")
    owner.post("/logout", data={"_csrf": session_csrf(session_factory)},
               follow_redirects=False)

    resp = client.post(f"/invite/{token}/signup", follow_redirects=False,
                       data={"_csrf": pre_auth_csrf(client), "name": "Joiner",
                             "email": "joiner@example.test", "password": PASSWORD})

    assert resp.headers["location"] == "/"
    with session_factory() as db:
        joiner = db.scalar(select(User).where(User.email == "joiner@example.test"))
        member = db.scalar(select(PortfolioMember).where(
            PortfolioMember.user_id == joiner.id))
        assert member.role == "viewer"
    # Signed in already: being sent back to a login page after creating an
    # account is a step nobody needs.
    assert client.get("/", headers=HTML, follow_redirects=False).status_code == 200


def test_a_new_account_gets_recovery_codes_it_is_asked_to_save(
    client, owner, session_factory
):
    """Issued but not shown, exactly as for an admin-created account: the
    banner asks them to save a set of their own on first sign-in. It is what
    gets them back in if they ever lose the way they signed up with.
    """
    token = _issue(owner, session_factory)
    owner.post("/logout", data={"_csrf": session_csrf(session_factory)},
               follow_redirects=False)
    client.post(f"/invite/{token}/signup", follow_redirects=False,
                data={"_csrf": pre_auth_csrf(client), "name": "Joiner",
                      "email": "joiner@example.test", "password": PASSWORD})

    with session_factory() as db:
        joiner = db.scalar(select(User).where(User.email == "joiner@example.test"))
        assert joiner.recovery_codes_seen_at is None
    assert "no saved recovery codes" in client.get("/", headers=HTML).text


def test_signing_up_without_an_invite_is_not_possible(client, owner, session_factory):
    """The invite is the whole of the authorisation. Without this, the endpoint
    is open registration on an app holding somebody's holdings."""
    owner.post("/logout", data={"_csrf": session_csrf(session_factory)},
               follow_redirects=False)

    resp = client.post("/invite/not-a-real-token/signup", headers=HTML,
                       follow_redirects=False,
                       data={"_csrf": pre_auth_csrf(client), "name": "Nobody",
                             "email": "nobody@example.test", "password": PASSWORD})

    assert resp.status_code == 410
    with session_factory() as db:
        assert db.scalar(select(User).where(
            User.email == "nobody@example.test")) is None


def test_an_email_that_already_exists_is_sent_to_sign_in(
    client, owner, session_factory
):
    token = _issue(owner, session_factory)
    owner.post("/logout", data={"_csrf": session_csrf(session_factory)},
               follow_redirects=False)

    page = client.post(f"/invite/{token}/signup", headers=HTML,
                       follow_redirects=False,
                       data={"_csrf": pre_auth_csrf(client), "name": "Again",
                             "email": "user@example.test", "password": PASSWORD})

    assert "already has an account" in page.text
    # Still live: a mistyped email must not burn the invitation.
    with session_factory() as db:
        assert db.scalar(select(PortfolioInvite)).used_at is None


# --------------------------------------------------------------------------- #
# Accepting with one
# --------------------------------------------------------------------------- #

def test_an_existing_account_joins_without_making_another(
    client, owner, session_factory
):
    token = _issue(owner, session_factory)
    owner.post("/logout", data={"_csrf": session_csrf(session_factory)},
               follow_redirects=False)
    # `make_login` creates the account AND a portfolio of its own, which is the
    # state that matters here: joining must add access, not replace it.
    make_login(client, session_factory, email="other@example.test")

    resp = client.post(f"/invite/{token}/accept", follow_redirects=False,
                       data={"_csrf": session_csrf(session_factory)})

    assert resp.headers["location"] == "/"
    with session_factory() as db:
        joined = db.scalar(select(User).where(User.email == "other@example.test"))
        assert len(db.scalars(select(PortfolioMember).where(
            PortfolioMember.user_id == joined.id)).all()) == 2


def test_a_spent_link_tells_the_next_person_so(client, owner, session_factory):
    token = _issue(owner, session_factory)
    owner.post("/logout", data={"_csrf": session_csrf(session_factory)},
               follow_redirects=False)
    client.post(f"/invite/{token}/signup", follow_redirects=False,
                data={"_csrf": pre_auth_csrf(client), "name": "First",
                      "email": "first@example.test", "password": PASSWORD})

    page = client.get(f"/invite/{token}", headers=HTML)

    assert page.status_code == 410
    assert "already been used" in page.text


def test_a_withdrawn_link_stops_working(owner, session_factory):
    token = _issue(owner, session_factory)
    with session_factory() as db:
        invite_id = db.scalar(select(PortfolioInvite)).id

    owner.post(f"/members/invite/{invite_id}/revoke", follow_redirects=False,
               data={"_csrf": session_csrf(session_factory)})

    assert owner.get(f"/invite/{token}", headers=HTML).status_code == 410


def test_the_owners_list_shows_what_happened_to_each_one(owner, session_factory):
    _issue(owner, session_factory)
    page = owner.get("/members", headers=HTML).text

    assert ">Live<" in page and "Withdraw" in page
    with session_factory() as db:
        db.scalar(select(PortfolioInvite)).used_at = invites._utcnow()
        db.commit()

    page = owner.get("/members", headers=HTML).text
    assert ">Used<" in page and "Withdraw" not in page


def test_the_banner_can_be_put_away_for_this_session_only(client, session_factory):
    """"Not now" must not mean "never": an account with no saved codes is one
    forgotten password from being unreachable, so a new session asks again."""
    make_login(client, session_factory)
    assert "no saved recovery codes" in client.get("/", headers=HTML).text

    client.post("/profile/recovery/later", follow_redirects=False,
                data={"_csrf": session_csrf(session_factory)})
    assert "no saved recovery codes" not in client.get("/", headers=HTML).text

    client.post("/logout", data={"_csrf": session_csrf(session_factory)},
                follow_redirects=False)
    # Sign in again through the form rather than `make_login`, which would try
    # to create the account a second time. A NEW session is the whole point.
    client.post("/login", follow_redirects=False, headers=HTML,
                data={"email": "user@example.test", "password": PASSWORD,
                      "_csrf": pre_auth_csrf(client)})

    assert "no saved recovery codes" in client.get("/", headers=HTML).text

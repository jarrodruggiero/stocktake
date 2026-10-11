"""Account, portfolio, member, key and instrument routes.

Most of these are ordinary forms, but several enforce rules that only exist
because getting them wrong loses something: a portfolio must keep an owner, an
API key is shown once and never again, and changing your password signs out
every other device. Those are the ones worth reading.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

import factories as fac
from app import auth as auth_mod
from app.models import (
    HoldingPref,
    Instrument,
    Portfolio,
    PortfolioInvite,
    PortfolioMember,
    User,
    UserSession,
)
from test_routes import PASSWORD, make_login, reading, session_csrf

HTML = {"accept": "text/html"}


def csrf(session_factory) -> str:
    return session_csrf(session_factory)


# --------------------------------------------------------------------------- #
# Password
# --------------------------------------------------------------------------- #

def test_changing_your_password_works_and_keeps_you_signed_in(client, session_factory):
    make_login(client, session_factory)

    resp = client.post("/profile/password",
                       data={"current_password": PASSWORD, "password": "a-longer-new-one",
                             "confirm": "a-longer-new-one", "_csrf": csrf(session_factory)},
                       headers=HTML, follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/profile?saved=password"
    # Still signed in: the page you are on should not log you out.
    assert client.get("/", headers=HTML, follow_redirects=False).status_code == 200


def test_changing_your_password_signs_out_every_other_device(client, session_factory):
    """A password change is what you do when you think somebody else has it, so
    every other session has to die — while this one survives."""
    make_login(client, session_factory)
    token = csrf(session_factory)      # ours, before another session exists
    with session_factory() as s:
        user = s.scalars(select(User)).one()
        auth_mod.create_session(s, user, __import__("app.settings", fromlist=["x"])
                                .PortfolioSettings())
        s.commit()
    with session_factory() as s:
        assert len(s.scalars(select(UserSession)).all()) == 2

    client.post("/profile/password",
                data={"current_password": PASSWORD, "password": "a-longer-new-one",
                      "confirm": "a-longer-new-one", "_csrf": token},
                headers=HTML)

    with session_factory() as s:
        remaining = s.scalars(select(UserSession)).all()
    assert len(remaining) == 1          # only the one that made the change


@pytest.mark.parametrize("payload,message", [
    ({"current_password": "wrong-password", "password": "a-longer-new-one",
      "confirm": "a-longer-new-one"}, "Current password is incorrect"),
    ({"current_password": PASSWORD, "password": "a-longer-new-one",
      "confirm": "something-else-entirely"}, "new passwords"),
    ({"current_password": PASSWORD, "password": "short", "confirm": "short"},
     "at least 10"),
])
def test_a_bad_password_change_is_refused_with_a_reason(client, session_factory,
                                                        payload, message):
    make_login(client, session_factory)

    resp = client.post("/profile/password", data={**payload, "_csrf": csrf(session_factory)},
                       headers=HTML)

    assert message in resp.text


def test_a_temporary_password_is_cleared_once_it_is_changed(client, session_factory):
    make_login(client, session_factory, email="fresh@example.test", must_change=True)

    client.post("/profile/password",
                data={"current_password": PASSWORD, "password": "a-longer-new-one",
                      "confirm": "a-longer-new-one", "_csrf": csrf(session_factory)},
                headers=HTML)

    with session_factory() as s:
        assert s.scalars(select(User)).one().must_change_password is False
    # …and the forced redirect to /profile is gone.
    assert client.get("/", headers=HTML, follow_redirects=False).status_code == 200


# --------------------------------------------------------------------------- #
# Accounts (admin)
# --------------------------------------------------------------------------- #

def test_deactivating_an_account_signs_it_out_everywhere(client, session_factory):
    make_login(client, session_factory, admin=True)
    token = csrf(session_factory)   # before another session exists
    with session_factory() as s:
        other = fac.make_user(s, "other@example.test")
        settings = __import__("app.settings", fromlist=["x"]).PortfolioSettings()
        auth_mod.create_session(s, other, settings)
        s.commit()
        other_id = other.id

    client.post(f"/users/{other_id}/active", data={"_csrf": token},
                headers=HTML)

    with session_factory() as s:
        assert s.get(User, other_id).is_active is False
        # A disabled account must not keep a live session lying around.
        assert s.scalars(
            select(UserSession).where(UserSession.user_id == other_id)).all() == []


def test_you_cannot_deactivate_yourself(client, session_factory):
    """Locking the only admin out of their own instance is not a thing the UI
    should let you do by mis-clicking."""
    make_login(client, session_factory, admin=True)
    with session_factory() as s:
        me = s.scalars(select(User)).one().id

    resp = client.post(f"/users/{me}/active", data={"_csrf": csrf(session_factory)},
                       headers=HTML)

    assert resp.status_code == 400


def test_toggling_an_unknown_account_is_a_404(client, session_factory):
    make_login(client, session_factory, admin=True)

    assert client.post("/users/9999/active", data={"_csrf": csrf(session_factory)},
                       headers=HTML).status_code == 404


def test_a_duplicate_email_is_refused(client, session_factory):
    make_login(client, session_factory, admin=True, email="taken@example.test")

    resp = client.post("/users/add",
                       data={"name": "Someone", "email": "taken@example.test",
                             "password": "a-long-enough-one", "_csrf": csrf(session_factory)},
                       headers=HTML)

    assert "already has an account" in resp.text


def test_a_new_account_gets_a_portfolio_of_its_own(client, session_factory):
    """Everyone starts with somewhere to put their own holdings; sharing is
    opt-in from the other direction."""
    make_login(client, session_factory, admin=True)

    client.post("/users/add",
                data={"name": "New Person", "email": "new@example.test",
                      "password": "a-long-enough-one", "_csrf": csrf(session_factory)},
                headers=HTML)

    with session_factory() as s:
        new_user = s.scalars(select(User).where(User.email == "new@example.test")).one()
        memberships = s.scalars(
            select(PortfolioMember).where(PortfolioMember.user_id == new_user.id)).all()
    assert [m.role for m in memberships] == ["owner"]
    assert new_user.must_change_password is True


def test_resetting_a_password_forces_a_change_and_signs_them_out(client, session_factory):
    make_login(client, session_factory, admin=True)
    token = csrf(session_factory)   # before another session exists
    with session_factory() as s:
        other = fac.make_user(s, "other@example.test")
        settings = __import__("app.settings", fromlist=["x"]).PortfolioSettings()
        auth_mod.create_session(s, other, settings)
        s.commit()
        other_id = other.id

    client.post(f"/users/{other_id}/reset",
                data={"password": "a-fresh-temporary-one", "_csrf": token},
                headers=HTML)

    with session_factory() as s:
        assert s.get(User, other_id).must_change_password is True
        assert s.scalars(
            select(UserSession).where(UserSession.user_id == other_id)).all() == []


def test_a_short_reset_password_is_refused(client, session_factory):
    make_login(client, session_factory, admin=True)
    with session_factory() as s:
        other = fac.make_user(s, "other@example.test")
        s.commit()
        other_id = other.id

    assert client.post(f"/users/{other_id}/reset",
                       data={"password": "short", "_csrf": csrf(session_factory)},
                       headers=HTML).status_code == 400


# --------------------------------------------------------------------------- #
# Portfolios
# --------------------------------------------------------------------------- #

def test_creating_a_portfolio_makes_you_its_owner_and_switches_to_it(client,
                                                                    session_factory):
    make_login(client, session_factory)

    resp = client.post("/portfolio/new", data={"name": "Family portfolio",
                                               "_csrf": csrf(session_factory)},
                       headers=HTML, follow_redirects=False)

    assert resp.status_code == 303
    with session_factory() as s:
        created = s.scalars(select(Portfolio).where(Portfolio.name == "Family portfolio")).one()
        member = s.scalars(select(PortfolioMember).where(
            PortfolioMember.portfolio_id == created.id)).one()
        assert member.role == "owner"
        # The session moved to it, or you would create a portfolio and not be in it.
        assert s.scalars(select(UserSession).order_by(
            UserSession.id.desc())).first().active_portfolio_id == created.id


def test_a_portfolio_needs_a_name(client, session_factory):
    make_login(client, session_factory)

    assert client.post("/portfolio/new", data={"name": "   ", "_csrf": csrf(session_factory)},
                       headers=HTML).status_code == 400


def test_switching_to_a_portfolio_you_are_not_in_is_refused(client, session_factory):
    """The id comes from a form, so it is checked against your own memberships —
    a forged value must not move your session into somebody else's holdings."""
    make_login(client, session_factory)
    with session_factory() as s:
        stranger = fac.make_user(s, "stranger@example.test")
        theirs = fac.make_portfolio(s, "Not yours", owner=stranger)
        s.commit()
        theirs_id = theirs.id

    resp = client.post("/portfolio/switch",
                       data={"portfolio_id": str(theirs_id), "_csrf": csrf(session_factory)},
                       headers=HTML)

    assert resp.status_code == 403


def test_switching_to_one_of_your_own_works(client, session_factory):
    make_login(client, session_factory)
    client.post("/portfolio/new", data={"name": "Second", "_csrf": csrf(session_factory)},
                headers=HTML)
    with session_factory() as s:
        first = s.scalars(select(Portfolio).order_by(Portfolio.id)).first().id

    resp = client.post("/portfolio/switch",
                       data={"portfolio_id": str(first), "_csrf": csrf(session_factory)},
                       headers=HTML, follow_redirects=False)

    assert resp.status_code == 303
    with session_factory() as s:
        assert s.scalars(select(UserSession).order_by(
            UserSession.id.desc())).first().active_portfolio_id == first


# --------------------------------------------------------------------------- #
# Members
# --------------------------------------------------------------------------- #

def add_stranger(session_factory, email="friend@example.test") -> int:
    with session_factory() as s:
        user = fac.make_user(s, email)
        s.commit()
        return user.id


def test_a_member_can_be_added_and_given_a_role(client, session_factory):
    make_login(client, session_factory)
    friend = add_stranger(session_factory)

    client.post("/members/add", data={"user_id": str(friend), "role": "viewer",
                                      "_csrf": csrf(session_factory)}, headers=HTML)

    with session_factory() as s:
        member = s.scalars(select(PortfolioMember).where(
            PortfolioMember.user_id == friend)).one()
        assert member.role == "viewer"


def test_an_unknown_role_is_refused(client, session_factory):
    make_login(client, session_factory)
    friend = add_stranger(session_factory)

    assert client.post("/members/add",
                       data={"user_id": str(friend), "role": "superuser",
                             "_csrf": csrf(session_factory)}, headers=HTML).status_code == 400


def test_adding_an_unknown_account_is_a_404(client, session_factory):
    make_login(client, session_factory)

    assert client.post("/members/add", data={"user_id": "9999", "role": "member",
                                             "_csrf": csrf(session_factory)},
                       headers=HTML).status_code == 404


def test_adding_the_same_person_twice_says_so(client, session_factory):
    make_login(client, session_factory)
    friend = add_stranger(session_factory)
    data = {"user_id": str(friend), "role": "member", "_csrf": csrf(session_factory)}
    client.post("/members/add", data=data, headers=HTML)

    resp = client.post("/members/add", data=data, headers=HTML, follow_redirects=False)

    assert "Already+a+member" in resp.headers["location"]


def test_a_members_role_can_be_changed(client, session_factory):
    make_login(client, session_factory)
    friend = add_stranger(session_factory)
    client.post("/members/add", data={"user_id": str(friend), "role": "viewer",
                                      "_csrf": csrf(session_factory)}, headers=HTML)
    with session_factory() as s:
        member_id = s.scalars(select(PortfolioMember).where(
            PortfolioMember.user_id == friend)).one().id

    client.post(f"/members/{member_id}/role", data={"role": "member",
                                                    "_csrf": csrf(session_factory)},
                headers=HTML)

    with session_factory() as s:
        assert s.get(PortfolioMember, member_id).role == "member"


def test_you_cannot_demote_yourself_out_of_ownership(client, session_factory):
    """Demoting the only owner would orphan the portfolio — nobody left who
    could add members or issue keys."""
    make_login(client, session_factory)
    with session_factory() as s:
        mine = s.scalars(select(PortfolioMember)).one().id

    resp = client.post(f"/members/{mine}/role", data={"role": "viewer",
                                                      "_csrf": csrf(session_factory)},
                       headers=HTML, follow_redirects=False)

    assert "Hand+ownership+over+first" in resp.headers["location"]


def test_the_last_owner_cannot_be_removed(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        mine = s.scalars(select(PortfolioMember)).one().id

    resp = client.post(f"/members/{mine}/remove", data={"_csrf": csrf(session_factory)},
                       headers=HTML, follow_redirects=False)

    assert "needs+an+owner" in resp.headers["location"]


def test_a_member_can_be_removed(client, session_factory):
    make_login(client, session_factory)
    friend = add_stranger(session_factory)
    client.post("/members/add", data={"user_id": str(friend), "role": "member",
                                      "_csrf": csrf(session_factory)}, headers=HTML)
    with session_factory() as s:
        member_id = s.scalars(select(PortfolioMember).where(
            PortfolioMember.user_id == friend)).one().id

    client.post(f"/members/{member_id}/remove", data={"_csrf": csrf(session_factory)},
                headers=HTML)

    with session_factory() as s:
        assert s.get(PortfolioMember, member_id) is None


def test_a_member_of_another_portfolio_cannot_be_touched(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        stranger = fac.make_user(s, "stranger@example.test")
        theirs = fac.make_portfolio(s, "Theirs", owner=stranger)
        s.commit()
        their_membership = s.scalars(select(PortfolioMember).where(
            PortfolioMember.portfolio_id == theirs.id)).one().id

    assert client.post(f"/members/{their_membership}/remove",
                       data={"_csrf": csrf(session_factory)},
                       headers=HTML).status_code == 404


# --------------------------------------------------------------------------- #
# Instruments
# --------------------------------------------------------------------------- #

def test_adding_an_instrument_fills_the_blanks_from_the_price_feed(client,
                                                                  session_factory,
                                                                  monkeypatch):
    from app import main as main_mod

    monkeypatch.setattr(main_mod.pricefeed, "lookup",
                        lambda ticker, exchange: {"symbol": "ACME.AX",
                                                  "name": "Acme Industries Ltd",
                                                  "currency": "AUD", "found": True})
    make_login(client, session_factory)

    client.post("/holdings/add",
                data={"ticker": "acme", "exchange": "asx", "asset_class": "share",
                      "name": "", "currency": "", "_csrf": csrf(session_factory)},
                headers=HTML)

    with session_factory() as s:
        inst = s.scalars(select(Instrument)).one()
    assert (inst.ticker, inst.exchange) == ("ACME", "ASX")
    assert inst.name == "Acme Industries Ltd"
    assert inst.yahoo_symbol == "ACME.AX"


def test_what_was_typed_beats_what_the_feed_says(client, session_factory, monkeypatch):
    from app import main as main_mod

    monkeypatch.setattr(main_mod.pricefeed, "lookup",
                        lambda ticker, exchange: {"symbol": "X", "name": "Wrong Name",
                                                  "currency": "USD", "found": True})
    make_login(client, session_factory)

    client.post("/holdings/add",
                data={"ticker": "ACME", "exchange": "ASX", "asset_class": "etf",
                      "name": "What I Called It", "currency": "AUD",
                      "_csrf": csrf(session_factory)},
                headers=HTML)

    with session_factory() as s:
        inst = s.scalars(select(Instrument)).one()
    assert inst.name == "What I Called It"
    assert inst.currency == "AUD"


def test_an_unknown_asset_class_is_refused(client, session_factory):
    make_login(client, session_factory)

    resp = client.post("/holdings/add",
                       data={"ticker": "ACME", "exchange": "ASX", "asset_class": "gold",
                             "_csrf": csrf(session_factory)},
                       headers=HTML, follow_redirects=False)

    assert "Unknown+asset+class" in resp.headers["location"]


def test_adding_an_instrument_someone_else_already_added_puts_it_on_your_list(
        client, session_factory, monkeypatch):
    """The catalogue is shared, so "add" often means "it existed; it is now
    yours to track". The answer is the same either way: saying which would tell
    this portfolio what another one had added."""
    from app import main as main_mod

    monkeypatch.setattr(main_mod.pricefeed, "lookup", lambda t, e: {"symbol": None,
                                                                   "name": None,
                                                                   "currency": None,
                                                                   "found": False})
    make_login(client, session_factory)
    with session_factory() as s:
        fac.make_instrument(s, "ACME", name="Acme")
        s.commit()

    resp = client.post("/holdings/add",
                       data={"ticker": "ACME", "exchange": "ASX", "asset_class": "etf",
                             "_csrf": csrf(session_factory)},
                       headers=HTML, follow_redirects=False)

    assert resp.headers["location"] == "/holdings?added=ACME"
    with reading(session_factory) as s:
        assert len(s.scalars(select(HoldingPref)).all()) == 1
    assert "ACME" in client.get("/holdings", headers=HTML).text


def test_the_drp_preference_saves_immediately(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        inst = fac.make_instrument(s, "ACME", name="Acme")
        fac.hold(s, inst)
        s.commit()
        inst_id = inst.id

    client.post(f"/holdings/{inst_id}/pref",
                data={"drp": "on", "note": "reinvesting this one",
                      "_csrf": csrf(session_factory)}, headers=HTML)

    with reading(session_factory) as s:
        pref = s.scalars(select(HoldingPref)).one()
    assert pref.drp is True
    assert pref.note == "reinvesting this one"


def test_clearing_the_drp_preference_persists_too(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        inst = fac.make_instrument(s, "ACME", name="Acme")
        fac.hold(s, inst)
        s.commit()
        inst_id = inst.id
    client.post(f"/holdings/{inst_id}/pref", data={"drp": "on",
                                                      "_csrf": csrf(session_factory)},
                headers=HTML)

    client.post(f"/holdings/{inst_id}/pref", data={"_csrf": csrf(session_factory)},
                headers=HTML)

    with reading(session_factory) as s:
        assert s.scalars(select(HoldingPref)).one().drp is False


def test_the_yahoo_symbol_is_only_changed_when_supplied(client, session_factory):
    """It is catalogue data, shared with everyone else holding the instrument,
    so a form that does not offer it must not change it."""
    make_login(client, session_factory)
    with session_factory() as s:
        inst = fac.make_instrument(s, "ACME", name="Acme", yahoo_symbol="ACME.XA")
        fac.hold(s, inst)
        s.commit()
        inst_id = inst.id

    resp = client.post(f"/holdings/{inst_id}/pref", data={"_csrf": csrf(session_factory)},
                       headers=HTML, follow_redirects=False)

    assert resp.status_code == 303
    with session_factory() as s:
        inst = s.get(Instrument, inst_id)
        assert (inst.yahoo_symbol, inst.name) == ("ACME.XA", "Acme")


def test_a_preference_on_an_unknown_instrument_is_a_404(client, session_factory):
    make_login(client, session_factory)

    assert client.post("/holdings/9999/pref", data={"_csrf": csrf(session_factory)},
                       headers=HTML).status_code == 404


def test_the_instruments_page_lists_what_is_held(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        from app import tenancy
        from test_routes import bind_to_only_portfolio
        bind_to_only_portfolio(s)
        held = fac.make_instrument(s, "ACME", name="Acme Industries")
        fac.hold(s, fac.make_instrument(s, "NOVA", name="Nova Group"))   # added, not traded
        fac.add_trade(s, held, "2026-01-05", "buy", 10, "5.00")
        s.commit()
        assert tenancy is not None

    page = client.get("/holdings", headers=HTML)

    assert "ACME" in page.text and "NOVA" in page.text


# --------------------------------------------------------------------------- #
# Price feed refresh
# --------------------------------------------------------------------------- #

def test_the_refresh_button_kicks_the_feed(client, session_factory, monkeypatch):
    from app import main as main_mod

    ran = []
    monkeypatch.setattr(main_mod, "_run_feed", lambda: ran.append(1) or True)
    make_login(client, session_factory)

    resp = client.post("/refresh", data={"_csrf": csrf(session_factory)},
                       headers=HTML, follow_redirects=False)

    import time
    waited = 0.0
    while not ran and waited < 5:          # fire and forget, on the executor
        time.sleep(0.05)
        waited += 0.05
    assert resp.status_code == 303 and ran == [1]


def test_a_quote_run_already_going_is_not_waited_for(monkeypatch):
    """A second request for live prices while one is fetching returns at
    once rather than queueing behind it."""
    import threading

    from app import main as main_mod

    fetched = []
    monkeypatch.setattr(main_mod.pricefeed, "refresh_quotes",
                        lambda s, settings: fetched.append(1) or 0)
    got = []
    # With a timeout: a run that kept the lock would hang the suite here.
    assert main_mod._quote_lock.acquire(timeout=2), "the premise: no run is going"
    try:
        worker = threading.Thread(target=lambda: got.append(main_mod._run_quotes()),
                                  daemon=True)
        worker.start()
        worker.join(2)
        assert got == [0] and not worker.is_alive() and fetched == []
    finally:
        main_mod._quote_lock.release()


def test_the_log_consoles_first_look_starts_at_the_first_line(client, session_factory,
                                                              monkeypatch):
    """The page asks for "after" only once it has lines; its first request
    gets the recent window, not everything after line one."""
    import collections
    import logging

    from app import logbuffer

    make_login(client, session_factory)
    monkeypatch.setattr(logbuffer, "_lines", collections.deque(maxlen=logbuffer.CAPACITY))
    monkeypatch.setattr(logbuffer, "_next_seq", 0)
    logging.getLogger("app.test").warning("the first line")

    lines = client.get("/admin/logs.json").json()["lines"]

    assert [(line["seq"], line["message"]) for line in lines][:1] == [(1, "the first line")]


def test_a_viewer_cannot_kick_the_feed(client, session_factory):
    with session_factory() as s:
        viewer = fac.make_user(s, "viewer@example.test",
                               password_hash=auth_mod.hash_password(PASSWORD))
        portfolio = fac.make_portfolio(s, "Shared")
        s.add(PortfolioMember(portfolio_id=portfolio.id, user_id=viewer.id, role="viewer"))
        s.commit()
    from test_routes import pre_auth_csrf
    token = pre_auth_csrf(client)
    client.post("/login", data={"email": "viewer@example.test", "password": PASSWORD,
                                "_csrf": token}, headers=HTML)

    assert client.post("/refresh", data={"_csrf": csrf(session_factory)},
                       headers=HTML).status_code == 403


# Role naming: `user.is_admin` and `member.role == "owner"` are different
# powers that both sound like "admin", so the label names the scope —
# decisions.md #34.

def test_the_portfolio_role_reads_as_portfolio_admin(client, session_factory):
    from app.models import ROLE_LABELS

    assert ROLE_LABELS["owner"] == "Portfolio admin"
    assert ROLE_LABELS["viewer"] == "Viewer"


def test_every_stored_role_has_a_label_and_a_blurb():
    """A role with no label renders as blank in the dropdown, which is a role
    nobody can knowingly pick."""
    from app.models import MEMBER_ROLES, ROLE_BLURBS, ROLE_LABELS

    for role in MEMBER_ROLES:
        assert ROLE_LABELS.get(role), f"{role} has no label"
        assert ROLE_BLURBS.get(role), f"{role} has no description"


def test_the_members_page_says_what_each_role_can_do(client, session_factory):
    """The labels alone don't say what a viewer can't do; the line under the
    people table does, for each role."""
    import html
    import re

    from app.models import MEMBER_ROLES, ROLE_BLURBS, ROLE_LABELS

    make_login(client, session_factory)
    page = html.unescape(client.get("/members", headers=HTML).text)

    for role in MEMBER_ROLES:
        said = rf"<strong>{re.escape(ROLE_LABELS[role])}</strong>\s*{re.escape(ROLE_BLURBS[role])}"
        assert re.search(said, page), role


def test_the_two_admin_labels_are_not_the_same_words(client, session_factory):
    """The point of the exercise: if these ever collapse to the same string,
    the distinction is invisible again."""
    from app.models import ROLE_LABELS

    assert ROLE_LABELS["owner"] != "Site admin"
    assert "Portfolio" in ROLE_LABELS["owner"]


def test_the_stored_values_are_untouched():
    """Only the presentation changed. Renaming the values is a data migration
    and rides into the migration squash, where it is free."""
    from app.models import MEMBER_ROLES

    assert MEMBER_ROLES == ("owner", "member", "viewer")


# Every route that sends you back to where you were. Parametrised so one going
# back to a bare `startswith("/")` check fails here rather than passing quietly.
REFERER_ROUTES = [
    pytest.param("/portfolio/switch", "/", id="portfolio-switch"),
    pytest.param("/refresh", "/", id="refresh"),
    pytest.param("/profile/recovery/later", "/", id="recovery-later"),
    pytest.param("/holdings/{instrument_id}/pref", "/holdings", id="instrument-pref"),
]


@pytest.mark.parametrize("path, fallback", REFERER_ROUTES)
def test_a_hostile_referer_cannot_turn_a_redirect_into_an_open_redirect(
        client, session_factory, monkeypatch, path, fallback):
    """`https://host//evil.test` has the path `//evil.test`, and a browser
    follows that off this site. "Starts with a slash" let it through."""
    from app import main as main_mod

    monkeypatch.setattr(main_mod, "_run_feed", lambda: True)
    make_login(client, session_factory)
    with session_factory() as s:
        mine = s.scalars(select(Portfolio).order_by(Portfolio.id)).first().id
        inst = fac.make_instrument(s, "ACME", name="Acme")
        fac.hold(s, inst)
        s.commit()
        inst_id = inst.id

    for referer, expected in (
            ("http://testserver//evil.test/x", fallback),
            (r"http://testserver/\evil.test", fallback),
            ("https://evil.test", fallback),
            ("http://testserver/charts", "/charts")):
        resp = client.post(path.format(instrument_id=inst_id),
                           data={"portfolio_id": str(mine), "_csrf": csrf(session_factory)},
                           headers={**HTML, "referer": referer}, follow_redirects=False)

        assert resp.status_code == 303, (path, referer)
        assert resp.headers["location"] == expected, (path, referer)


# --------------------------------------------------------------------------- #
# What only an instance admin may do, and that what they do takes effect
# --------------------------------------------------------------------------- #
# A mutation run removed `_require_admin` from adding, deactivating and
# resetting accounts and from saving settings, and nothing failed: every test
# of those routes signed in as an admin. It also removed the line that stores
# a new password, from both the change and the reset: the tests checked the
# redirect and the sign-outs, never that the new password works.

def _accounts(session_factory) -> list:
    with session_factory() as s:
        return sorted((u.email, u.is_active, u.is_admin, u.password_hash, u.must_change_password)
                      for u in s.scalars(select(User)))


@pytest.mark.parametrize("method, path, data", [
    ("get", "/users", None),
    ("get", "/admin/accounts", None),
    ("post", "/users/add", {"name": "New", "email": "new@example.test",
                            "password": "a-long-enough-one"}),
    ("post", "/users/{other}/active", {}),
    ("post", "/users/{other}/reset", {"password": "a-long-enough-one"}),
    ("post", "/admin/settings", {"timezone": "UTC"}),
])
def test_a_non_admin_cannot_manage_accounts_or_settings(client, session_factory, tmp_path,
                                                        monkeypatch, method, path, data):
    from app import configfile

    conf = tmp_path / "config.yaml"
    conf.write_text("timezone: Australia/Melbourne\n")
    monkeypatch.setattr(configfile, "CONFIG_FILE", str(conf))
    make_login(client, session_factory, admin=False)
    token = csrf(session_factory)
    with session_factory() as s:
        other_id = fac.make_user(s, "other@example.test").id
        s.commit()
    before = _accounts(session_factory)
    url = path.format(other=other_id)

    resp = (client.get(url, headers=HTML) if method == "get" else
            client.post(url, data={"_csrf": token, **data}, headers=HTML,
                        follow_redirects=False))

    assert resp.status_code == 403
    assert _accounts(session_factory) == before
    assert conf.read_text() == "timezone: Australia/Melbourne\n"


def _signs_in(client, email: str, password: str) -> bool:
    from test_routes import pre_auth_csrf

    resp = client.post("/login", data={"email": email, "password": password,
                                       "_csrf": pre_auth_csrf(client)},
                       headers=HTML, follow_redirects=False)
    return resp.status_code == 303 and "error" not in resp.headers.get("location", "")


def test_a_changed_password_is_the_one_that_works(client, session_factory):
    from fastapi.testclient import TestClient

    make_login(client, session_factory)
    client.post("/profile/password",
                data={"current_password": PASSWORD, "password": "a-longer-new-one",
                      "confirm": "a-longer-new-one", "_csrf": csrf(session_factory)},
                headers=HTML, follow_redirects=False)

    fresh = TestClient(client.app)
    assert not _signs_in(fresh, "user@example.test", PASSWORD)
    assert _signs_in(fresh, "user@example.test", "a-longer-new-one")


def test_a_reset_password_is_the_one_that_works(client, session_factory):
    from fastapi.testclient import TestClient

    make_login(client, session_factory, admin=True)
    token = csrf(session_factory)
    with session_factory() as s:
        other_id = fac.make_user(s, "other@example.test",
                                 password_hash=auth_mod.hash_password(PASSWORD)).id
        s.commit()

    client.post(f"/users/{other_id}/reset",
                data={"password": "a-fresh-temporary-one", "_csrf": token}, headers=HTML)

    fresh = TestClient(client.app)
    assert not _signs_in(fresh, "other@example.test", PASSWORD)
    assert _signs_in(fresh, "other@example.test", "a-fresh-temporary-one")


def test_a_new_account_has_recovery_codes_waiting(client, session_factory):
    from app import twofactor

    make_login(client, session_factory, admin=True)
    client.post("/users/add", data={"name": "New", "email": "new@example.test",
                                    "password": "a-long-enough-one",
                                    "_csrf": csrf(session_factory)}, headers=HTML)

    with session_factory() as s:
        new = s.scalar(select(User).where(User.email == "new@example.test"))
        assert twofactor.has_recovery_codes(s, new)


# --------------------------------------------------------------------------- #
# What only an owner may do in a portfolio, and what each page reaches
# --------------------------------------------------------------------------- #
# Removing the owner check from adding members, changing a role and
# withdrawing an invitation failed nothing, nor did removing the admin check
# from the server log and from portfolio settings: no test signed in as a
# plain member. Nor did dropping the portfolio filter from the members list,
# from the last-owner count, or from a new API key's portfolios: no test had a
# second portfolio for them to reach.

def _as_plain_member(client, session_factory) -> dict:
    """Signed in as a member (not an owner, not an admin) of a portfolio owned
    by someone else, with another member and a live invitation in it."""
    from app import invites

    make_login(client, session_factory, admin=False)
    with session_factory() as s:
        me = s.scalar(select(User).where(User.email == "user@example.test"))
        boss = fac.make_user(s, "boss@example.test")
        shared = fac.make_portfolio(s, "Shared", owner=boss)
        mine = fac.add_member(s, shared, me, role="member")
        other = fac.add_member(s, shared, fac.make_user(s, "other@example.test"), role="viewer")
        outsider = fac.make_user(s, "outsider@example.test")
        invite_raw = invites.create(s, portfolio_id=shared.id, role="member",
                                    created_by=boss.id)
        invite_id = s.scalar(select(PortfolioInvite.id))
        s.commit()
        ids = {"shared": shared.id, "mine": mine.id, "other": other.id,
               "outsider": outsider.id, "invite": invite_id, "raw": invite_raw}
    client.post("/portfolio/switch", data={"portfolio_id": str(ids["shared"]),
                                           "_csrf": csrf(session_factory)}, headers=HTML)
    return ids


def _memberships(session_factory) -> list:
    with session_factory() as s:
        return sorted((m.portfolio_id, m.user_id, m.role) for m in s.scalars(select(PortfolioMember)))


@pytest.mark.parametrize("path, data", [
    ("/members/add", {"user_id": "{outsider}", "role": "owner"}),
    ("/members/{mine}/role", {"role": "owner"}),
    ("/members/{other}/role", {"role": "owner"}),
    ("/members/{other}/remove", {}),
    ("/members/invite/{invite}/revoke", {}),
])
def test_a_plain_member_cannot_manage_the_members(client, session_factory, path, data):
    ids = _as_plain_member(client, session_factory)
    before = _memberships(session_factory)

    resp = client.post(path.format(**ids), headers=HTML, follow_redirects=False,
                       data={"_csrf": csrf(session_factory),
                             **{k: v.format(**ids) for k, v in data.items()}})

    assert resp.status_code == 403
    assert _memberships(session_factory) == before
    with session_factory() as s:
        assert s.get(PortfolioInvite, ids["invite"]).revoked_at is None


def test_the_server_log_and_portfolio_settings_are_for_admins(client, session_factory):
    ids = _as_plain_member(client, session_factory)

    assert client.get("/admin/logs.json").status_code == 403
    resp = client.post("/portfolio/settings", headers=HTML, follow_redirects=False,
                       data={"name": "Renamed", "_csrf": csrf(session_factory)})
    assert resp.status_code == 403
    with session_factory() as s:
        assert s.get(Portfolio, ids["shared"]).name == "Shared"


def test_the_members_page_lists_this_portfolios_people(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        stranger = fac.make_user(s, "stranger@example.test", name="Stranger Danger")
        fac.make_portfolio(s, "Theirs", owner=stranger)
        s.commit()

    page = client.get("/members", headers=HTML).text
    # The People table, not the first table on the page (the brokerage fees),
    # nor the add-someone picker, which lists every account on purpose.
    people = page.split("<h2>People</h2>", 1)[1].split("</table>", 1)[0]

    assert "user@example.test" in people, "the premise: this is the members table"
    assert "stranger@example.test" not in people


def test_the_last_owner_is_counted_in_this_portfolio_only(client, session_factory):
    """My portfolio has one owner, me, and a second member; another portfolio
    has its own owner. I cannot leave mine ownerless."""
    make_login(client, session_factory)
    with session_factory() as s:
        mine = s.scalar(select(PortfolioMember).where(PortfolioMember.role == "owner"))
        friend = fac.make_user(s, "friend@example.test")
        fac.add_member(s, s.get(Portfolio, mine.portfolio_id), friend, role="member")
        fac.make_portfolio(s, "Theirs", owner=fac.make_user(s, "stranger@example.test"))
        s.commit()
        my_membership = mine.id

    resp = client.post(f"/members/{my_membership}/remove", headers=HTML,
                       follow_redirects=False, data={"_csrf": csrf(session_factory)})

    assert "error=A+portfolio+needs+an+owner" in resp.headers["location"]
    with session_factory() as s:
        assert s.get(PortfolioMember, my_membership) is not None


def test_a_new_key_reaches_only_the_portfolios_chosen(client, session_factory):
    from app.models import ApiKey

    make_login(client, session_factory)
    with session_factory() as s:
        me = s.scalar(select(User))
        mine = s.scalar(select(Portfolio))
        second = fac.make_portfolio(s, "Second", owner=me)
        fac.make_portfolio(s, "Theirs", owner=fac.make_user(s, "stranger@example.test"))
        s.commit()
        chosen = mine.id

    client.post("/keys/new", headers=HTML, data={
        "name": "one portfolio", "scopes": "read", "portfolios": [str(chosen)],
        "_csrf": csrf(session_factory)})

    with session_factory() as s:
        key = s.scalar(select(ApiKey))
        assert [p.id for p in key.portfolios] == [chosen]
        assert second.id != chosen

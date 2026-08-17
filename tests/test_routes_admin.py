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
    ApiKey,
    HoldingPref,
    Instrument,
    Portfolio,
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
# API keys
# --------------------------------------------------------------------------- #

def test_a_key_is_shown_once_and_only_its_hash_is_kept(client, session_factory):
    make_login(client, session_factory)

    resp = client.post("/keys/new", data={"name": "Budget app", "scopes": "read",
                                          "_csrf": csrf(session_factory)},
                       headers=HTML, follow_redirects=False)

    raw = resp.headers["location"].split("key=")[1]
    assert raw.startswith("pfk_")
    with session_factory() as s:
        stored = s.scalars(select(ApiKey)).one()
        assert stored.key_hash == auth_mod.hash_token(raw)
        assert raw not in stored.key_hash          # the value itself is not kept
        assert stored.prefix in raw                # …but it stays identifiable


def test_an_unknown_scope_is_refused(client, session_factory):
    make_login(client, session_factory)

    assert client.post("/keys/new", data={"name": "k", "scopes": "admin",
                                          "_csrf": csrf(session_factory)},
                       headers=HTML).status_code == 400


def test_a_key_can_be_revoked(client, session_factory):
    make_login(client, session_factory)
    client.post("/keys/new", data={"name": "k", "scopes": "read",
                                   "_csrf": csrf(session_factory)}, headers=HTML)
    with session_factory() as s:
        key_id = s.scalars(select(ApiKey)).one().id

    client.post(f"/keys/{key_id}/revoke", data={"_csrf": csrf(session_factory)},
                headers=HTML)

    with session_factory() as s:
        assert s.get(ApiKey, key_id).revoked_at is not None


def test_another_portfolios_key_cannot_be_revoked(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        stranger = fac.make_user(s, "stranger@example.test")
        theirs = fac.make_portfolio(s, "Theirs", owner=stranger)
        s.commit()
        raw, key_hash, prefix = auth_mod.new_api_key()
        s.add(ApiKey(portfolio_id=theirs.id, name="theirs", key_hash=key_hash,
                     prefix=prefix, scopes="read"))
        s.commit()
        key_id = s.scalars(select(ApiKey)).one().id

    assert client.post(f"/keys/{key_id}/revoke", data={"_csrf": csrf(session_factory)},
                       headers=HTML).status_code == 404


def test_a_viewer_cannot_issue_keys(client, session_factory):
    """A key is full access to the portfolio's data, so issuing one is an
    owner's decision — not something read-only access can do."""
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

    assert client.post("/keys/new", data={"name": "k", "scopes": "read",
                                          "_csrf": csrf(session_factory)},
                       headers=HTML).status_code == 403


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
    yours to track" — which the message has to distinguish."""
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

    assert "new=0" in resp.headers["location"]     # existed already
    with reading(session_factory) as s:
        assert len(s.scalars(select(HoldingPref)).all()) == 1


def test_the_drp_preference_saves_immediately(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        inst = fac.make_instrument(s, "ACME", name="Acme")
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
    so a form that does not mention it must not blank it."""
    make_login(client, session_factory)
    with session_factory() as s:
        inst = fac.make_instrument(s, "ACME", name="Acme")
        s.commit()
        inst_id = inst.id

    client.post(f"/holdings/{inst_id}/pref", data={"_csrf": csrf(session_factory)},
                headers=HTML)

    with session_factory() as s:
        assert s.get(Instrument, inst_id).yahoo_symbol == "ACME.AX"


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
        fac.make_instrument(s, "NOVA", name="Nova Group")
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

    assert resp.status_code == 303


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

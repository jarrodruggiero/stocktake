"""The second factor, and the ways back in when it goes wrong.

A second factor is only worth having if it cannot be walked around, so most of
these tests are about the walk-arounds:

  * a password alone must not reach a single page — the half-authenticated
    session is refused at `load_session`, the one place every route resolves a
    cookie, rather than by each route remembering to check;
  * the code stage must be inside the lockout, or the factor is a six-digit
    secret that anyone holding the password can brute-force at leisure;
  * enrolment must be confirmed with a real code, or people switch it on with a
    secret they never scanned and lock themselves out;
  * every recovery path (recovery codes, an admin, the CLI) must actually work,
    because the failure mode is "you can never see your own portfolio again".

TOTP codes are generated with pyotp here rather than stubbed: the point is that
a real authenticator's output is accepted, and a stub would only prove the code
calls itself.
"""

from __future__ import annotations

import datetime as dt
import re

import pyotp
import pytest
from sqlalchemy import select

import factories as fac
from app import auth as auth_mod
from app import twofactor
from app.models import RecoveryCode, User, UserSession
from test_routes import PASSWORD, make_login, pre_auth_csrf, session_csrf

HTML = {"accept": "text/html"}


def code_for(secret: str) -> str:
    return pyotp.TOTP(secret).now()


@pytest.fixture
def enrolled(client, session_factory):
    """A signed-in user with 2FA already on. Returns their secret."""
    make_login(client, session_factory)
    secret = pyotp.random_base32()
    with session_factory() as s:
        user = s.scalars(select(User)).one()
        twofactor.enable(s, user, secret)
        s.commit()
    return secret


# --------------------------------------------------------------------------- #
# The primitives
# --------------------------------------------------------------------------- #

def test_a_current_code_verifies(pf):
    secret = twofactor.new_secret()

    assert twofactor.verify_code(secret, code_for(secret))


def test_a_wrong_code_does_not(pf):
    secret = twofactor.new_secret()

    assert not twofactor.verify_code(secret, "000000")


@pytest.mark.parametrize("code", ["", "   ", "abcdef", "12345", None])
def test_junk_is_rejected_without_reaching_the_library(code):
    """Empty and non-numeric input is refused here rather than letting pyotp
    decide what an empty code means."""
    assert not twofactor.verify_code("JBSWY3DPEHPK3PXP", code)


def test_a_code_with_spaces_in_it_still_works():
    """People paste "123 456" out of their phone."""
    secret = twofactor.new_secret()
    spaced = code_for(secret)

    assert twofactor.verify_code(secret, f"{spaced[:3]} {spaced[3:]}")


def test_no_secret_means_no_code_is_ever_valid():
    """A user who never enrolled must not be verifiable by anything."""
    assert not twofactor.verify_code(None, "000000")
    assert not twofactor.verify_code("", "000000")


def test_one_step_of_clock_drift_is_tolerated():
    """Phones drift. A 30-second window either side is the standard trade-off:
    stricter generates support requests, looser widens replay for no gain."""
    secret = twofactor.new_secret()
    totp = pyotp.TOTP(secret)
    previous = totp.at(dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=30))

    assert twofactor.verify_code(secret, previous)


def test_a_code_two_steps_old_is_refused():
    secret = twofactor.new_secret()
    totp = pyotp.TOTP(secret)
    stale = totp.at(dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=90))

    assert not twofactor.verify_code(secret, stale)


def test_the_provisioning_uri_names_this_install(pf):
    secret = twofactor.new_secret()

    uri = twofactor.provisioning_uri(secret, "someone@example.test", "portfolio")

    assert uri.startswith("otpauth://totp/")
    assert "portfolio" in uri
    assert secret in uri


def test_the_qr_is_rendered_locally_as_inline_svg(pf):
    """The obvious shortcut is a chart URL, which would post the TOTP secret to
    a third party in a query string."""
    svg = twofactor.qr_svg("otpauth://totp/x?secret=JBSWY3DPEHPK3PXP")

    assert svg.lstrip().startswith("<?xml") or svg.lstrip().startswith("<svg")
    assert "<svg" in svg
    assert "http://" not in svg.replace("http://www.w3.org", "")  # no remote refs


# --------------------------------------------------------------------------- #
# Recovery codes
# --------------------------------------------------------------------------- #

def test_a_set_of_recovery_codes_is_issued_and_stored_hashed(pf):
    user = fac.make_user(pf, "a@example.test")

    codes = twofactor.generate_recovery_codes(pf, user)

    assert len(codes) == twofactor.RECOVERY_CODE_COUNT
    stored = pf.scalars(select(RecoveryCode.code_hash)).all()
    # The raw codes must not be recoverable from the database.
    for raw in codes:
        assert raw not in stored
        assert twofactor.hash_code(raw) in stored


def test_a_recovery_code_works_once(pf):
    user = fac.make_user(pf, "a@example.test")
    codes = twofactor.generate_recovery_codes(pf, user)

    assert twofactor.consume_recovery_code(pf, user, codes[0])
    assert not twofactor.consume_recovery_code(pf, user, codes[0])


def test_a_spent_code_is_marked_not_deleted(pf):
    """So "3 of 8 left" is answerable, and a replay is distinguishable from a
    code that was never issued."""
    user = fac.make_user(pf, "a@example.test")
    codes = twofactor.generate_recovery_codes(pf, user)

    twofactor.consume_recovery_code(pf, user, codes[0])

    assert len(pf.scalars(select(RecoveryCode)).all()) == twofactor.RECOVERY_CODE_COUNT
    assert twofactor.remaining_recovery_codes(pf, user) == twofactor.RECOVERY_CODE_COUNT - 1


def test_a_recovery_code_is_accepted_however_it_was_typed(pf):
    user = fac.make_user(pf, "a@example.test")
    codes = twofactor.generate_recovery_codes(pf, user)
    messy = f"  {codes[0].lower().replace('-', ' ')}  "

    assert twofactor.consume_recovery_code(pf, user, messy)


def test_one_users_recovery_code_is_useless_to_another(pf):
    mine = fac.make_user(pf, "mine@example.test")
    theirs = fac.make_user(pf, "theirs@example.test")
    codes = twofactor.generate_recovery_codes(pf, mine)
    twofactor.generate_recovery_codes(pf, theirs)

    assert not twofactor.consume_recovery_code(pf, theirs, codes[0])


def test_reissuing_replaces_the_old_set(pf):
    """Otherwise a code someone thought they had destroyed keeps working."""
    user = fac.make_user(pf, "a@example.test")
    first = twofactor.generate_recovery_codes(pf, user)

    twofactor.generate_recovery_codes(pf, user)

    assert not twofactor.consume_recovery_code(pf, user, first[0])


def test_disabling_keeps_the_recovery_codes(pf):
    """**Reversed deliberately** — see `twofactor.disable`.

    Destroying them was right while they were a 2FA-only fallback: a later
    re-enrolment must not silently inherit escape hatches the user believes are
    gone. They are now the ACCOUNT's recovery, standing in for a forgotten
    password as well, so they have to outlive any single factor. Taking them
    here would leave somebody who merely switched authenticator with no
    recovery at all — and no indication of it.
    """
    user = fac.make_user(pf, "a@example.test")
    codes = twofactor.enable(pf, user, twofactor.new_secret())
    assert codes

    twofactor.disable(pf, user)

    assert not twofactor.is_enabled(user)
    assert user.totp_secret is None
    assert len(pf.scalars(select(RecoveryCode)).all()) == twofactor.RECOVERY_CODE_COUNT


def test_a_secret_without_being_switched_on_is_not_enabled(pf):
    """A half-finished enrolment — scanned the QR, closed the tab — must not
    lock anyone out."""
    user = fac.make_user(pf, "a@example.test")
    user.totp_secret = twofactor.new_secret()

    assert not twofactor.is_enabled(user)


# --------------------------------------------------------------------------- #
# Logging in
# --------------------------------------------------------------------------- #

def login_password_only(client, email="user@example.test"):
    token = pre_auth_csrf(client)
    return client.post("/login", data={"email": email, "password": PASSWORD, "_csrf": token},
                       headers=HTML, follow_redirects=False)


def test_a_password_alone_lands_on_the_code_page(client, session_factory, enrolled):
    client.cookies.clear()

    resp = login_password_only(client)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login/code"


def test_a_password_alone_reaches_no_page_at_all(client, session_factory, enrolled):
    """The whole point. The cookie exists and is valid, but `load_session`
    refuses anything still awaiting a code — so the dashboard bounces to login
    exactly as if there were no session."""
    client.cookies.clear()
    login_password_only(client)

    resp = client.get("/", headers=HTML, follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_the_right_code_completes_the_login(client, session_factory, enrolled):
    client.cookies.clear()
    login_password_only(client)

    resp = client.post("/login/code", data={"code": code_for(enrolled),
                                            "_csrf": pre_auth_csrf(client)},
                       headers=HTML, follow_redirects=False)

    assert resp.headers["location"] == "/"
    assert client.get("/", headers=HTML).status_code == 200


def test_a_wrong_code_does_not_complete_it(client, session_factory, enrolled):
    client.cookies.clear()
    login_password_only(client)

    resp = client.post("/login/code", data={"code": "000000",
                                            "_csrf": pre_auth_csrf(client)},
                       headers=HTML)

    assert "isn&#39;t right" in resp.text or "isn't right" in resp.text
    assert client.get("/", headers=HTML, follow_redirects=False).status_code == 303


def test_a_recovery_code_completes_the_login_and_is_spent(client, session_factory, enrolled):
    with session_factory() as s:
        user = s.scalars(select(User)).one()
        codes = twofactor.generate_recovery_codes(s, user)
        s.commit()
    client.cookies.clear()
    login_password_only(client)

    resp = client.post("/login/code", data={"code": codes[0],
                                            "_csrf": pre_auth_csrf(client)},
                       headers=HTML, follow_redirects=False)

    assert resp.headers["location"] == "/"
    with session_factory() as s:
        user = s.scalars(select(User)).one()
        assert twofactor.remaining_recovery_codes(s, user) == len(codes) - 1


def test_the_code_page_is_not_reachable_without_giving_a_password_first(client, session_factory):
    make_login(client, session_factory)
    client.cookies.clear()

    resp = client.get("/login/code", headers=HTML, follow_redirects=False)

    assert resp.headers["location"] == "/login"


def test_a_finished_session_cannot_go_back_to_the_code_page(client, session_factory, enrolled):
    """`load_pending_session` refuses a fully-authenticated session, so the
    code step can't be used to sidestep anything later."""
    client.cookies.clear()
    login_password_only(client)
    client.post("/login/code", data={"code": code_for(enrolled),
                                     "_csrf": pre_auth_csrf(client)}, headers=HTML)

    resp = client.get("/login/code", headers=HTML, follow_redirects=False)

    assert resp.headers["location"] == "/login"


def test_guessing_codes_is_covered_by_the_lockout(client, session_factory, enrolled):
    """Without this the second factor is a six-digit secret that anyone holding
    the password can brute-force at leisure."""
    client.cookies.clear()
    login_password_only(client)

    for _ in range(9):  # the configured max_attempts is 8
        client.post("/login/code", data={"code": "000000",
                                         "_csrf": pre_auth_csrf(client)}, headers=HTML)

    resp = client.post("/login/code", data={"code": code_for(enrolled),
                                            "_csrf": pre_auth_csrf(client)}, headers=HTML)

    # Even the RIGHT code is refused once locked out.
    assert "Too many attempts" in resp.text


def test_a_user_without_2fa_logs_in_as_before(client, session_factory):
    """The feature is opt-in; nothing changes for anyone who hasn't enabled it."""
    make_login(client, session_factory)
    client.cookies.clear()

    resp = login_password_only(client)

    assert resp.headers["location"] == "/"


# --------------------------------------------------------------------------- #
# Enrolling and turning it off
# --------------------------------------------------------------------------- #

def test_the_setup_page_offers_a_secret_and_a_qr(client, session_factory):
    make_login(client, session_factory)

    page = client.get("/profile/2fa", headers=HTML).text

    assert "<svg" in page
    assert 'name="secret"' in page


def test_enabling_needs_a_code_generated_from_that_secret(client, session_factory):
    """Skipping this check is how people switch 2FA on with a secret they never
    successfully scanned, and lock themselves out of their own portfolio."""
    make_login(client, session_factory)
    secret = pyotp.random_base32()

    resp = client.post("/profile/2fa/enable",
                       data={"secret": secret, "code": "000000",
                             "_csrf": session_csrf(session_factory)},
                       headers=HTML, follow_redirects=False)

    assert "error=" in resp.headers["location"]
    with session_factory() as s:
        assert not twofactor.is_enabled(s.scalars(select(User)).one())


def test_enabling_with_a_real_code_turns_it_on_and_shows_the_codes_once(client, session_factory):
    make_login(client, session_factory)
    secret = pyotp.random_base32()

    resp = client.post("/profile/2fa/enable",
                       data={"secret": secret, "code": code_for(secret),
                             "_csrf": session_csrf(session_factory)},
                       headers=HTML)

    assert "only time these are shown" in resp.text
    with session_factory() as s:
        user = s.scalars(select(User)).one()
        assert twofactor.is_enabled(user)
        assert twofactor.remaining_recovery_codes(s, user) == twofactor.RECOVERY_CODE_COUNT
    # And they are not shown again on a later visit.
    assert "only time these are shown" not in client.get("/profile", headers=HTML).text


def test_an_abandoned_enrolment_leaves_nothing_behind(client, session_factory):
    """The unconfirmed secret rides in the form, never the user row."""
    make_login(client, session_factory)

    client.get("/profile/2fa", headers=HTML)

    with session_factory() as s:
        user = s.scalars(select(User)).one()
        assert user.totp_secret is None


def test_turning_it_off_needs_both_the_password_and_a_code(client, session_factory, enrolled):
    for data in (
        {"password": "wrong-password-entirely", "code": code_for(enrolled)},
        {"password": PASSWORD, "code": "000000"},
    ):
        resp = client.post("/profile/2fa/disable",
                           data={**data, "_csrf": session_csrf(session_factory)},
                           headers=HTML, follow_redirects=False)
        assert "error=" in resp.headers["location"]

    with session_factory() as s:
        assert twofactor.is_enabled(s.scalars(select(User)).one())


def test_turning_it_off_with_both_works(client, session_factory, enrolled):
    resp = client.post("/profile/2fa/disable",
                       data={"password": PASSWORD, "code": code_for(enrolled),
                             "_csrf": session_csrf(session_factory)},
                       headers=HTML, follow_redirects=False)

    assert resp.headers["location"] == "/profile?saved=2fa-off"
    with session_factory() as s:
        assert not twofactor.is_enabled(s.scalars(select(User)).one())


def test_a_recovery_code_also_turns_it_off(client, session_factory, enrolled):
    """Someone whose phone is gone still needs to be able to switch it off."""
    with session_factory() as s:
        codes = twofactor.generate_recovery_codes(s, s.scalars(select(User)).one())
        s.commit()

    resp = client.post("/profile/2fa/disable",
                       data={"password": PASSWORD, "code": codes[0],
                             "_csrf": session_csrf(session_factory)},
                       headers=HTML, follow_redirects=False)

    assert resp.headers["location"] == "/profile?saved=2fa-off"


# --------------------------------------------------------------------------- #
# Recovery: admin, and the command line
# --------------------------------------------------------------------------- #

def test_an_admin_can_clear_someone_elses_2fa(client, session_factory):
    make_login(client, session_factory, admin=True)
    with session_factory() as s:
        other = fac.make_user(s, "other@example.test",
                              password_hash=auth_mod.hash_password(PASSWORD))
        twofactor.enable(s, other, twofactor.new_secret())
        s.commit()
        other_id = other.id
    token = session_csrf(session_factory)

    resp = client.post(f"/users/{other_id}/2fa/clear", data={"_csrf": token},
                       headers=HTML, follow_redirects=False)

    assert resp.headers["location"] == "/users?reset=2fa"
    with session_factory() as s:
        assert not twofactor.is_enabled(s.get(User, other_id))
        # Their sessions went too — the account's security just changed.
        assert s.scalars(
            select(UserSession).where(UserSession.user_id == other_id)
        ).all() == []


def test_a_non_admin_cannot_clear_anyone_elses_2fa(client, session_factory):
    make_login(client, session_factory, admin=False)
    with session_factory() as s:
        other = fac.make_user(s, "other@example.test")
        twofactor.enable(s, other, twofactor.new_secret())
        s.commit()
        other_id = other.id

    resp = client.post(f"/users/{other_id}/2fa/clear",
                       data={"_csrf": session_csrf(session_factory)}, headers=HTML)

    assert resp.status_code == 403
    with session_factory() as s:
        assert twofactor.is_enabled(s.get(User, other_id))


def _methods_table(client) -> str:
    page = client.get("/profile", headers=HTML).text
    return re.search(r'<table class="methods">.*?</table>', page, re.S).group(0)


def test_the_account_page_lists_every_way_in_and_whether_it_is_on(client, session_factory):
    """One place that answers "how can this account be signed into", rather
    than a status somebody has to infer from which forms are on the page.

    The status is a word, not only a colour: read as a hue alone it is not a
    status at all for anybody who cannot separate the two.
    """
    make_login(client, session_factory)
    methods = _methods_table(client)
    assert "Password" in methods and "Two-factor authentication" in methods
    assert ">Off<" in methods and "/profile/2fa" in methods

    with session_factory() as s:
        twofactor.enable(s, s.scalars(select(User)).one(), pyotp.random_base32())
        s.commit()
    methods = _methods_table(client)
    assert ">On<" in methods and ">Off<" not in methods

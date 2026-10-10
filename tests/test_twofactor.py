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
from freezegun import freeze_time
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

    assert twofactor.matching_step(secret, code_for(secret)) is not None


def test_a_wrong_code_does_not(pf):
    secret = twofactor.new_secret()

    assert twofactor.matching_step(secret, "000000") is None


@pytest.mark.parametrize("code", ["", "   ", "abcdef", "12345", None])
def test_junk_is_rejected_without_reaching_the_library(code):
    """Empty and non-numeric input is refused here rather than letting pyotp
    decide what an empty code means."""
    assert twofactor.matching_step("JBSWY3DPEHPK3PXP", code) is None


def test_a_code_with_spaces_in_it_still_works():
    """People paste "123 456" out of their phone."""
    secret = twofactor.new_secret()
    spaced = code_for(secret)

    assert twofactor.matching_step(secret, f"{spaced[:3]} {spaced[3:]}") is not None


def test_no_secret_means_no_code_is_ever_valid():
    """A user who never enrolled must not be verifiable by anything."""
    assert twofactor.matching_step(None, "000000") is None
    assert twofactor.matching_step("", "000000") is None


@pytest.mark.parametrize("offset, accepted", [(-2, False), (-1, True), (0, True), (1, True),
                                              (2, False)])
def test_a_code_is_good_for_its_own_step_and_one_either_side(offset, accepted):
    """Phones drift either way. A 30-second window either side is the standard
    trade-off: stricter generates support requests, looser widens replay for
    no gain. Built from step numbers rather than from "now minus 90 seconds",
    which is three steps back, not two."""
    secret = twofactor.new_secret()
    totp = pyotp.TOTP(secret)
    with freeze_time("2026-08-02 12:00:10"):  # one clock for both sides
        now = totp.timecode(dt.datetime.now(dt.timezone.utc))
        step = twofactor.matching_step(secret, totp.generate_otp(now + offset))

    assert (step == now + offset) if accepted else step is None


# --------------------------------------------------------------------------- #
# Replay — GHSA-28h6-x798-2qv5
#
# A code is valid for about 90 seconds, so without a record of what was used,
# anyone who saw one go by could use it again in that time. Frozen clocks
# throughout: these are about which STEP a code belongs to, and a test that
# straddled a 30-second boundary would be testing the boundary instead.
# --------------------------------------------------------------------------- #

NOW = "2026-10-03 01:00:10"


def frozen_code(secret: str) -> str:
    """The current code under a frozen clock. Not `code_for`: pyotp's `now()`
    reads a naive datetime and converts it as LOCAL time, and freezegun's
    naive now is UTC — so off a UTC machine it is a code for the wrong step."""
    return pyotp.TOTP(secret).at(dt.datetime.now(dt.timezone.utc))


def _user_with_secret(pf):
    user = fac.make_user(pf, "replay@example.test")
    user.totp_secret = twofactor.new_secret()
    pf.flush()
    return user


def test_a_code_is_accepted_once(pf):
    user = _user_with_secret(pf)
    with freeze_time(NOW):
        code = frozen_code(user.totp_secret)

        assert twofactor.accept_code(pf, user, code)
        assert not twofactor.accept_code(pf, user, code), "the same code was accepted twice"


def test_an_earlier_code_is_refused_once_a_later_one_was_used(pf):
    """The previous step's code is still inside the drift window, but anyone
    holding it saw it before the one just used."""
    user = _user_with_secret(pf)
    totp = pyotp.TOTP(user.totp_secret)
    with freeze_time(NOW):
        now = dt.datetime.now(dt.timezone.utc)
        assert twofactor.accept_code(pf, user, totp.at(now))

        assert not twofactor.accept_code(pf, user, totp.at(now - dt.timedelta(seconds=30)))


def test_the_next_code_is_still_accepted(pf):
    """Refusing replays must not refuse the person: the code after the one they
    just used works."""
    user = _user_with_secret(pf)
    totp = pyotp.TOTP(user.totp_secret)
    with freeze_time(NOW):
        now = dt.datetime.now(dt.timezone.utc)
        assert twofactor.accept_code(pf, user, totp.at(now - dt.timedelta(seconds=30)))

        assert twofactor.accept_code(pf, user, totp.at(now))


def test_two_requests_racing_with_one_code_cannot_both_pass(session_factory):
    """The relay case: the same code arrives twice before either request has
    committed. Each loaded the user before the other wrote, so a read-then-write
    check would pass both. The UPDATE's own condition is what refuses one."""
    with session_factory() as s:
        user = _user_with_secret(s)
        s.commit()
        user_id = user.id
    with freeze_time(NOW), session_factory() as first, session_factory() as second:
        mine = first.get(User, user_id)
        theirs = second.get(User, user_id)
        code = frozen_code(mine.totp_secret)

        assert twofactor.accept_code(first, mine, code)
        first.commit()
        assert not twofactor.accept_code(second, theirs, code), "both racers got in"


def test_a_new_secret_forgets_the_old_ones_steps(pf):
    """A fresh enrolment's first code can share a step with the last code from
    the old secret, and must not be refused for it."""
    user = _user_with_secret(pf)
    with freeze_time(NOW):
        assert twofactor.accept_code(pf, user, frozen_code(user.totp_secret))
        user.totp_enabled_at = dt.datetime.now(dt.timezone.utc)

        twofactor.disable(pf, user)
        assert user.totp_last_step is None
        twofactor.begin_enrolment(user)

        assert twofactor.accept_code(pf, user, frozen_code(user.totp_secret))


def test_issuing_a_secret_starts_with_no_step_recorded(pf):
    """Wherever the secret is replaced, the record goes with it — not only on
    the disable path that happens to come first today."""
    user = _user_with_secret(pf)
    user.totp_enabled_at = dt.datetime.now(dt.timezone.utc)
    user.totp_last_step = 99_999_999
    old = user.totp_secret

    twofactor.begin_enrolment(user)

    assert user.totp_secret != old
    assert user.totp_last_step is None


def test_a_code_used_to_sign_in_cannot_sign_in_again(client, session_factory, enrolled):
    """End to end, through the login form: somebody who watched the code go in
    and also has the password still does not get a session out of it."""
    code = code_for(enrolled)
    client.cookies.clear()
    login_password_only(client)
    first = client.post("/login/code", data={"code": code, "_csrf": pre_auth_csrf(client)},
                        headers=HTML, follow_redirects=False)
    assert first.headers["location"] == "/"

    client.cookies.clear()
    login_password_only(client)
    again = client.post("/login/code", data={"code": code, "_csrf": pre_auth_csrf(client)},
                        headers=HTML, follow_redirects=False)

    assert again.status_code == 200
    assert "isn&#39;t right" in again.text or "isn't right" in again.text
    assert client.get("/", headers=HTML, follow_redirects=False).status_code == 303


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


def test_new_codes_for_one_account_leave_anothers_alone(pf):
    """Issuing a set clears the old one, and only that account's: one person
    asking for new codes must not take everybody else's way back in."""
    mine = fac.make_user(pf, "mine@example.test")
    theirs = fac.make_user(pf, "theirs@example.test")
    their_codes = twofactor.generate_recovery_codes(pf, theirs)

    twofactor.generate_recovery_codes(pf, mine)

    assert twofactor.consume_recovery_code(pf, theirs, their_codes[0])


def test_whether_an_account_has_codes_is_its_own(pf):
    mine = fac.make_user(pf, "mine@example.test")
    theirs = fac.make_user(pf, "theirs@example.test")
    twofactor.generate_recovery_codes(pf, theirs)

    assert twofactor.has_recovery_codes(pf, mine) is False
    assert len(twofactor.ensure_recovery_codes(pf, mine)) == twofactor.RECOVERY_CODE_COUNT


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


def test_code_guesses_from_many_addresses_still_lock_the_code_step(
        client, session_factory, enrolled):
    """GHSA-gqj4-vm54-mp3j. The guesses arrive from a different address each
    — rotating machines, or a forged X-Forwarded-For — and the right code is
    still refused once the account has had its allowance."""
    from app.models import LoginAttempt

    client.cookies.clear()
    login_password_only(client)
    with session_factory() as s:
        email = s.scalars(select(User)).one().email
        for n in range(8):  # the configured max_attempts
            s.add(LoginAttempt(email=email, ip=f"192.0.2.{n + 1}", success=False,
                               created_at=dt.datetime.now(dt.timezone.utc)))
        s.commit()

    resp = client.post("/login/code", data={"code": code_for(enrolled),
                                            "_csrf": pre_auth_csrf(client)}, headers=HTML)

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
    # The key, for somebody who cannot scan. The secret is NOT a form field any
    # more: it is held on the row, and a posted one would let a caller enrol an
    # authenticator this account never scanned.
    assert "<code>" in page
    assert 'name="secret"' not in page


def test_a_secret_posted_with_the_form_is_ignored(client, session_factory):
    """Only the secret this account was issued counts.

    While it rode in the form, anything posted alongside a matching code turned
    2FA on — so a caller could enrol an authenticator this account had never
    scanned, and the real owner would be locked behind somebody else's phone.
    Holding it on the row closed that; this is what keeps it closed.
    """
    make_login(client, session_factory)
    client.get("/profile/2fa", headers=HTML)          # issues the real one
    theirs = pyotp.random_base32()

    client.post("/profile/2fa/enable", headers=HTML,
                data={"secret": theirs, "code": code_for(theirs),
                      "_csrf": session_csrf(session_factory)})

    with session_factory() as s:
        user = s.scalars(select(User)).one()
        assert not twofactor.is_enabled(user)
        assert user.totp_secret != theirs


def test_returning_to_the_setup_page_shows_the_same_secret(client, session_factory):
    """The bug this replaced: a secret generated per render meant a refresh —
    or anything else that re-fetched the page — silently invalidated the QR
    somebody had just scanned, and their code stopped matching with nothing on
    screen to explain it. Found in sundries first.
    """
    make_login(client, session_factory)

    first = client.get("/profile/2fa", headers=HTML).text
    again = client.get("/profile/2fa", headers=HTML).text

    key = re.search(r"<code>([A-Z2-7]+)</code>", first).group(1)
    assert f"<code>{key}</code>" in again


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
    # Through the page, because that is what issues the secret. Posting one of
    # its own used to work, which is what let a caller enrol an authenticator
    # this account had never scanned.
    page = client.get("/profile/2fa", headers=HTML).text
    secret = re.search(r"<code>([A-Z2-7]+)</code>", page).group(1)

    resp = client.post("/profile/2fa/enable",
                       data={"code": code_for(secret),
                             "_csrf": session_csrf(session_factory)},
                       headers=HTML)

    assert "only time these are shown" in resp.text
    with session_factory() as s:
        user = s.scalars(select(User)).one()
        assert twofactor.is_enabled(user)
        assert twofactor.remaining_recovery_codes(s, user) == twofactor.RECOVERY_CODE_COUNT
    # And they are not shown again on a later visit.
    assert "only time these are shown" not in client.get("/profile", headers=HTML).text


def test_an_abandoned_enrolment_leaves_nothing_ENABLED(client, session_factory):
    """It leaves a secret now — that is what makes the QR survive a refresh —
    but nothing that can be signed in with.

    `is_enabled` wants `totp_enabled_at` as well, so an enrolment nobody
    finished is inert: the login flow never asks for a code, and the account is
    exactly as it was.
    """
    make_login(client, session_factory)

    client.get("/profile/2fa", headers=HTML)

    with session_factory() as s:
        user = s.scalars(select(User)).one()
        assert user.totp_secret is not None      # so returning shows the same QR
        assert user.totp_enabled_at is None      # and it turns nothing on
        assert twofactor.is_enabled(user) is False


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
    # A button opening the dialog, not a link away: the table is the index of
    # every way in, so acting on a row should not leave the page.
    assert ">Off<" in methods and 'data-dialog="twofactordialog"' in methods

    with session_factory() as s:
        twofactor.enable(s, s.scalars(select(User)).one(), pyotp.random_base32())
        s.commit()
    methods = _methods_table(client)
    assert ">On<" in methods and ">Off<" not in methods

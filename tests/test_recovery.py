"""Recovery codes: getting back in without an email reset.

A self-hosted app has no mailbox it can trust, and a reset link is only as
strong as the account it is sent to.

**The rule everything here protects: a single sign-in may spend at most ONE
recovery code** (decisions.md #45). Otherwise somebody holding the list spends
one for the password and a second for the second factor, and the list alone is
the whole account.

    lost authenticator   password + code            -> in; password and 2FA intact
    forgot password      email + code, then 2FA     -> new password; 2FA intact
    lost both            refused                    -> `python -m app.recover`

For an account with no second factor, email + code is full access, so the list
is password-equivalent. True of email reset too, but it has to be said on the
page rather than implied — `test_the_codes_page_is_honest_about_what_they_are`
pins that.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import select

from app import twofactor
from app.models import RecoveryCode, User, UserSession
from test_routes import PASSWORD, make_login, pre_auth_csrf, session_csrf

HTML = {"accept": "text/html"}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _codes_for(session_factory, email: str) -> list[RecoveryCode]:
    with session_factory() as s:
        user = s.scalars(select(User).where(User.email == email)).one()
        return s.scalars(
            select(RecoveryCode).where(RecoveryCode.user_id == user.id)).all()


def _issue(session_factory, email: str) -> list[str]:
    """Give an account a known set of codes and hand back the raw ones."""
    with session_factory() as s:
        user = s.scalars(select(User).where(User.email == email)).one()
        raw = twofactor.generate_recovery_codes(s, user)
        s.commit()
        return raw


def _enable_totp(session_factory, email: str) -> str:
    with session_factory() as s:
        user = s.scalars(select(User).where(User.email == email)).one()
        secret = twofactor.new_secret()
        user.totp_secret = secret
        user.totp_enabled_at = twofactor._utcnow()
        s.commit()
        return secret


def _recover(client, email: str, code: str):
    """Post the forgot-password form the way the page would."""
    client.get("/login/recover", headers=HTML)
    token = pre_auth_csrf(client)
    return client.post("/login/recover",
                       data={"email": email, "code": code, "_csrf": token},
                       headers=HTML, follow_redirects=False)


# --------------------------------------------------------------------------- #
# Entropy — NIST 800-63B on look-up secrets
# --------------------------------------------------------------------------- #

def test_a_code_carries_at_least_64_bits_of_entropy():
    """NIST SP 800-63B: a look-up secret SHALL have at least 64 bits of
    entropy, or at least 20 bits if failed attempts are throttled.

    The codes were 10 characters of a 31-symbol alphabet — 49.5 bits, which is
    legal only *because* of the throttle. Sizing them past 64 makes the entropy
    stand on its own, and the throttle stays anyway (see the test below): it
    defends against more than guessing, and removing a working control because
    a standard stopped insisting on it is not an improvement.
    """
    import math

    bits = twofactor.RECOVERY_CODE_CHARS * math.log2(len(twofactor._ALPHABET))

    assert bits >= 64, f"{bits:.1f} bits is below the NIST floor"


def test_the_alphabet_stays_unambiguous():
    """These get written on paper and read back. O/0 and I/1/L are the pairs
    people get wrong, and a recovery code is used exactly when nobody has the
    patience for a transcription error."""
    for character in "O0I1L":
        assert character not in twofactor._ALPHABET


def test_codes_are_stored_hashed_and_never_in_the_clear(session_factory, client):
    email = make_login(client, session_factory)
    raw = _issue(session_factory, email)

    stored = {row.code_hash for row in _codes_for(session_factory, email)}

    for code in raw:
        assert code not in stored
        assert twofactor.hash_code(code) in stored


def test_a_code_matches_however_it_is_typed_back(session_factory, client):
    """Lower case, spaces, dashes stripped — it was read off paper."""
    email = make_login(client, session_factory)
    raw = _issue(session_factory, email)[0]

    with session_factory() as s:
        user = s.scalars(select(User).where(User.email == email)).one()
        assert twofactor.consume_recovery_code(s, user, raw.lower().replace("-", " "))


# --------------------------------------------------------------------------- #
# Issued at account creation, not at 2FA enrolment
# --------------------------------------------------------------------------- #

def test_an_account_gets_codes_the_moment_it_is_created(client, session_factory):
    """Codes must not wait for 2FA enrolment: the people most likely to lock
    themselves out are the ones who skip it, and they would have no way back
    in at all."""
    from test_routes import do_setup

    do_setup(client)

    assert len(_codes_for(session_factory, "first@example.test")) == \
        twofactor.RECOVERY_CODE_COUNT


def test_turning_2fa_off_no_longer_destroys_them(client, session_factory):
    """It used to, correctly, when they were a 2FA-only fallback. Now they are
    the account's recovery and must outlive any one factor."""
    email = make_login(client, session_factory)
    _issue(session_factory, email)
    _enable_totp(session_factory, email)

    with session_factory() as s:
        user = s.scalars(select(User).where(User.email == email)).one()
        twofactor.disable(s, user)
        s.commit()

    assert len(_codes_for(session_factory, email)) == twofactor.RECOVERY_CODE_COUNT


def test_enabling_2fa_keeps_the_codes_you_already_saved(client, session_factory):
    """Regenerating here would silently invalidate the list somebody printed at
    signup, and they would not find out until they needed it."""
    email = make_login(client, session_factory)
    _issue(session_factory, email)
    before = {row.code_hash for row in _codes_for(session_factory, email)}
    assert before, "the fixture must start with codes or this proves nothing"

    with session_factory() as s:
        user = s.scalars(select(User).where(User.email == email)).one()
        secret = twofactor.new_secret()
        twofactor.enable(s, user, secret)
        s.commit()

    assert {row.code_hash for row in _codes_for(session_factory, email)} == before


def test_a_legacy_account_with_no_codes_gets_them_on_enrolment(client, session_factory):
    """Accounts that predate this have none, and the migration cannot make any
    — a code must be shown once, and a migration has nobody to show it to."""
    email = make_login(client, session_factory)
    with session_factory() as s:
        user = s.scalars(select(User).where(User.email == email)).one()
        for row in s.scalars(select(RecoveryCode).where(
                RecoveryCode.user_id == user.id)).all():
            s.delete(row)
        s.commit()

    with session_factory() as s:
        user = s.scalars(select(User).where(User.email == email)).one()
        codes = twofactor.enable(s, user, twofactor.new_secret())
        s.commit()

    assert len(codes) == twofactor.RECOVERY_CODE_COUNT


# --------------------------------------------------------------------------- #
# Case 1: lost the authenticator — password + code
# --------------------------------------------------------------------------- #

def test_a_code_gets_you_past_the_second_factor(client, session_factory):
    email = make_login(client, session_factory)
    raw = _issue(session_factory, email)
    _enable_totp(session_factory, email)
    client.cookies.clear()

    token = pre_auth_csrf(client)
    client.post("/login", data={"email": email, "password": PASSWORD, "_csrf": token},
                headers=HTML, follow_redirects=False)
    resp = client.post("/login/code",
                       data={"code": raw[0], "_csrf": pre_auth_csrf(client)},
                       headers=HTML, follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_that_path_leaves_both_the_password_and_2fa_alone(client, session_factory):
    """The point of only resetting what was lost. A phone left at the office
    should not cost somebody their enrolment."""
    email = make_login(client, session_factory)
    raw = _issue(session_factory, email)
    _enable_totp(session_factory, email)
    client.cookies.clear()

    token = pre_auth_csrf(client)
    client.post("/login", data={"email": email, "password": PASSWORD, "_csrf": token},
                headers=HTML, follow_redirects=False)
    client.post("/login/code", data={"code": raw[0], "_csrf": pre_auth_csrf(client)},
                headers=HTML, follow_redirects=False)

    with session_factory() as s:
        user = s.scalars(select(User).where(User.email == email)).one()
        assert twofactor.is_enabled(user) is True
        assert user.must_change_password is False


def test_a_spent_code_cannot_be_spent_again(client, session_factory):
    email = make_login(client, session_factory)
    raw = _issue(session_factory, email)

    with session_factory() as s:
        user = s.scalars(select(User).where(User.email == email)).one()
        assert twofactor.consume_recovery_code(s, user, raw[0]) is True
        s.commit()
    with session_factory() as s:
        user = s.scalars(select(User).where(User.email == email)).one()
        assert twofactor.consume_recovery_code(s, user, raw[0]) is False


def test_one_persons_code_is_useless_on_another_account(client, session_factory):
    """Codes are matched per user, not globally — an obvious property that a
    query missing its user_id filter would silently break."""
    import app.auth as auth_mod
    import factories as fac

    email = make_login(client, session_factory)
    raw = _issue(session_factory, email)
    with session_factory() as s:
        other = fac.make_user(s, "other@example.test",
                              password_hash=auth_mod.hash_password(PASSWORD))
        s.commit()

    with session_factory() as s:
        other = s.scalars(select(User).where(User.email == "other@example.test")).one()
        assert twofactor.consume_recovery_code(s, other, raw[0]) is False


# --------------------------------------------------------------------------- #
# Case 2: forgot the password — email + code, then still the second factor
# --------------------------------------------------------------------------- #

def test_a_code_and_an_email_start_a_password_reset(client, session_factory):
    email = make_login(client, session_factory)
    raw = _issue(session_factory, email)
    client.cookies.clear()

    resp = _recover(client, email, raw[0])

    assert resp.status_code == 303
    # Straight to the forced password change: there is no second factor here.
    assert resp.headers["location"] == "/profile"
    with session_factory() as s:
        assert s.scalars(select(User).where(
            User.email == email)).one().must_change_password is True


def test_the_reset_still_has_to_pass_the_second_factor(client, session_factory):
    """The heart of the design. Somebody holding only the code list is stopped
    here — they have satisfied the password and cannot satisfy 2FA."""
    email = make_login(client, session_factory)
    raw = _issue(session_factory, email)
    _enable_totp(session_factory, email)
    client.cookies.clear()

    resp = _recover(client, email, raw[0])

    assert resp.headers["location"] == "/login/code"
    with session_factory() as s:
        row = s.scalars(select(UserSession).order_by(UserSession.id.desc())).first()
        assert row.awaiting_totp is True


def test_a_code_cannot_also_answer_the_2fa_challenge_it_led_to(client, session_factory):
    """**The load-bearing rule.** One code per sign-in.

    Spending a second code here would mean the list alone defeats both factors,
    which is the whole attack this design exists to prevent. The TOTP itself
    still works — only the code is refused.
    """
    email = make_login(client, session_factory)
    raw = _issue(session_factory, email)
    _enable_totp(session_factory, email)
    client.cookies.clear()

    _recover(client, email, raw[0])
    resp = client.post("/login/code",
                       data={"code": raw[1], "_csrf": pre_auth_csrf(client)},
                       headers=HTML, follow_redirects=False)

    assert resp.status_code == 200          # refused, form redrawn
    with session_factory() as s:
        row = s.scalars(select(UserSession).order_by(UserSession.id.desc())).first()
        assert row.awaiting_totp is True, "a second code got past the second factor"
    # …and the second code was NOT consumed by the attempt.
    with session_factory() as s:
        user = s.scalars(select(User).where(User.email == email)).one()
        unused = s.scalars(select(RecoveryCode).where(
            RecoveryCode.user_id == user.id, RecoveryCode.used_at.is_(None))).all()
        assert len(unused) == twofactor.RECOVERY_CODE_COUNT - 1


def test_the_real_authenticator_still_works_after_a_code_reset(client, session_factory):
    """The legitimate user in that same flow: they forgot the password but do
    have their phone, so they get through."""
    import pyotp

    email = make_login(client, session_factory)
    raw = _issue(session_factory, email)
    secret = _enable_totp(session_factory, email)
    client.cookies.clear()

    _recover(client, email, raw[0])
    resp = client.post("/login/code",
                       data={"code": pyotp.TOTP(secret).now(),
                             "_csrf": pre_auth_csrf(client)},
                       headers=HTML, follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/profile"   # forced password change


def test_recovery_does_not_disclose_whether_an_account_exists(client, session_factory):
    """An unauthenticated endpoint that takes an email is an enumeration oracle
    unless the two answers are identical."""
    email = make_login(client, session_factory)
    raw = _issue(session_factory, email)
    client.cookies.clear()

    unknown = _recover(client, "nobody@example.test", raw[0])
    wrong = _recover(client, email, "AAAAA-AAAAA-AAAAA")

    assert unknown.status_code == wrong.status_code
    assert _body(unknown) == _body(wrong)


def _body(resp) -> str:
    """A response's text with the CSRF token masked, so two renders of the same
    page compare equal."""
    return re.sub(r'value="[^"]{20,}"', 'value="TOKEN"', resp.text)


def test_recovery_attempts_are_throttled(client, session_factory):
    """NIST makes throttling optional once a look-up secret clears 64 bits.
    It is kept anyway: it is the difference between one wrong guess and an
    unbounded stream of them, and it costs nothing."""
    email = make_login(client, session_factory)
    _issue(session_factory, email)
    client.cookies.clear()

    # The test config allows 3 attempts.
    for _ in range(4):
        resp = _recover(client, email, "AAAAA-AAAAA-AAAAA")

    assert "too many" in resp.text.lower()


def test_a_used_code_is_refused_by_the_recovery_form(client, session_factory):
    email = make_login(client, session_factory)
    raw = _issue(session_factory, email)
    client.cookies.clear()

    _recover(client, email, raw[0])
    client.cookies.clear()
    again = _recover(client, email, raw[0])

    assert again.status_code == 200          # refused
    # No apostrophe in the assertion: Jinja escapes them, so "aren&#39;t"
    # would never match. The message is worded to avoid one.
    assert "do not match" in again.text.lower()


def test_the_recovery_form_needs_its_csrf_token(client, session_factory):
    email = make_login(client, session_factory)
    raw = _issue(session_factory, email)
    client.cookies.clear()
    client.get("/login/recover", headers=HTML)

    resp = client.post("/login/recover", data={"email": email, "code": raw[0]},
                       headers=HTML)

    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# Case 3: lost both — the CLI, which is allowed to be blunt
# --------------------------------------------------------------------------- #

def test_the_cli_password_reset_also_clears_2fa(client, session_factory, capsys):
    """An operator resetting a password cannot prove the person still holds the
    authenticator, and leaving it on would lock them out of the account that
    was just recovered for them. Blunt, but it is the last resort by
    definition."""
    from app import recover as recover_cli

    email = make_login(client, session_factory)
    _enable_totp(session_factory, email)

    with session_factory() as s:
        user = s.scalars(select(User).where(User.email == email)).one()
        recover_cli.set_password(s, user, "brand-new-password", clear_2fa=True)
        s.commit()

    with session_factory() as s:
        user = s.scalars(select(User).where(User.email == email)).one()
        assert twofactor.is_enabled(user) is False
        assert user.must_change_password is True


def test_the_cli_says_out_loud_that_it_cleared_2fa():
    """The warning matters as much as the behaviour: an operator who does not
    know 2FA was removed will not tell the user to re-enrol, and the account
    quietly stays on one factor.

    Tested against `consequences()` rather than by running `main()`, because
    main() builds its own session factory from the real config and would talk
    to a different database than the one this suite is using — a test that
    passed against the wrong database would be worse than none.
    """
    from app import recover as recover_cli

    printed = "\n".join(recover_cli.consequences(["password", "2FA cleared"])).lower()

    assert "two-factor" in printed
    assert "removed" in printed
    assert "re-enrol" in printed
    # …and that the codes are NOT touched, which is the other half of what an
    # operator needs to know.
    assert "recovery codes were not changed" in printed


def test_the_cli_says_nothing_about_2fa_when_it_left_it_alone():
    """The warning has to be conditional, or it becomes noise people skip past
    — and then miss on the run where it mattered."""
    from app import recover as recover_cli

    printed = "\n".join(recover_cli.consequences(["password"])).lower()

    assert "two-factor" not in printed


def test_keeping_2fa_is_possible_when_only_the_password_went_missing(
        client, session_factory):
    """`--keep-2fa`, for the operator who knows the authenticator is fine."""
    from app import recover as recover_cli

    email = make_login(client, session_factory)
    _enable_totp(session_factory, email)

    with session_factory() as s:
        user = s.scalars(select(User).where(User.email == email)).one()
        changed = recover_cli.set_password(s, user, "brand-new-password",
                                           clear_2fa=False)
        s.commit()

    assert "2FA cleared" not in changed
    with session_factory() as s:
        user = s.scalars(select(User).where(User.email == email)).one()
        assert twofactor.is_enabled(user) is True


# --------------------------------------------------------------------------- #
# Being told about them
# --------------------------------------------------------------------------- #

def test_an_account_that_has_never_seen_its_codes_is_nagged(client, session_factory):
    """Codes nobody saved are codes nobody has. Existing accounts are in
    exactly this state after upgrading, which is why the prompt is a banner on
    every page rather than a line on the account page."""
    make_login(client, session_factory)

    page = client.get("/", headers=HTML)

    assert "recovery codes" in page.text.lower()
    assert "/profile/recovery" in page.text


def test_the_nag_stops_once_they_have_been_shown(client, session_factory):
    email = make_login(client, session_factory)

    client.post("/profile/recovery",
                data={"_csrf": session_csrf(session_factory)}, headers=HTML)
    page = client.get("/", headers=HTML)

    with session_factory() as s:
        user = s.scalars(select(User).where(User.email == email)).one()
        assert user.recovery_codes_seen_at is not None
    assert "/profile/recovery" not in page.text


def test_showing_them_issues_a_fresh_set(client, session_factory):
    """They are hashed, so the originals cannot be shown twice. Asking to see
    them means replacing them — and the page has to say so, because any list
    already written down stops working."""
    email = make_login(client, session_factory)
    before = {row.code_hash for row in _codes_for(session_factory, email)}

    page = client.post("/profile/recovery",
                       data={"_csrf": session_csrf(session_factory)}, headers=HTML)

    after = {row.code_hash for row in _codes_for(session_factory, email)}
    assert after.isdisjoint(before)
    assert len(after) == twofactor.RECOVERY_CODE_COUNT
    assert "no longer work" in page.text.lower() or "invalidat" in page.text.lower()


def test_the_codes_page_is_honest_about_what_they_are(client, session_factory):
    """The caveat that cannot be designed away: with no second factor, this
    list IS the account. Somebody deciding where to keep it needs to know that
    before they decide, not after."""
    make_login(client, session_factory)

    page = client.post("/profile/recovery",
                       data={"_csrf": session_factory and session_csrf(session_factory)},
                       headers=HTML)

    # Whitespace-normalised: the claim is what matters, and a reflow that puts
    # a line break inside the sentence does not stop the page making it.
    lowered = " ".join(page.text.lower().split())
    # The claim, not a keyword. This asserted `"safe" or "secure"` appeared
    # somewhere, which a condensing pass can break while the page still makes
    # the point — a word-presence check was standing in for the sentence.
    assert "treat it exactly like a password" in lowered, (
        "the page does not say what holding this list means without 2FA")
    assert "shown once" in lowered, "nothing says they cannot be seen again"
    # And the way to the full explanation, now that it lives in the docs.
    assert "learn more about recovery codes" in lowered


def test_the_wizard_shows_them_right_after_the_account_is_made(client, session_factory):
    """"As soon as an account is created" — the wizard's own step, before 2FA,
    because the codes are what makes skipping 2FA survivable."""
    from test_setup_wizard import _through_the_account

    _through_the_account(client)

    page = client.get("/setup/recovery", headers=HTML)

    assert page.status_code == 200
    codes = _codes_for(session_factory, "owner@example.test")
    assert len(codes) == twofactor.RECOVERY_CODE_COUNT


@pytest.mark.parametrize("path", ["/login/recover", "/profile/recovery"])
def test_neither_recovery_route_leaks_a_code_into_the_log(path, client,
                                                          session_factory, caplog):
    """A code in a log line is a credential in a log line."""
    import logging

    email = make_login(client, session_factory)
    raw = _issue(session_factory, email)
    caplog.set_level(logging.DEBUG)

    if path == "/login/recover":
        client.cookies.clear()
        _recover(client, email, raw[0])
    else:
        client.post(path, data={"_csrf": session_csrf(session_factory)}, headers=HTML)

    for code in raw:
        assert code not in caplog.text

"""The command-line way back in when there is no admin left to ask.

An admin can reset another person's password and clear their second factor from
the web UI. This is for the case with no web answer: the *only* admin loses
their password or their authenticator, which on a single-user self-hosted
install is otherwise the end of the account.

The tests care about two things — that it actually recovers the account, and
that it recovers it *properly*: a password set here has to be argon2id-hashed
and immediately usable at the real login form, because the failure mode of
doing this by hand in SQL is an account that can never log in again.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

import factories as fac
from app import auth as auth_mod
from app import recover, twofactor
from app.models import RecoveryCode, User, UserSession


@pytest.fixture
def cli(session_factory, monkeypatch, capsys):
    """Run `recover.main()` against the test database.

    The CLI builds its own session factory from config; point it at the one the
    test already has so both see the same rows.
    """
    monkeypatch.setattr(recover, "make_session_factory", lambda _db: session_factory)
    monkeypatch.setattr(recover, "load_config", lambda _cls: _Settings())
    monkeypatch.setattr(recover.tenancy, "install", lambda _factory: None)

    def _run(*argv):
        code = recover.main(list(argv))
        return code, capsys.readouterr().out
    return _run


class _Settings:
    database = None


@pytest.fixture
def locked_out(session_factory):
    """One admin, with 2FA on and a password nobody remembers."""
    with session_factory() as s:
        user = fac.make_user(s, "owner@example.test",
                             password_hash=auth_mod.hash_password("forgotten-password"),
                             is_admin=True)
        fac.make_portfolio(s, "Theirs", owner=user)
        twofactor.enable(s, user, twofactor.new_secret())
        auth_mod.create_session(s, user, _AuthSettings(), active_portfolio_id=None)
        s.commit()
        return user.id


class _AuthSettings:
    class auth:
        session_ttl_days = 30


# --------------------------------------------------------------------------- #
# Looking around
# --------------------------------------------------------------------------- #

def test_listing_shows_every_account_and_its_state(cli, locked_out):
    code, out = cli("--list")

    assert code == 0
    assert "owner@example.test" in out
    assert "admin" in out
    assert "2FA on" in out


def test_running_it_bare_lists_rather_than_erroring(cli, locked_out):
    """The first thing anyone does when locked out is run it with no arguments.
    That should show them what exists, not an argparse error."""
    code, out = cli()

    assert code == 0
    assert "owner@example.test" in out
    assert "--email" in out


def test_an_empty_database_says_so_kindly(cli, session_factory):
    code, out = cli("--list")

    assert code == 0
    assert "No accounts exist yet" in out


def test_an_unknown_email_is_an_error(cli, locked_out):
    code, out = cli("--email", "nobody@example.test", "--password", "whatever-long")

    assert code == 1


def test_naming_an_account_with_no_action_just_describes_it(cli, locked_out):
    code, out = cli("--email", "owner@example.test")

    assert code == 0
    assert "Nothing to do" in out


# --------------------------------------------------------------------------- #
# Recovering
# --------------------------------------------------------------------------- #

def test_the_new_password_actually_works_at_the_login_form(
    cli, client, session_factory, locked_out
):
    """The whole point of the command existing rather than documented SQL: the
    password is hashed the way the app hashes, so it works immediately."""
    cli("--email", "owner@example.test", "--password", "a-brand-new-password")
    cli("--email", "owner@example.test", "--clear-2fa")

    from test_routes import pre_auth_csrf

    token = pre_auth_csrf(client)
    resp = client.post(
        "/login",
        data={"email": "owner@example.test", "password": "a-brand-new-password",
              "_csrf": token},
        headers={"accept": "text/html"}, follow_redirects=False,
    )

    assert resp.status_code == 303
    # …and lands on the forced password change rather than the dashboard: the
    # operator who ran the command knows this password, so it is a handover
    # credential, not the account's password. See `set_password`.
    assert resp.headers["location"] == "/profile"


def test_the_password_is_stored_hashed_not_in_the_clear(cli, session_factory, locked_out):
    cli("--email", "owner@example.test", "--password", "a-brand-new-password")

    with session_factory() as s:
        user = s.get(User, locked_out)
        assert "a-brand-new-password" not in user.password_hash
        assert user.password_hash.startswith("$argon2")


def test_a_weak_password_is_refused(cli, session_factory, locked_out):
    """Same validation as the web form — the CLI is not a way around the rules."""
    code, _ = cli("--email", "owner@example.test", "--password", "short")

    assert code == 1
    with session_factory() as s:
        assert auth_mod.verify_password(
            s.get(User, locked_out).password_hash, "forgotten-password")


def test_clearing_2fa_removes_the_secret_but_keeps_the_recovery_codes(
        cli, session_factory, locked_out):
    """Destroying the codes here would be right if they were a 2FA-only
    fallback. They are the ACCOUNT's recovery — the way back in when the
    password is what went missing — so clearing one factor must not take them,
    or somebody who merely switched authenticator is left with no way back in
    and nothing on screen to say so.
    """
    before = _code_count(session_factory, locked_out)
    assert before > 0, "the fixture must start with codes or this proves nothing"

    code, _ = cli("--email", "owner@example.test", "--clear-2fa")

    assert code == 0
    with session_factory() as s:
        user = s.get(User, locked_out)
        assert not twofactor.is_enabled(user)
    assert _code_count(session_factory, locked_out) == before


def _code_count(session_factory, user_id: int) -> int:
    with session_factory() as s:
        return len(s.scalars(
            select(RecoveryCode).where(RecoveryCode.user_id == user_id)).all())


def test_recovery_signs_every_existing_session_out(cli, session_factory, locked_out):
    """A recovery usually means something went wrong; it is the one moment
    where signing everything out is clearly right."""
    with session_factory() as s:
        assert s.scalars(select(UserSession)).all() != []

    cli("--email", "owner@example.test", "--password", "a-brand-new-password")

    with session_factory() as s:
        assert s.scalars(select(UserSession)).all() == []


def test_a_recovered_account_must_choose_its_own_password(
    cli, session_factory, locked_out
):
    """**The forced password change is deliberate; do not remove it.**

    It looks like ceremony for the person recovering their own account — they
    own the machine and just chose the password. But the ordinary use is an
    administrator recovering somebody else's, which ends with the administrator
    knowing that password and the second factor cleared, so the password is all
    that protects it. One extra form on first sign-in closes that, and
    self-recovery pays the cheaper of the two mistakes. decisions.md #61.
    """
    cli("--email", "owner@example.test", "--password", "a-brand-new-password")

    with session_factory() as s:
        assert s.get(User, locked_out).must_change_password is True


def test_a_disabled_account_can_be_re_enabled(cli, session_factory):
    with session_factory() as s:
        user = fac.make_user(s, "off@example.test", is_active=False)
        s.commit()
        user_id = user.id

    cli("--email", "off@example.test", "--activate")

    with session_factory() as s:
        assert s.get(User, user_id).is_active


def test_admin_can_be_granted_when_the_only_admin_is_gone(cli, session_factory):
    with session_factory() as s:
        user = fac.make_user(s, "member@example.test", is_admin=False)
        s.commit()
        user_id = user.id

    cli("--email", "member@example.test", "--make-admin")

    with session_factory() as s:
        assert s.get(User, user_id).is_admin


def test_several_repairs_can_be_done_in_one_run(cli, session_factory, locked_out):
    code, out = cli("--email", "owner@example.test", "--password", "a-brand-new-password",
                    "--clear-2fa", "--activate")

    assert code == 0
    assert "password" in out and "2FA cleared" in out
    with session_factory() as s:
        user = s.get(User, locked_out)
        assert not twofactor.is_enabled(user)
        assert auth_mod.verify_password(user.password_hash, "a-brand-new-password")

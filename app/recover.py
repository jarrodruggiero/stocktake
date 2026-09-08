"""Getting back in when nobody can let you in.

    python -m app.recover --list
    python -m app.recover --email you@example.com --password 'new-password'
    python -m app.recover --email you@example.com --clear-2fa
    python -m app.recover --email you@example.com --make-admin

For the case with no web answer: the only admin loses their password or their
authenticator. Every run is logged and invalidates that account's sessions.

Why a command rather than documented SQL, and why it is not a backdoor:
decisions.md #71.
"""

from __future__ import annotations

import argparse
import logging
import sys

from sqlalchemy import select

from appcore import load_config, make_session_factory

from . import auth, tenancy, twofactor
from .models import User
from .settings import PortfolioSettings

log = logging.getLogger("app.recover")


def _users(session) -> list[User]:
    return list(session.scalars(select(User).order_by(User.id)).all())


def _describe(user: User) -> str:
    bits = [f"#{user.id}", user.email]
    if user.is_admin:
        bits.append("admin")
    if not user.is_active:
        bits.append("DISABLED")
    if twofactor.is_enabled(user):
        bits.append("2FA on")
    if user.must_change_password:
        bits.append("must change password")
    return "  ".join(bits)


def set_password(session, user: User, password: str, *,
                 clear_2fa: bool = True) -> list[str]:
    """Set a password from the command line, clearing the second factor with it
    by default. Returns what changed, for the caller to report.

    Both the clearing and the forced password change on next sign-in are
    deliberate — decisions.md #61. `--keep-2fa` covers the narrower case.
    """
    changed = ["password"]
    user.password_hash = auth.hash_password(password)
    user.must_change_password = True
    if clear_2fa and twofactor.is_enabled(user):
        twofactor.disable(session, user)
        changed.append("2FA cleared")
    return changed


def consequences(changed: list[str]) -> list[str]:
    """The lines printed after a recovery, spelling out what it cost.

    A separate function so it can be read — and tested — without standing up a
    database. The wording is the point: an operator who does not realise the
    second factor is gone will not tell the person to re-enrol, and the account
    stays on one factor indefinitely, looking perfectly healthy.
    """
    lines: list[str] = []
    if "2FA cleared" in changed:
        lines += [
            "",
            "  ! Two-factor authentication was REMOVED from this account.",
            "    Tell them to re-enrol an authenticator from Account settings",
            "    as soon as they are back in. Until they do, the password is",
            "    the only thing protecting it.",
        ]
    if "password" in changed:
        lines += [
            "",
            "  They will be asked to choose a new password when they sign in,",
            "  so the one you just set does not stay known to you.",
        ]
    lines += [
        "",
        "  Their recovery codes were NOT changed. If those are lost too, they",
        "  can generate a new set from Account settings once signed in.",
    ]
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.recover",
        description="Recover an account that has locked itself out.",
    )
    parser.add_argument("--list", action="store_true",
                        help="show every account and its state, then exit")
    parser.add_argument("--email", help="the account to act on")
    parser.add_argument("--password", help="set this password (argon2id hashed)")
    parser.add_argument("--clear-2fa", action="store_true",
                        help="remove the second factor (implied by --password)")
    parser.add_argument("--keep-2fa", action="store_true",
                        help="with --password, leave the second factor in place — "
                             "only safe when you know the person still has it")
    parser.add_argument("--make-admin", action="store_true",
                        help="grant admin, for when the only admin is gone")
    parser.add_argument("--activate", action="store_true",
                        help="re-enable a disabled account")
    args = parser.parse_args(argv)

    logging.basicConfig(level="INFO", format="%(levelname)s %(message)s")
    settings = load_config(PortfolioSettings)
    factory = make_session_factory(settings.database)
    tenancy.install(factory)

    with factory() as session:
        if args.list or not args.email:
            users = _users(session)
            if not users:
                print("No accounts exist yet — open the app and it will offer /setup.")
                return 0
            print(f"{len(users)} account(s):")
            for user in users:
                print("  " + _describe(user))
            if not args.email:
                # Not an error: running it bare to see what's there is the
                # first thing anyone does when locked out.
                print("\nPass --email to act on one of these.")
            return 0

        user = session.scalar(select(User).where(User.email == args.email.strip().lower()))
        if user is None:
            print(f"No account with email {args.email!r}. Use --list to see them.",
                  file=sys.stderr)
            return 1

        if not (args.password or args.clear_2fa or args.make_admin or args.activate):
            print(_describe(user))
            print("Nothing to do — add --password, --clear-2fa, --make-admin or --activate.")
            return 0

        changed = []
        if args.password:
            problem = auth.password_problem(args.password)
            if problem:
                print(f"Refusing that password: {problem}", file=sys.stderr)
                return 1
            changed.extend(
                set_password(session, user, args.password,
                             clear_2fa=not args.keep_2fa)
            )
        elif args.clear_2fa:
            twofactor.disable(session, user)
            changed.append("2FA cleared")
        if args.make_admin:
            user.is_admin = True
            changed.append("admin granted")
        if args.activate:
            user.is_active = True
            changed.append("account enabled")

        # Any existing session is now suspect — a recovery usually means
        # something went wrong, and it is the one moment where signing
        # everything out is clearly right.
        auth.destroy_sessions_for(session, user.id)
        session.commit()
        log.warning("account recovery for %s: %s", user.email, ", ".join(changed))
        print(f"Done for {user.email}: {', '.join(changed)}.")
        print("All existing sessions were signed out.")
        for line in consequences(changed):
            print(line)
    return 0


if __name__ == "__main__":  # pragma: no cover - module entrypoint, exercised via main()
    raise SystemExit(main())

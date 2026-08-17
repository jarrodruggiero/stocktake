"""Every account carries a stable, opaque public identifier.

Nothing reads this yet, and that is deliberate. It exists now because of what
it costs to add later.

If another application ever federates against this one, OIDC needs a `sub`
claim: an identifier that is
**stable** for the life of the account and **opaque** to anyone holding it. The
obvious candidate is `user.id`, and it is the wrong one twice over — an
auto-increment integer leaks how many accounts exist and how recently one was
made, and it collides with every other app's user 1.

Adding the column after federation means backfilling it across every app at
once, while they are already exchanging identities. Adding it now means one
migration in one app that nobody notices.

So this file's job is to make sure it is genuinely usable as a `sub` when that
day comes: unique, never reissued, never guessable from another one, and
present on every account including the ones that predate it.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

import factories as fac
from app.models import User


def _uuid_or_fail(value: str) -> uuid.UUID:
    """Parses, or the assertion message says what it actually got."""
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise AssertionError(f"{value!r} is not a UUID: {exc}") from None


def test_a_new_account_gets_one_without_being_asked(db):
    user = fac.make_user(db, "a@example.test")
    db.flush()

    assert user.public_id
    _uuid_or_fail(user.public_id)


def test_it_is_a_version_4_uuid(db):
    """Random, not time- or MAC-derived. A v1 UUID encodes the host's MAC
    address and the moment of creation, which is the opposite of opaque."""
    user = fac.make_user(db, "a@example.test")
    db.flush()

    assert _uuid_or_fail(user.public_id).version == 4


def test_two_accounts_never_share_one(db):
    first = fac.make_user(db, "a@example.test")
    second = fac.make_user(db, "b@example.test")
    db.flush()

    assert first.public_id != second.public_id


def test_it_is_not_derived_from_the_primary_key(db):
    """The property that makes it safe to hand to another app: it says nothing
    about which account it is, or how many exist.

    `user.id` fails both — `sub: 1` means "the first account on this install",
    and `sub: 47` says a great deal about how busy it is. The unguessability
    itself is covered by `test_it_is_a_version_4_uuid`, since a v4 UUID is 122
    random bits from the system CSPRNG; what is checked here is the specific
    mistake of deriving the value from the row.
    """
    users = [fac.make_user(db, f"u{i}@example.test") for i in range(5)]
    db.flush()

    assert len({u.public_id for u in users}) == 5
    for user in users:
        assert user.public_id != str(user.id)
        # A UUID built out of the id — uuid.UUID(int=...) — is the other
        # tempting shortcut, and it is just a padded hex form of the number.
        assert user.public_id != str(uuid.UUID(int=user.id))

    # NOT `startswith(str(user.id))`, which was here and was flaky at about one
    # run in four: a v4 UUID begins with a random hex digit, so for user 1 it
    # legitimately starts with "1" one time in sixteen. An assertion that fails
    # on correct code is worse than no assertion — it trains people to re-run
    # the suite instead of reading it.


def test_it_survives_a_change_to_everything_else(db):
    """Stable for the life of the account — an OIDC `sub` that changed when
    somebody edited their email would silently unlink them from every other app
    in the suite."""
    user = fac.make_user(db, "a@example.test")
    db.flush()
    original = user.public_id

    user.email = "renamed@example.test"
    user.name = "Renamed"
    user.password_hash = "different"
    db.flush()

    assert user.public_id == original


def test_the_database_refuses_a_duplicate(db):
    """Belt and braces behind the generator: a `sub` collision across two
    accounts would let one person be authenticated as another."""
    import pytest
    from sqlalchemy.exc import IntegrityError

    first = fac.make_user(db, "a@example.test")
    db.flush()
    second = fac.make_user(db, "b@example.test")
    second.public_id = first.public_id

    with pytest.raises(IntegrityError):
        db.flush()
    # A failed flush leaves the session in a broken state — without this the
    # fixture's teardown raises and buries the real assertion.
    db.rollback()


def test_every_account_created_through_the_app_has_one(client, session_factory):
    """Not just the factory — the real signup path, which is what actually
    creates accounts."""
    from test_routes import do_setup

    do_setup(client)

    with session_factory() as s:
        user = s.scalars(select(User)).one()
        _uuid_or_fail(user.public_id)


def test_an_admin_created_account_has_one_too(client, session_factory):
    from test_routes import make_login, session_csrf

    make_login(client, session_factory)
    client.post("/users/add", data={"name": "Second", "email": "second@example.test",
                                "password": "correct-horse-battery",
                                "_csrf": session_csrf(session_factory)},
                headers={"accept": "text/html"})

    with session_factory() as s:
        user = s.scalars(select(User).where(
            User.email == "second@example.test")).one()
        _uuid_or_fail(user.public_id)


def test_it_is_not_exposed_anywhere_yet(client, session_factory):
    """Nothing reads it, and until federation exists nothing should — a public
    identifier that leaks into pages before it has a purpose is just a wider
    surface. This is the tripwire for that."""
    from test_routes import make_login

    email = make_login(client, session_factory)
    with session_factory() as s:
        public_id = s.scalars(select(User).where(User.email == email)).one().public_id

    for path in ("/", "/profile", "/users", "/admin"):
        page = client.get(path, headers={"accept": "text/html"})
        assert public_id not in page.text, f"{path} leaks the public id"

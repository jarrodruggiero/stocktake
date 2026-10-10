"""Which account a provider's identity signs in as, and when a new one is made.

The protocol is tested in `test_oidc.py`. This is the half that decides who
gets in, so nearly all of it is about refusal.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

import factories as fac
from app import federation, invites, twofactor
from app.models import ExternalIdentity, OidcState, PortfolioMember, User
from app.settings import PortfolioSettings
from appcore import oidc

ISSUER = "https://idp.example.test"


def an_identity(subject: str = "subject-1", email: str = "them@example.test",
                name: str = "Them") -> oidc.Identity:
    return oidc.Identity(subject=subject, issuer=ISSUER, email=email,
                         email_verified=True, name=name)


@pytest.fixture
def settings() -> PortfolioSettings:
    s = PortfolioSettings()
    s.auth.oidc.enabled = True
    s.auth.oidc.issuer = ISSUER
    s.auth.oidc.client_id = "stocktake"
    s.auth.oidc.client_secret = "a-secret"
    s.auth.oidc.redirect_uri = "https://stocktake.example.test/login/oidc/callback"
    return s


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #

def test_a_linked_identity_signs_in_as_its_account(session_factory, settings):
    settings.auth.oidc.provisioning = "off"
    with session_factory() as db:
        user = fac.make_user(db, "them@example.test")
        db.flush()
        federation.link(db, user, an_identity())

        assert federation.resolve(db, settings, an_identity(), None).id == user.id


def test_an_email_match_is_not_an_account_match(session_factory, settings):
    """The rule the whole feature rests on. An address is something a directory
    can be TOLD, so matching on it would let anybody who can set an email in
    the provider take over an existing local account.
    """
    settings.auth.oidc.provisioning = "off"
    with session_factory() as db:
        fac.make_user(db, "them@example.test")
        db.commit()

        with pytest.raises(federation.FederationError):
            federation.resolve(db, settings, an_identity(), None)


def test_provisioning_refuses_to_join_an_existing_email(session_factory, settings):
    """Open provisioning must not silently adopt a local account either — two
    people are not one person because a directory says so."""
    settings.auth.oidc.provisioning = "open"
    with session_factory() as db:
        fac.make_user(db, "them@example.test")
        db.commit()

        with pytest.raises(federation.FederationError, match="already uses"):
            federation.resolve(db, settings, an_identity(), None)


# --------------------------------------------------------------------------- #
# Who may sign in
# --------------------------------------------------------------------------- #

def test_off_admits_nobody_new(session_factory, settings):
    settings.auth.oidc.provisioning = "off"
    with session_factory() as db:
        with pytest.raises(federation.FederationError, match="administrator"):
            federation.resolve(db, settings, an_identity(), None)
        assert db.scalar(select(User)) is None


def test_open_admits_anyone_the_provider_authenticates(session_factory, settings):
    settings.auth.oidc.provisioning = "open"
    with session_factory() as db:
        user = federation.resolve(db, settings, an_identity(), None)

        assert user.email == "them@example.test"
        assert user.password_hash is None


def test_invite_admits_a_stranger_only_with_one(session_factory, settings):
    settings.auth.oidc.provisioning = "invite"
    with session_factory() as db:
        with pytest.raises(federation.FederationError, match="invitation"):
            federation.resolve(db, settings, an_identity(), None)
        assert db.scalar(select(User)) is None


def test_invite_lets_the_holder_in_and_into_the_portfolio(session_factory, settings):
    settings.auth.oidc.provisioning = "invite"
    with session_factory() as db:
        owner = fac.make_user(db, "owner@example.test")
        portfolio = fac.make_portfolio(db, "Family", owner=owner)
        db.flush()
        token = invites.create(db, portfolio_id=portfolio.id, role="viewer",
                               created_by=owner.id)
        invite = invites.lookup(db, token)

        user = federation.resolve(db, settings, an_identity(), invite)
        invites.accept_row(db, invite, user)

        member = db.scalar(select(PortfolioMember).where(
            PortfolioMember.user_id == user.id))
        assert member.role == "viewer"


# --------------------------------------------------------------------------- #
# The state that survives the redirect
# --------------------------------------------------------------------------- #

def test_the_state_must_match_what_was_issued(session_factory, settings):
    """What stops a callback somebody else constructed from completing a
    sign-in here."""
    with session_factory() as db:
        row = OidcState(token_hash=federation._hash("tok"), state="issued",
                        nonce="n", verifier="v",
                        expires_at=federation._utcnow() + dt.timedelta(minutes=5))
        db.add(row)
        db.flush()

        with pytest.raises(federation.FederationError, match="did not match"):
            federation.take_state(db, "tok", "something-else")


def test_an_expired_attempt_is_refused(session_factory, settings):
    with session_factory() as db:
        db.add(OidcState(token_hash=federation._hash("tok"), state="s", nonce="n",
                         verifier="v",
                         expires_at=federation._utcnow() - dt.timedelta(seconds=1)))
        db.flush()

        with pytest.raises(federation.FederationError, match="expired"):
            federation.take_state(db, "tok", "s")


def test_a_stale_attempt_is_swept(session_factory):
    with session_factory() as db:
        db.add(OidcState(token_hash="x", state="s", nonce="n", verifier="v",
                         expires_at=federation._utcnow() - dt.timedelta(minutes=1)))
        db.flush()

        assert federation.purge_expired_states(db) == 1


# --------------------------------------------------------------------------- #
# Linking and unlinking
# --------------------------------------------------------------------------- #

def test_one_provider_account_belongs_to_one_local_account(session_factory):
    with session_factory() as db:
        first = fac.make_user(db, "first@example.test")
        second = fac.make_user(db, "second@example.test")
        db.flush()
        federation.link(db, first, an_identity())

        with pytest.raises(federation.FederationError, match="already linked"):
            federation.link(db, second, an_identity())


def test_unlinking_the_only_way_in_is_refused(session_factory, settings):
    """An account with no password and no other identity would be unreachable,
    and a settings page should not be able to do that quietly."""
    settings.auth.oidc.provisioning = "open"
    with session_factory() as db:
        user = federation.resolve(db, settings, an_identity(), None)
        row = db.scalar(select(ExternalIdentity))

        with pytest.raises(federation.FederationError, match="Set a password"):
            federation.unlink(db, user, row.id)
        assert db.scalar(select(ExternalIdentity)) is not None


def test_unlinking_is_allowed_once_a_password_exists(session_factory, settings):
    settings.auth.oidc.provisioning = "open"
    with session_factory() as db:
        user = federation.resolve(db, settings, an_identity(), None)
        user.password_hash = "an-argon2-hash"
        db.flush()
        row = db.scalar(select(ExternalIdentity))

        assert federation.unlink(db, user, row.id)
        assert db.scalar(select(ExternalIdentity)) is None


def test_one_account_cannot_unlink_anothers_identity(session_factory, settings):
    settings.auth.oidc.provisioning = "open"
    with session_factory() as db:
        theirs = federation.resolve(db, settings, an_identity(), None)
        mine = fac.make_user(db, "mine@example.test")
        db.flush()
        row = db.scalar(select(ExternalIdentity).where(
            ExternalIdentity.user_id == theirs.id))

        assert not federation.unlink(db, mine, row.id)
        assert db.get(ExternalIdentity, row.id) is not None


# --------------------------------------------------------------------------- #
# Off means off
# --------------------------------------------------------------------------- #

def test_an_unconfigured_provider_cannot_begin(session_factory):
    plain = PortfolioSettings()
    with session_factory() as db:
        with pytest.raises(federation.FederationError):
            federation.begin(db, plain)


def test_the_reason_names_what_is_missing(settings):
    settings.auth.oidc.client_secret = None
    assert "client_secret" in federation.unavailable_reason(settings)

    settings.auth.oidc.enabled = False
    assert "Admin" in federation.unavailable_reason(settings)


# --------------------------------------------------------------------------- #
# Public clients
# --------------------------------------------------------------------------- #

def test_a_public_client_is_offered_without_a_secret(settings):
    """A public client has no secret to set, so the absence of one is not a
    reason to withhold the button — which is what stopped it rendering."""
    settings.auth.oidc.client_auth = "none"
    settings.auth.oidc.client_secret = None

    assert federation.configured(settings)
    assert federation.unavailable_reason(settings) is None


def test_only_the_exact_value_none_waives_the_secret(settings):
    """Anything unrecognised still needs one. A `client_auth` typo must not be
    a way to run unauthenticated — decisions.md #121."""
    settings.auth.oidc.client_auth = "nome"
    settings.auth.oidc.client_secret = None

    assert not federation.configured(settings)
    assert "client_secret" in federation.unavailable_reason(settings)


def test_the_client_auth_setting_reaches_the_provider(settings):
    """The wire between the two halves. Without it every test above still
    passes and the token request goes out with a Basic header anyway."""
    settings.auth.oidc.client_auth = "none"

    assert federation.provider(settings).client_auth == "none"


def test_a_provisioned_account_can_get_back_in_without_the_provider(
    session_factory, settings
):
    """It has no password, so recovery codes are the only self-service route
    back if the provider goes away.

    Every other path that creates an account issues them; this one did not,
    which left the account with no password AND no codes — an admin reset was
    the only way in. `recovery_codes_seen_at` stays NULL so the banner still
    asks them to save a set of their own.
    """
    settings.auth.oidc.provisioning = "open"
    with session_factory() as db:
        user = federation.resolve(db, settings, an_identity(), None)

        assert user.password_hash is None
        assert twofactor.remaining_recovery_codes(db, user) > 0
        assert user.recovery_codes_seen_at is None


# --------------------------------------------------------------------------- #
# With more than one on file
# --------------------------------------------------------------------------- #
# The tests above each put ONE identity in the database, and a lookup that
# ignored its `where` returned that one anyway: a mutation run removed the
# (issuer, subject) match from sign-in, linking and unlinking, and nothing
# failed. These put a second one beside it.

def test_each_identity_signs_in_as_its_own_account(session_factory, settings):
    settings.auth.oidc.provisioning = "off"
    with session_factory() as db:
        first = fac.make_user(db, "first@example.test")
        second = fac.make_user(db, "second@example.test")
        db.flush()
        federation.link(db, first, an_identity("subject-1", "first@example.test"))
        federation.link(db, second, an_identity("subject-2", "second@example.test"))

        assert federation.resolve(db, settings, an_identity("subject-2"), None).id == second.id
        assert federation.resolve(db, settings, an_identity("subject-1"), None).id == first.id
        # The same subject from another issuer is somebody else entirely.
        elsewhere = oidc.Identity(subject="subject-1", issuer="https://other.example.test",
                                  email="first@example.test", email_verified=True, name="x")
        with pytest.raises(federation.FederationError):
            federation.resolve(db, settings, elsewhere, None)


def test_linking_a_new_identity_beside_someone_elses_is_allowed(session_factory):
    with session_factory() as db:
        first = fac.make_user(db, "first@example.test")
        second = fac.make_user(db, "second@example.test")
        db.flush()
        federation.link(db, first, an_identity("subject-1"))

        row = federation.link(db, second, an_identity("subject-2"))

        assert (row.user_id, row.subject) == (second.id, "subject-2")


def test_unlinking_one_of_two_leaves_the_other(session_factory):
    """With two, either may go even with no password: the other is still a way
    in. The one named goes, not whichever was linked first."""
    with session_factory() as db:
        user = fac.make_user(db, "them@example.test", password_hash=None)
        db.flush()
        federation.link(db, user, an_identity("subject-1"))
        second = federation.link(db, user, an_identity("subject-2"))

        assert federation.unlink(db, user, second.id) is True

        assert [i.subject for i in federation.identities_for(db, user)] == ["subject-1"]


def test_two_attempts_in_flight_each_finish_with_their_own(session_factory):
    """Two tabs, or two people, signing in at once: each callback is matched to
    the attempt its own token started."""
    with session_factory() as db:
        for token, state in (("first-token", "first-state"), ("second-token", "second-state")):
            db.add(OidcState(token_hash=federation._hash(token), state=state, nonce=token,
                             verifier="v",
                             expires_at=federation._utcnow() + dt.timedelta(minutes=5)))
        db.flush()

        assert federation.take_state(db, "second-token", "second-state").nonce == "second-token"
        assert federation.take_state(db, "first-token", "first-state").nonce == "first-token"


def test_a_deactivated_account_does_not_sign_in_through_its_provider(session_factory, settings):
    with session_factory() as db:
        user = fac.make_user(db, "them@example.test")
        db.flush()
        federation.link(db, user, an_identity())
        user.is_active = False
        db.flush()

        with pytest.raises(federation.FederationError, match="no longer active"):
            federation.resolve(db, settings, an_identity(), None)

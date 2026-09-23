"""Every route the login middleware waves through has to guard itself.

`PUBLIC_PATHS` and `PUBLIC_PREFIXES` are the list of things reachable with no
session. Adding to that list is a two-word change with no obvious consequence,
and **the failure mode is silent publication of an endpoint** — nothing breaks,
nothing logs, and the route simply answers anybody.

The `/api/` half of this already has derived guards. The rest did not, and it
grew a new member: `/oidc/backchannel-logout` accepts a POST from a provider
with no cookie, no CSRF token and no API key, because the signature on the
`logout_token` is the whole authentication. That is correct, and it is exactly
the kind of correct thing that makes the next one easier to wave through.

So this reads the routing table and the handlers' own source. A public POST
must call something that authenticates it; a new one that calls none of them
fails here, naming itself.
"""

from __future__ import annotations

import inspect

import pytest

# What counts as a route authenticating itself. Each is a call that refuses the
# request unless something proves it is genuine:
#
#   verify_csrf           — the session's own CSRF token
#   verify_pre_auth_csrf  — the token the login pages mint before a session
#                           exists, which is what carries /login/recover, both
#                           halves of the passkey ceremony, and invite signup
#   verify_logout_token   — a provider's signature on a back-channel logout
#   api_session           — an API key
#   _wizard_step          — the first-run wizard, which owns its own gate
#
# Add to this list only alongside the thing that does the guarding, never to
# make a failure go away. `verify_csrf` is a substring of the pre-auth name, so
# the two are listed for the reader rather than for the match.
SELF_GUARDING = ("verify_csrf", "verify_pre_auth_csrf", "verify_logout_token",
                 "api_session", "_wizard_step")


@pytest.fixture(scope="module")
def main(app_module):
    return app_module


def _public_post_routes(main):
    """(path, handler) for every POST the middleware does not protect."""
    found = []
    for route in main.app.routes:
        methods = getattr(route, "methods", None) or set()
        path = getattr(route, "path", "")
        if "POST" not in methods:
            continue
        if path in main.PUBLIC_PATHS or path.startswith(main.PUBLIC_PREFIXES):
            found.append((path, route.endpoint))
    return found


def test_there_are_public_posts_to_check(main):
    """An empty sweep proves nothing — the guard's own smoke test."""
    paths = [path for path, _ in _public_post_routes(main)]

    assert "/login" in paths
    assert "/oidc/backchannel-logout" in paths


def test_every_public_post_authenticates_itself(main):
    naked = []
    for path, handler in _public_post_routes(main):
        try:
            source = inspect.getsource(handler)
        except OSError:  # pragma: no cover - a handler with no readable source
            continue
        if not any(guard in source for guard in SELF_GUARDING):
            naked.append(path)

    assert not naked, (
        "these POST routes are public to the middleware and guard nothing: "
        f"{naked}. Either they are protected some other way — say which, here "
        "— or they answer anybody."
    )


def test_the_guard_notices_a_naked_public_post(main, monkeypatch):
    """Planted violation, so the sweep cannot pass by finding nothing.

    A path added to `PUBLIC_PATHS` whose handler guards nothing is the exact
    mistake this exists to catch, and it is a two-word change to make.
    """
    async def unguarded_handler():  # pragma: no cover - never called
        return {}

    class _Route:
        methods = {"POST"}
        path = "/a-new-public-thing"
        endpoint = unguarded_handler

    monkeypatch.setattr(main, "PUBLIC_PATHS", main.PUBLIC_PATHS | {_Route.path})
    # `app.routes` is a read-only property, so the list it returns is extended
    # in place and put back — a fake app object would test the fake instead.
    original = list(main.app.routes)
    main.app.router.routes.append(_Route())
    try:
        naked = [path for path, handler in _public_post_routes(main)
                 if not any(g in inspect.getsource(handler)
                            for g in SELF_GUARDING)]
    finally:
        main.app.router.routes[:] = original

    assert naked == ["/a-new-public-thing"]

"""A viewer reads everything and changes nothing, on every route.

Each route that writes checks the role itself (`_require_write`, or the owner
or admin check above it). A mutation run removed that check from saving and
deleting charts and from every route of the plan, recording a planned buy
among them, which writes a trade; nothing failed. The tests that sign in as a
viewer tried a handful of routes.

So this signs in as the owner, makes whatever each route names in its path,
turns them into a viewer, and sends every writing route the request a careful
person would make, the one test_hostile_input.py builds. Each must be refused
and leave every table as it was.

It is an inventory too: every POST route is either a write, refused here, or
one a viewer may use, with the reason. A new route has to be put in one.

And nothing writes without the session's CSRF token. `verify_csrf` had its
own tests, and about 17 routes had a test that they needed it; removing the
check from any of 12 others passed everything. Every route that checks is
now sent the careful request without the token, and the routes that don't
check are written down with why.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest
from sqlalchemy import select, text
from test_hostile_input import (
    HTML,
    WHEN,
    _all_routes,
    _fields,
    _fresh,
    _method,
    _send,
    _signed_in,
    _typed,
)

from app import tenancy
from app.models import Base, PortfolioMember, User

# Routes a viewer may use, and why.
VIEWER_MAY = {
    # Signing in and out, and the first-run wizard: before any role exists.
    "/login": "sign-in", "/login/code": "sign-in", "/login/passkey": "sign-in",
    "/login/passkey/options": "sign-in", "/login/recover": "sign-in", "/logout": "sign-out",
    "/oidc/backchannel-logout": "a provider ending sessions",
    "/session/keepalive": "their own session", "/setup": "first run",
    "/setup/2fa": "first run", "/setup/2fa/skip": "first run", "/setup/database": "first run",
    "/setup/database/test": "first run", "/setup/environment": "first run",
    "/setup/features": "first run", "/setup/finish": "first run",
    "/setup/portfolio": "first run", "/setup/profile": "first run",
    "/invite/{token}/accept": "the token is the authorisation",
    "/invite/{token}/signup": "the token is the authorisation",
    # Their own account, which a role in some portfolio does not touch.
    "/profile/2fa/disable": "own account", "/profile/2fa/enable": "own account",
    "/profile/appearance": "own account", "/profile/colors": "own account",
    "/profile/colors/reset": "own account", "/profile/columns": "own account",
    "/profile/columns/order": "own account", "/profile/columns/reset": "own account",
    "/profile/oidc/link": "own account", "/profile/oidc/{identity_id}/unlink": "own account",
    "/profile/passkeys": "own account", "/profile/passkeys/options": "own account",
    "/profile/passkeys/{credential_id}/remove": "own account",
    "/profile/password": "own account", "/profile/recovery": "own account",
    "/profile/recovery/later": "own account", "/profile/sessions/revoke-others": "own account",
    "/profile/sessions/{session_id}/revoke": "own account",
    "/portfolio/switch": "among their own memberships",
    "/portfolio/new": "a portfolio of their own",
    # A key never does more than its creator can (auth.key_reach), so a viewer's
    # key reads; the API's own tests refuse its writes.
    "/keys/new": "reads only, for a viewer", "/keys/{key_id}/revoke": "their own key",
    "/api/v1/trades": "refused in test_api", "/api/v1/dividends": "refused in test_api",
    # Charts are each person's; building, arranging and deleting their own
    # writes nothing of the portfolio's.
    "/charts/order": "their own charts' order", "/charts/preview": "reads",
    "/charts/save": "their own charts", "/charts/{chart_id}/delete": "their own charts",
}

SCHEDULED = {"/schedule/complete", "/schedule/skip", "/schedule/delete"}
WRITES = sorted(r.path for r in _all_routes() if "POST" in r.methods
                and r.path not in VIEWER_MAY)


def _gate(route) -> str | None:
    """The role check a handler makes itself, read off the calls it makes."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(route.endpoint)))
    called = {node.func.id for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    return next((g for g in ("_require_admin", "_require_owner") if g in called), None)


# Each right, and the role holding every right below it but not that one: a
# portfolio's owner who is not a site admin, and a member who writes but owns
# nothing.
JUST_BELOW = {"_require_admin": "owner", "_require_owner": "member"}
GATED = sorted((_method(r), r.path, JUST_BELOW[gate])
               for r in _all_routes() if (gate := _gate(r)))


@pytest.fixture
def contained(monkeypatch, tmp_path):
    """What test_hostile_input's `sandbox` points away: the config file, the
    formats folder, staging, Yahoo and the restart. Copied rather than
    imported, because it is a fixture."""
    from pathlib import Path

    from test_hostile_input import YAHOO_SAYS
    from test_pricefeed import stub_yf

    from app import configfile, lifecycle, statements
    from app import main as main_mod

    stub_yf(monkeypatch, info=dict(YAHOO_SAYS))
    monkeypatch.setattr(statements, "_pdf_text", lambda data: data.decode("utf-8", "replace"))
    monkeypatch.setattr(lifecycle, "request_stop", lambda reason: None)
    real = Path(main_mod.APP_DIR.parent, "tests", "config.test.yaml")
    config = tmp_path / "config.yaml"
    config.write_text(real.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(configfile, "CONFIG_FILE", str(config))
    monkeypatch.setattr(main_mod.settings.imports, "templates_dir", str(tmp_path / "formats"))
    monkeypatch.setattr(main_mod.settings.imports, "staging_dir", str(tmp_path / "staged"))


def _everything(session_factory) -> dict:
    """Every table's rows, but the session's own activity, which any request
    moves (the idle window slides)."""
    out = {}
    with session_factory() as s:
        tenancy.allow_unscoped(s)
        for table in Base.metadata.sorted_tables:
            if table.name in ("user_session", "login_attempt"):
                continue
            out[table.name] = sorted(map(repr, s.execute(text(
                f'SELECT * FROM "{table.name}"')).all()))
    return out


def _refused(client, session_factory, monkeypatch, method: str, path: str, role: str):
    """Send the careful request as somebody with `role` and no site admin, and
    hold the route to a 403 that changed nothing."""
    import test_hostile_input

    # The walker leaves the OCR route out (it needs tesseract); a refusal comes
    # before any of that, so any file reaches it.
    monkeypatch.setitem(test_hostile_input.FILES, "/imports-exports/statement/visual",
                        ("statement.pdf", b"%PDF-1.4", "application/pdf"))
    route = next(r for r in _all_routes() if r.path == path and method in r.methods)
    world = _signed_in(client, session_factory)
    if path in SCHEDULED:
        client.post("/schedule/save", headers=HTML, follow_redirects=False, data={
            "name": "Regular buys", "interval_days": "28", "amount": "500",
            "brokerage": "9.50", "start_date": WHEN, "tickers": "ACME",
            "_csrf": world.csrf()})
    fields = _fields(route)
    made = {f.name: _fresh(world, f.name) for f in fields if f.where == "path"}
    values = _typed(world, route, world.tick(), made)
    values.setdefault("token", "0" * 32)          # the OCR edit's staged statement
    with session_factory() as s:
        s.scalar(select(PortfolioMember).where(
            PortfolioMember.user_id == world.user_id,
            PortfolioMember.portfolio_id == world.portfolio_id)).role = role
        s.get(User, world.user_id).is_admin = False     # that role, and nothing more
        s.commit()
    before = _everything(session_factory)

    resp = _send(world, route, fields, values)

    assert resp.status_code == 403, (path, resp.status_code, resp.text[:200])
    assert _everything(session_factory) == before, path


@pytest.mark.parametrize("path", WRITES)
def test_a_viewer_is_refused_every_write(client, session_factory, contained, monkeypatch,
                                         path):
    _refused(client, session_factory, monkeypatch, "POST", path, "viewer")


@pytest.mark.parametrize("method, path, role", GATED,
                         ids=[f"{method} {path}" for method, path, _role in GATED])
def test_each_right_is_refused_to_the_role_just_below_it(client, session_factory, contained,
                                                         monkeypatch, method, path, role):
    """A viewer has no right at all, so the sweep above cannot tell an admin
    check from a write check: with one swapped for the other on deleting an
    import format, everything passed, and an owner who is no site admin could
    delete a format for the whole app. Here each admin route is sent by such an
    owner, and each owner route by a member who can write."""
    _refused(client, session_factory, monkeypatch, method, path, role)


# Written out rather than taken from GATED, which is read off the handlers: a
# check swapped for a weaker one takes its route out of GATED, and a test that
# reads its own expectation cannot notice (test_tenancy's PERSONAL_MODELS).
ADMIN_ROUTES = {
    "GET /users", "GET /admin/accounts", "GET /admin/settings", "GET /admin/logs.json",
    "POST /users/add", "POST /users/{user_id}/active", "POST /users/{user_id}/reset",
    "POST /users/{user_id}/2fa/clear", "POST /admin/settings", "POST /admin/settings/restart",
    "POST /portfolio/settings", "POST /imports-exports/formats",
    "POST /imports-exports/formats/{kind}/{slug}/delete",
    "POST /imports-exports/statement/design/install",
}
OWNER_ROUTES = {
    "POST /members/add", "POST /members/invite", "POST /members/invite/{invite_id}/revoke",
    "POST /members/{member_id}/role", "POST /members/{member_id}/remove",
}


def test_the_gated_routes_are_the_ones_written_down():
    gated = {(f"{method} {path}", role) for method, path, role in GATED}

    assert gated == ({(route, "owner") for route in ADMIN_ROUTES}
                     | {(route, "member") for route in OWNER_ROUTES})


def test_every_post_route_is_either_a_write_or_one_a_viewer_may_use():
    posts = {r.path for r in _all_routes() if "POST" in r.methods}
    stale = sorted(set(VIEWER_MAY) - posts)
    assert not stale, f"VIEWER_MAY names routes that no longer exist: {stale}"
    assert len(WRITES) >= 40, "the sweep is not empty"
    assert {"/trade/new", "/schedule/complete", "/schedule/save"} <= set(WRITES)


# --------------------------------------------------------------------------- #
# Nothing writes without the session's CSRF token
# --------------------------------------------------------------------------- #

# POST routes that do not check the session's token, and why. Before anyone
# signs in, the forms carry the pre-auth token instead (`auth.verify_pre_auth
# _csrf`, double-submit against a cookie); test_security covers that one.
NO_SESSION_TOKEN = {
    "/api/v1/trades": "a bearer key, which no browser sends on its own",
    "/api/v1/dividends": "a bearer key, which no browser sends on its own",
    "/oidc/backchannel-logout": "the provider's signature on the token is the "
                                "authorisation (decisions.md #127)",
    "/charts/preview": "draws a draft chart and writes nothing",
}
PRE_AUTH_TOKEN = {
    "/login/passkey", "/login/passkey/options", "/login/recover", "/invite/{token}/signup",
    "/setup", "/setup/database", "/setup/database/test", "/setup/profile",
}


def _token_check(route) -> str:
    called = set()
    for node in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(route.endpoint)))):
        if isinstance(node, ast.Call):
            func = node.func
            called.add(func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", ""))
    if "verify_csrf" in called:
        return "session"
    return "pre-auth" if any("pre_auth" in name for name in called) else "none"


# Checked against the session's token, and tested elsewhere. Once setup is
# finished the wizard is closed, so its steps refuse before any token is read:
# test_setup_wizard sends them without one while the wizard is open. A stale
# sign-in form gets the sign-in page back rather than a 403 (decisions.md #43).
ELSEWHERE = {path for path in (r.path for r in _all_routes() if "POST" in r.methods)
             if path.startswith("/setup/")} - PRE_AUTH_TOKEN | {"/login"}

POSTS = {r.path: r for r in _all_routes() if "POST" in r.methods}
SESSION_CHECKED = sorted(set(POSTS) - set(NO_SESSION_TOKEN) - PRE_AUTH_TOKEN - ELSEWHERE)


def test_every_post_route_checks_a_token_or_says_why_not():
    """Read off the handlers, and held to the lists above: a route that loses
    its check stops reading as checked, and shows up here."""
    found = {path: _token_check(route) for path, route in POSTS.items()}

    assert {p for p, k in found.items() if k == "none"} == set(NO_SESSION_TOKEN)
    assert {p for p, k in found.items() if k == "pre-auth"} == PRE_AUTH_TOKEN
    assert {found[p] for p in ELSEWHERE} == {"session"}
    assert len(SESSION_CHECKED) >= 70, "the sweep is not empty"


@pytest.mark.parametrize("path", SESSION_CHECKED)
def test_nothing_is_written_without_the_csrf_token(client, session_factory, contained,
                                                  monkeypatch, path):
    """Signed in with every right (a site admin who owns the portfolio), the
    careful request with no token in the form or the header: refused, and
    every table as it was."""
    import test_hostile_input

    monkeypatch.setitem(test_hostile_input.FILES, "/imports-exports/statement/visual",
                        ("statement.pdf", b"%PDF-1.4", "application/pdf"))
    route = POSTS[path]
    world = _signed_in(client, session_factory)
    if path in SCHEDULED:
        client.post("/schedule/save", headers=HTML, follow_redirects=False, data={
            "name": "Regular buys", "interval_days": "28", "amount": "500",
            "brokerage": "9.50", "start_date": WHEN, "tickers": "ACME",
            "_csrf": world.csrf()})
    fields = _fields(route)
    made = {f.name: _fresh(world, f.name) for f in fields if f.where == "path"}
    values = _typed(world, route, world.tick(), made)
    values.setdefault("token", "0" * 32)
    values.setdefault("code", "123456")        # or FastAPI refuses before the handler runs
    before = _everything(session_factory)
    world.csrf = lambda: None              # `_send` adds a token only when there is one

    resp = _send(world, route, fields, values)

    assert resp.status_code == 403, (path, resp.status_code, resp.text[:200])
    assert _everything(session_factory) == before, path


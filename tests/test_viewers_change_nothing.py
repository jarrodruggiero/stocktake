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
"""

from __future__ import annotations

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


@pytest.fixture
def contained(monkeypatch, tmp_path):
    """What test_hostile_input's `sandbox` points away: the config file, the
    formats folder, staging, Yahoo and the restart. Copied rather than
    imported, because it is a fixture."""
    from pathlib import Path

    from test_hostile_input import YAHOO_SAYS
    from test_pricefeed import stub_yf

    from app import configfile, imports_web, lifecycle, statements
    from app import main as main_mod

    stub_yf(monkeypatch, info=dict(YAHOO_SAYS))
    monkeypatch.setattr(statements, "_pdf_text", lambda data: data.decode("utf-8", "replace"))
    monkeypatch.setattr(lifecycle, "request_stop", lambda reason: None)
    real = Path(main_mod.APP_DIR.parent, "tests", "config.test.yaml")
    config = tmp_path / "config.yaml"
    config.write_text(real.read_text())
    monkeypatch.setattr(configfile, "CONFIG_FILE", str(config))
    monkeypatch.setattr(main_mod.settings.imports, "templates_dir", str(tmp_path / "formats"))
    monkeypatch.setattr(imports_web, "STAGING", tmp_path / "staged")


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


@pytest.mark.parametrize("path", WRITES)
def test_a_viewer_is_refused_every_write(client, session_factory, contained, monkeypatch,
                                         path):
    import test_hostile_input

    # The walker leaves the OCR route out (it needs tesseract); a refusal comes
    # before any of that, so any file reaches it.
    monkeypatch.setitem(test_hostile_input.FILES, "/imports-exports/statement/visual",
                        ("statement.pdf", b"%PDF-1.4", "application/pdf"))
    route = next(r for r in _all_routes() if r.path == path and "POST" in r.methods)
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
            PortfolioMember.portfolio_id == world.portfolio_id)).role = "viewer"
        s.get(User, world.user_id).is_admin = False     # a viewer, and nothing more
        s.commit()
    before = _everything(session_factory)

    resp = _send(world, route, fields, values)

    assert resp.status_code == 403, (path, resp.status_code, resp.text[:200])
    assert _everything(session_factory) == before, path
    assert _method(route) == "POST"


def test_every_post_route_is_either_a_write_or_one_a_viewer_may_use():
    posts = {r.path for r in _all_routes() if "POST" in r.methods}
    stale = sorted(set(VIEWER_MAY) - posts)
    assert not stale, f"VIEWER_MAY names routes that no longer exist: {stale}"
    assert len(WRITES) >= 40, "the sweep is not empty"
    assert {"/trade/new", "/schedule/complete", "/schedule/save"} <= set(WRITES)

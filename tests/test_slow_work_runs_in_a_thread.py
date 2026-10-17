"""Slow work in an `async` route runs in a thread, so the app goes on answering.

An async route runs on the event loop, the one thread every other request
needs. OCR there, at up to a minute a page, held them all, /healthz with them,
so the liveness probe could restart the pod mid-import. The setup wizard's
database test was the same failure (test_setup_wizard.py).
"""

from __future__ import annotations

import asyncio
import gc
import time

import httpx

from app import ocr, pagemap, statements
from test_routes import make_login, session_csrf

HTML = {"accept": "text/html"}
SLOW = 1.0


def health_during(app, send, *, cookies=None) -> tuple[float, float]:
    """Seconds until `send` finished, and until /healthz, asked just after
    `send` began, answered."""
    async def both():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver",
                                     cookies=cookies) as http:
            began = time.monotonic()

            async def slow():
                await send(http)
                return time.monotonic() - began

            async def health():
                await asyncio.sleep(0.1)
                await http.get("/healthz")
                return time.monotonic() - began

            return tuple(await asyncio.gather(slow(), health()))

    # A full garbage collection, late in the suite, stopped the loop for 0.7 s
    # on a CI runner. That is not the slow work holding it, so none runs here.
    enabled = gc.isenabled()
    gc.disable()
    try:
        return asyncio.run(both())
    finally:
        if enabled:
            gc.enable()


def _slowly(answer):
    def slow(*_args, **_kwargs):
        time.sleep(SLOW)
        if isinstance(answer, Exception):
            raise answer
        return answer
    return slow


def _upload(path: str, token: str):
    async def send(http):
        await http.post(path, headers=HTML, data={"_csrf": token},
                        files={"file": ("scan.pdf", b"%PDF-1.4 fake", "application/pdf")})
    return send


def test_a_scanned_statement_is_read_in_a_thread(client, session_factory, app_module,
                                                 monkeypatch):
    monkeypatch.setattr(statements, "_pdf_text", lambda data: "")
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "read_pdf", _slowly("ALPHA paid 104.70 on 15 July 2026"))
    make_login(client, session_factory)

    took, health_took = health_during(
        app_module.app, _upload("/imports-exports/statement", session_csrf(session_factory)),
        cookies=client.cookies)

    assert took >= SLOW
    assert health_took < 0.6, "the health check waited for the OCR"


def test_a_statement_is_drawn_for_the_visual_mapper_in_a_thread(client, session_factory,
                                                                app_module, monkeypatch):
    monkeypatch.setattr(pagemap, "_render", _slowly(ValueError("not a PDF")))
    make_login(client, session_factory)

    took, health_took = health_during(
        app_module.app,
        _upload("/imports-exports/statement/visual", session_csrf(session_factory)),
        cookies=client.cookies)

    assert took >= SLOW
    assert health_took < 0.6, "the health check waited for the pages to be drawn"


def test_a_chart_preview_is_worked_out_in_a_thread(client, session_factory, app_module,
                                                   monkeypatch):
    """The preview runs the whole chart engine, as often as the builder is
    dragged; the charts page, which runs the same engine, never held the loop."""
    monkeypatch.setattr(app_module.charts_build, "run", _slowly({"series": []}))
    make_login(client, session_factory)

    token = session_csrf(session_factory)

    async def preview(http):
        await http.post("/charts/preview", json={"kind": "line"},
                        headers={"x-csrf-token": token})

    took, health_took = health_during(app_module.app, preview, cookies=client.cookies)

    assert took >= SLOW
    assert health_took < 0.6, "the health check waited for the chart"


def test_a_back_channel_logout_is_verified_in_a_thread(client, app_module, monkeypatch):
    """Its verification fetched the provider's configuration and keys, ten
    seconds' timeout each, on the event loop."""
    conf = app_module.settings.auth.oidc
    for name, value in (("enabled", True), ("issuer", "https://idp.example.test"),
                        ("client_id", "stocktake"), ("client_secret", "a-secret"),
                        ("redirect_uri", "https://app.example.test/login/oidc/callback")):
        monkeypatch.setattr(conf, name, value)
    monkeypatch.setattr(app_module.oidc, "verify_logout_token",
                        _slowly(app_module.oidc.OidcError("not verified")))

    async def logout(http):
        await http.post("/oidc/backchannel-logout", data={"logout_token": "x.y.z"})

    took, health_took = health_during(app_module.app, logout)

    assert took >= SLOW
    assert health_took < 0.6, "the health check waited for the verification"


def test_a_password_is_checked_in_a_thread(client, session_factory, app_module, monkeypatch):
    """Argon2 takes tens of milliseconds by design, for unknown addresses too,
    and every sign-in spent them on the one thread every other request
    needs."""
    from test_routes import pre_auth_csrf

    make_login(client, session_factory)
    client.post("/logout", data={"_csrf": session_csrf(session_factory)}, follow_redirects=False)
    token = pre_auth_csrf(client)
    monkeypatch.setattr(app_module.auth, "verify_password", _slowly(False))

    async def sign_in(http):
        await http.post("/login", headers=HTML, data={
            "email": "user@example.test", "password": "wrong", "_csrf": token})

    took, health_took = health_during(app_module.app, sign_in, cookies=client.cookies)

    assert took >= SLOW
    assert health_took < 0.6, "the health check waited for the password"


def test_no_async_route_runs_argon2_on_the_event_loop(app_module):
    """Every handler that checks or makes a password hash, read off its
    syntax tree: an `async def` that calls one directly holds the loop for as
    long as argon2 takes. `asyncio.to_thread(auth.verify_password, …)` names
    it without calling it, and passes."""
    import ast
    import inspect
    import textwrap

    argon2 = {"verify_password", "hash_password"}
    found = []
    for route in app_module.app.routes:
        handler = getattr(route, "endpoint", None)
        if handler is None or not inspect.iscoroutinefunction(handler):
            continue
        tree = ast.parse(textwrap.dedent(inspect.getsource(handler)))
        calls = {node.func.attr if isinstance(node.func, ast.Attribute)
                 else getattr(node.func, "id", "")
                 for node in ast.walk(tree) if isinstance(node, ast.Call)}
        found += [route.path for name in calls & argon2]

    assert found == []

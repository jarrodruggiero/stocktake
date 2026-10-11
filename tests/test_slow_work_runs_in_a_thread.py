"""Slow work in an `async` route runs in a thread, so the app goes on answering.

An async route runs on the event loop, the one thread every other request
needs. OCR there, at up to a minute a page, held them all, /healthz with them,
so the liveness probe could restart the pod mid-import. The setup wizard's
database test was the same failure (test_setup_wizard.py).
"""

from __future__ import annotations

import asyncio
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

    return asyncio.run(both())


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

    async def preview(http):
        await http.post("/charts/preview", json={"kind": "line"})

    took, health_took = health_during(app_module.app, preview, cookies=client.cookies)

    assert took >= SLOW
    assert health_took < 0.6, "the health check waited for the chart"

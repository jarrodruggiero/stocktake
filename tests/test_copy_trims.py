"""Words taken off pages people already understand, kept off by a test.

The sign-in page's recovery link explained itself twice, and the charts page
explained how to drag a card. Each is a label now, or nothing.
"""

from __future__ import annotations

import re

from test_routes import make_login

HTML = {"accept": "text/html"}


def test_the_sign_in_page_says_recover_account(client, session_factory):
    make_login(client, session_factory)
    client.cookies.clear()
    page = client.get("/login", headers=HTML).text
    link = re.search(r'<a href="/login/recover">([^<]+)</a>', page)
    assert link and link.group(1) == "Recover account", link
    assert "recovery codes" not in page.lower()


def test_the_charts_page_does_not_explain_dragging(client, session_factory):
    make_login(client, session_factory)
    page = client.get("/charts", headers=HTML).text
    assert "Drag a card" not in page

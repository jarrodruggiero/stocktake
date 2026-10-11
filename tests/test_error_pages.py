"""A refusal or a fault, as a person in a browser meets it.

There was no HTML handler at all: 107 `HTTPException`s answered a browser
with `{"detail": ...}`, a form missing a field with FastAPI's list of
errors, and a fault with a bare "Internal Server Error". A page's own
fetch() calls and the API keep their JSON.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from test_routes import make_login, session_csrf

HTML = {"accept": "text/html"}
NOT_A_TICKER = {"report": "holdings", "fmt": "csv", "ticker": "İ" * 10}


def test_a_page_that_is_not_there_is_a_page(client, session_factory):
    make_login(client, session_factory)

    resp = client.get("/no-such-page", headers=HTML)

    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("text/html")
    assert "<h1>Not found</h1>" in resp.text
    assert "There&#39;s nothing at this address." in resp.text
    # Somewhere to go that says where it is (decisions.md #40).
    assert '<a class="backlink" href="/">' in resp.text


def test_a_refusal_is_a_page_giving_the_reason(client, session_factory):
    make_login(client, session_factory)

    resp = client.get("/export/download", params=NOT_A_TICKER, headers=HTML)

    assert resp.status_code == 400
    assert "<h1>That didn&#39;t work</h1>" in resp.text
    assert "That is not a ticker" in resp.text


def test_a_refusal_keeps_its_own_headers(client, session_factory):
    make_login(client, session_factory)

    resp = client.post("/charts", headers=HTML)

    assert resp.status_code == 405
    assert "GET" in resp.headers["allow"]
    assert "This address doesn&#39;t take that kind of request." in resp.text


def test_a_fetch_and_the_api_still_get_json(client, session_factory):
    make_login(client, session_factory)

    fetched = client.get("/export/download", params=NOT_A_TICKER)
    api = client.get("/api/v1/portfolio", headers={**HTML, "Authorization": "Bearer nope"})

    assert fetched.json() == {"detail": "that is not a ticker"}
    assert api.status_code == 401
    assert api.headers["content-type"].startswith("application/json")


def test_a_form_missing_a_field_is_a_page_naming_it(client, session_factory):
    make_login(client, session_factory)

    resp = client.post("/imports-exports/statement/commit", headers=HTML,
                       data={"_csrf": session_csrf(session_factory), "ticker": "ACME",
                             "net_amount": "10.00"})

    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("text/html")
    assert "Missing or unreadable: payment date." in resp.text


def test_a_fault_is_a_page_that_tells_nothing_of_the_code(client, monkeypatch, app_module):
    def broken(db):
        raise RuntimeError("the inner workings")

    monkeypatch.setattr(app_module, "_users_exist", broken)
    quiet = TestClient(app_module.app, raise_server_exceptions=False)

    page = quiet.get("/login", headers=HTML)
    plain = quiet.get("/login")

    assert page.status_code == 500
    assert "<h1>Something went wrong</h1>" in page.text
    assert "inner workings" not in page.text
    assert (plain.status_code, plain.text) == (500, "Internal Server Error")

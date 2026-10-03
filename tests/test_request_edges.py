"""What any request can carry, refused once at the edge rather than per field.

`test_hostile_input.py` walks every route with these and more, but it is
deselected by default. These are the same guarantees in the default run, plus
the one the walker cannot see: an upload is bytes, and must still get through.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from test_api import bearer, issue_key

import factories as fac
from app.models import ROW_ID_MAX, User
from test_routes import bind_to_only_portfolio, make_login, session_csrf

HTML = {"accept": "text/html"}
NUL_REFUSED = "Text can't contain a NUL character."


@pytest.fixture
def signed_in(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        portfolio = bind_to_only_portfolio(s)
        fac.make_instrument(s, "ACME", asset_class="share")
        s.commit()
        return portfolio.id


@pytest.mark.parametrize("how", ["form", "json", "query", "path"])
def test_a_nul_is_refused_wherever_it_arrives(client, session_factory, signed_in, how):
    """Postgres refuses text with a NUL even to look something up, so this was
    a 500 wherever one reached a query, and stored junk on SQLite."""
    csrf = {"x-csrf-token": session_csrf(session_factory)}
    if how == "form":
        resp = client.post("/portfolio/new", data={"name": "a\x00b", **csrf}, headers=HTML)
    elif how == "json":
        resp = client.post("/charts/save", headers=csrf,
                           json={"name": "a\x00b", "spec": {}})
    elif how == "query":
        resp = client.get("/holdings/lookup?ticker=AC%00ME", headers=HTML)
    else:
        resp = client.get("/holding/AC%00ME", headers=HTML)

    assert resp.status_code == 400
    assert resp.text == NUL_REFUSED


def test_an_upload_may_carry_nul_bytes(client, session_factory, signed_in):
    """A file is bytes, and a PDF is full of NULs: only a form or JSON body is
    read for one, so an upload reaches its route and is judged there."""
    csv = b"Trade Date,Buy/Sell,Code,Units,Price,Brokerage\n02/07/2026,Buy,ACME,1,1.00,0\x00\n"

    resp = client.post("/imports-exports/csv", headers=HTML,
                       data={"broker": "testbroker", "_csrf": session_csrf(session_factory)},
                       files={"file": ("trades.csv", csv, "text/csv")})

    assert resp.text != NUL_REFUSED


def test_a_form_without_one_still_reaches_its_route(client, session_factory, signed_in):
    """The body is read for the check, and the route must still get all of it."""
    resp = client.post("/portfolio/new", headers=HTML, follow_redirects=False,
                       data={"name": "Second portfolio",
                             "_csrf": session_csrf(session_factory)})

    assert resp.status_code == 303


@pytest.mark.parametrize("trade_id", [ROW_ID_MAX + 1, 2**63])
def test_an_id_no_row_can_have_is_refused_not_looked_up(
        client, session_factory, signed_in, trade_id):
    """Past ROW_ID_MAX it names nothing, and from 2**63 the lookup itself was a
    500: an OverflowError on SQLite, "integer out of range" on Postgres."""
    resp = client.get(f"/trade/{trade_id}/edit", headers=HTML)

    assert resp.status_code == 422


def test_the_largest_id_a_row_can_have_is_still_looked_up(client, session_factory, signed_in):
    resp = client.get(f"/trade/{ROW_ID_MAX}/edit", headers=HTML)

    assert resp.status_code == 404


def test_a_bare_nan_in_api_json_is_a_422_not_a_500(client, session_factory, signed_in):
    """Python's JSON parser accepts NaN. FastAPI's 422 echoed it back and could
    not encode it, so a request rightly refused became a 500."""
    with session_factory() as s:
        user_id = s.scalars(select(User.id)).one()
    raw = issue_key(session_factory, signed_in, scopes="read,write", created_by=user_id)
    body = json.dumps({"ticker": "ACME", "type": "buy", "date": "2026-07-01",
                       "units": float("nan"), "unit_price": "1.00"})

    resp = client.post("/api/v1/trades", content=body,
                       headers={**bearer(raw), "content-type": "application/json"})

    assert resp.status_code == 422
    assert "nan" in resp.text

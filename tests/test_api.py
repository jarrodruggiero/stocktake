"""/api/v1 — the machine surface the budgeting and retirement apps will use.

Two properties matter more than the response shapes: a key is the *only* way
in (the login middleware deliberately waves /api/ through, so api.py must
refuse on its own), and a key can never see past its own portfolio.
"""

from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal

import pytest
from freezegun import freeze_time
from sqlalchemy import select

import factories as fac
import fixture_portfolio as ref
from app import auth as auth_mod
from app import queries, tenancy
from app.models import ApiKey, Dividend, Trade

READ_ENDPOINTS = ["/api/v1/portfolio", "/api/v1/holdings", "/api/v1/trades",
                  "/api/v1/dividends", "/api/v1/schedule"]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def issue_key(session_factory, portfolio_id: int, *, scopes: str = "read",
              created_by: int | None = None, revoked: bool = False) -> str:
    """Mint a key the way the Members page does, and hand back the raw value.
    Only its hash is ever stored, so this is the one moment it exists."""
    raw, key_hash, prefix = auth_mod.new_api_key()
    with session_factory() as s:
        key = ApiKey(portfolio_id=portfolio_id, name="test key", key_hash=key_hash,
                     prefix=prefix, scopes=scopes, created_by=created_by)
        if revoked:
            import datetime as dt
            key.revoked_at = dt.datetime.now(dt.timezone.utc)
        s.add(key)
        s.commit()
    return raw


def bearer(raw: str) -> dict:
    return {"Authorization": f"Bearer {raw}"}


@contextmanager
def bound(session_factory, portfolio_id: int, user_id: int | None = None):
    session = session_factory()
    try:
        tenancy.bind(session, portfolio_id, user_id)
        yield session
    finally:
        session.close()


@pytest.fixture
def furnished(client, session_factory):
    """One portfolio holding the reference data, plus its owner."""
    with session_factory() as s:
        user = fac.make_user(s, "api@example.test")
        portfolio = fac.make_portfolio(s, "API portfolio", owner=user)
        s.commit()
        tenancy.bind(s, portfolio.id, user.id)
        ref.build_reference(s)
        ids = (portfolio.id, user.id)
    return ids


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("path", READ_ENDPOINTS)
def test_no_key_is_401_not_a_login_redirect(client, path):
    """/api/ is exempt from the login middleware, so the router itself has to
    refuse — a redirect here would hand an HTML page to a JSON client."""
    resp = client.get(path, follow_redirects=False)

    assert resp.status_code == 401


@pytest.mark.parametrize("path", READ_ENDPOINTS)
def test_a_bogus_key_is_401(client, path):
    resp = client.get(path, headers=bearer("pfk_deadbeef_not-a-real-key"))

    assert resp.status_code == 401


def test_a_revoked_key_stops_working(client, session_factory, furnished):
    portfolio_id, user_id = furnished
    raw = issue_key(session_factory, portfolio_id, created_by=user_id, revoked=True)

    assert client.get("/api/v1/portfolio", headers=bearer(raw)).status_code == 401


def test_the_x_api_key_header_is_accepted_too(client, session_factory, furnished):
    portfolio_id, user_id = furnished
    raw = issue_key(session_factory, portfolio_id, created_by=user_id)

    assert client.get("/api/v1/portfolio", headers={"X-API-Key": raw}).status_code == 200


def test_using_a_key_records_when_it_was_last_used(client, session_factory, furnished):
    portfolio_id, user_id = furnished
    raw = issue_key(session_factory, portfolio_id, created_by=user_id)

    client.get("/api/v1/portfolio", headers=bearer(raw))

    with session_factory() as s:
        assert s.scalars(select(ApiKey)).one().last_used_at is not None


# --------------------------------------------------------------------------- #
# Scopes
# --------------------------------------------------------------------------- #

def test_a_read_only_key_cannot_write(client, session_factory, furnished):
    portfolio_id, user_id = furnished
    raw = issue_key(session_factory, portfolio_id, scopes="read", created_by=user_id)
    body = {"ticker": "ALPHA", "type": "buy", "date": "2026-07-01", "units": "1",
            "unit_price": "10.00"}

    assert client.post("/api/v1/trades", json=body, headers=bearer(raw)).status_code == 403
    assert client.post("/api/v1/dividends",
                       json={"ticker": "ALPHA", "date": "2026-07-01",
                             "cash_amount": "5.00"},
                       headers=bearer(raw)).status_code == 403


def test_a_write_key_can_record_a_trade(client, session_factory, furnished):
    portfolio_id, user_id = furnished
    raw = issue_key(session_factory, portfolio_id, scopes="read,write", created_by=user_id)

    resp = client.post("/api/v1/trades",
                       json={"ticker": "ALPHA", "type": "buy", "date": "2026-07-01",
                             "units": "10", "unit_price": "14.00", "brokerage": "9.50"},
                       headers=bearer(raw))

    assert resp.status_code == 201
    with bound(session_factory, portfolio_id) as s:
        latest = s.scalars(select(Trade).order_by(Trade.id.desc())).first()
        assert latest.quantity == 10
        assert latest.note == "via API"


# --------------------------------------------------------------------------- #
# Tenancy at the API surface
# --------------------------------------------------------------------------- #

@freeze_time(ref.TODAY)
def test_a_key_never_sees_another_portfolios_holdings(client, session_factory, furnished):
    """The headline guarantee: a key belongs to exactly one portfolio, so
    nothing here takes a portfolio parameter and nothing can widen the view."""
    mine_id, mine_user = furnished
    with session_factory() as s:
        neighbour = fac.make_user(s, "neighbour@example.test")
        theirs = fac.make_portfolio(s, "Neighbour portfolio", owner=neighbour)
        s.commit()
        tenancy.bind(s, theirs.id, neighbour.id)
        # A holding only THEY own, on an instrument only they trade.
        secret = fac.make_instrument(s, "ZEPHYR", name="Zephyr Ltd")
        fac.add_trade(s, secret, "2025-01-06", "buy", 500, "3.00", brokerage="9.50")
        fac.add_prices(s, secret, [("2026-08-01", "4.00"), ("2026-08-02", "4.00")])
        s.commit()

    raw = issue_key(session_factory, mine_id, created_by=mine_user)

    holdings = client.get("/api/v1/holdings", headers=bearer(raw)).json()["holdings"]
    trades = client.get("/api/v1/trades", headers=bearer(raw)).json()["trades"]

    assert "ZEPHYR" not in [h["ticker"] for h in holdings]
    assert "ZEPHYR" not in [t["ticker"] for t in trades]


@freeze_time(ref.TODAY)
def test_the_headline_numbers_agree_with_the_web_dashboard(client, session_factory, furnished):
    """One engine, two surfaces: a reader must never see a different total from
    the one the browser shows."""
    portfolio_id, user_id = furnished
    raw = issue_key(session_factory, portfolio_id, created_by=user_id)

    payload = client.get("/api/v1/portfolio", headers=bearer(raw)).json()

    with bound(session_factory, portfolio_id, user_id) as s:
        open_positions, _ = queries.split_positions(queries.all_holdings(s))
        totals = queries.totals(open_positions)
    assert Decimal(str(payload["cost"])) == totals.cost == ref.TOTAL_COST_AUD
    assert Decimal(str(payload["value"])) == totals.value == ref.TOTAL_VALUE_AUD
    assert payload["currency"] == "AUD"


@freeze_time(ref.TODAY)
def test_holdings_lists_only_open_positions(client, session_factory, furnished):
    portfolio_id, user_id = furnished
    raw = issue_key(session_factory, portfolio_id, created_by=user_id)

    tickers = [h["ticker"]
               for h in client.get("/api/v1/holdings", headers=bearer(raw)).json()["holdings"]]

    assert sorted(tickers) == ["ALPHA", "BETAX", "GAMMA", "OMEGA"]
    assert "ZULU" not in tickers   # sold out entirely


# --------------------------------------------------------------------------- #
# Filters
# --------------------------------------------------------------------------- #

@freeze_time(ref.TODAY)
def test_trades_can_be_filtered_by_date_and_ticker(client, session_factory, furnished):
    portfolio_id, user_id = furnished
    raw = issue_key(session_factory, portfolio_id, created_by=user_id)

    # The reference portfolio's only trades on/after 2024-06-01 are BETAX's
    # sell (2024-06-10) and ZULU's (2024-06-20).
    since = client.get("/api/v1/trades", params={"since": "2024-06-01"},
                       headers=bearer(raw)).json()["trades"]
    assert sorted(t["ticker"] for t in since) == ["BETAX", "ZULU"]

    betax = client.get("/api/v1/trades", params={"ticker": "betax"},
                       headers=bearer(raw)).json()["trades"]
    assert {t["ticker"] for t in betax} == {"BETAX"}
    assert len(betax) == 3        # two buys and one sell


def test_a_malformed_since_is_rejected(client, session_factory, furnished):
    portfolio_id, user_id = furnished
    raw = issue_key(session_factory, portfolio_id, created_by=user_id)

    assert client.get("/api/v1/trades", params={"since": "last-tuesday"},
                      headers=bearer(raw)).status_code == 400
    assert client.get("/api/v1/dividends", params={"since": "last-tuesday"},
                      headers=bearer(raw)).status_code == 400


@freeze_time(ref.TODAY)
def test_dividends_report_whether_they_were_reinvested(client, session_factory, furnished):
    portfolio_id, user_id = furnished
    raw = issue_key(session_factory, portfolio_id, created_by=user_id)

    rows = client.get("/api/v1/dividends", headers=bearer(raw)).json()["dividends"]

    # ALPHA's 2021 distribution bought units; the 2022 one was taken as cash.
    by_date = {r["date"]: r for r in rows}
    assert by_date["2021-07-01"]["reinvested"] is True
    assert by_date["2022-07-01"]["reinvested"] is False


# --------------------------------------------------------------------------- #
# Financial years
# --------------------------------------------------------------------------- #

@freeze_time(ref.TODAY)
def test_the_fy_endpoint_agrees_with_the_report(client, session_factory, furnished):
    from app import fyreport

    portfolio_id, user_id = furnished
    raw = issue_key(session_factory, portfolio_id, created_by=user_id)

    payload = client.get(f"/api/v1/fy/{ref.FY2024}", headers=bearer(raw)).json()

    assert payload["fy"] == "FY23/24"
    assert payload["start"] == "2023-07-01"
    assert payload["end"] == "2024-06-30"
    assert Decimal(str(payload["cgt"]["net_capital_gain"])) == ref.FY2024_NET_CAPITAL_GAIN
    with bound(session_factory, portfolio_id, user_id) as s:
        assert fyreport.fy_cgt(s, ref.FY2024).net_capital_gain == ref.FY2024_NET_CAPITAL_GAIN


@pytest.mark.parametrize("year", [1999, 2101])
def test_a_year_outside_the_supported_range_is_rejected(client, session_factory, furnished,
                                                        year):
    portfolio_id, user_id = furnished
    raw = issue_key(session_factory, portfolio_id, created_by=user_id)

    assert client.get(f"/api/v1/fy/{year}", headers=bearer(raw)).status_code == 400


# --------------------------------------------------------------------------- #
# Plan
# --------------------------------------------------------------------------- #

@freeze_time(ref.TODAY)
def test_the_plan_endpoint_returns_the_rotation_and_what_is_next(client, session_factory,
                                                                furnished):
    from app.models import InvestmentPlan, InvestmentPlanEntry

    portfolio_id, user_id = furnished
    with bound(session_factory, portfolio_id, user_id) as s:
        plan = tenancy.owned(s, InvestmentPlan(name="Regular buys", interval_days=28,
                                               amount=Decimal("500"),
                                               start_date=fac.d("2026-07-06")))
        s.add(plan)
        s.flush()
        alpha = s.scalars(select(fac.Instrument).where(fac.Instrument.ticker == "ALPHA")).one()
        betax = s.scalars(select(fac.Instrument).where(fac.Instrument.ticker == "BETAX")).one()
        s.add(InvestmentPlanEntry(plan_id=plan.id, position=0, instrument_id=alpha.id))
        s.add(InvestmentPlanEntry(plan_id=plan.id, position=1, instrument_id=betax.id))
        s.commit()

    raw = issue_key(session_factory, portfolio_id, created_by=user_id)
    payload = client.get("/api/v1/schedule", headers=bearer(raw)).json()

    assert payload["plan"]["rotation"] == ["ALPHA", "BETAX"]
    assert payload["plan"]["interval_days"] == 28
    # The anchor date IS the first buy, so that is what comes next.
    assert payload["upcoming"][0] == {"ticker": "ALPHA", "due_date": "2026-07-06",
                                      "amount": 500.0}


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #

@freeze_time(ref.TODAY)
def test_a_future_dated_trade_is_rejected(client, session_factory, furnished):
    portfolio_id, user_id = furnished
    raw = issue_key(session_factory, portfolio_id, scopes="read,write", created_by=user_id)

    resp = client.post("/api/v1/trades",
                       json={"ticker": "ALPHA", "type": "buy", "date": "2026-09-01",
                             "units": "1", "unit_price": "10.00"},
                       headers=bearer(raw))

    assert resp.status_code == 400


@freeze_time(ref.TODAY)
def test_a_sell_beyond_the_units_held_is_refused(client, session_factory, furnished):
    """Same guard as the web form: a negative balance would later break the FY
    report's FIFO matcher, so it is refused at the door."""
    portfolio_id, user_id = furnished
    raw = issue_key(session_factory, portfolio_id, scopes="read,write", created_by=user_id)

    # GAMMA holds 100 units; 500 cannot be sold.
    resp = client.post("/api/v1/trades",
                       json={"ticker": "GAMMA", "type": "sell", "date": "2026-07-01",
                             "units": "500", "unit_price": "5.00"},
                       headers=bearer(raw))

    assert resp.status_code == 409


def test_an_unknown_ticker_is_a_404(client, session_factory, furnished):
    portfolio_id, user_id = furnished
    raw = issue_key(session_factory, portfolio_id, scopes="read,write", created_by=user_id)

    resp = client.post("/api/v1/trades",
                       json={"ticker": "NOSUCH", "type": "buy", "date": "2026-07-01",
                             "units": "1", "unit_price": "1.00"},
                       headers=bearer(raw))

    assert resp.status_code == 404


@freeze_time(ref.TODAY)
def test_an_identical_dividend_is_refused_as_a_duplicate(client, session_factory, furnished):
    portfolio_id, user_id = furnished
    raw = issue_key(session_factory, portfolio_id, scopes="read,write", created_by=user_id)
    body = {"ticker": "ALPHA", "date": "2026-07-01", "cash_amount": "42.00"}

    assert client.post("/api/v1/dividends", json=body, headers=bearer(raw)).status_code == 201
    assert client.post("/api/v1/dividends", json=body, headers=bearer(raw)).status_code == 409

    with bound(session_factory, portfolio_id) as s:
        same = s.scalars(select(Dividend).where(Dividend.date == fac.d("2026-07-01"))).all()
        assert len(same) == 1

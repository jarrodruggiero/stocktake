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
from app.models import ApiKey, Dividend, Instrument, Trade

READ_ENDPOINTS = ["/api/v1/portfolio", "/api/v1/holdings", "/api/v1/trades",
                  "/api/v1/dividends", "/api/v1/schedule"]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def issue_key(session_factory, portfolio_id: int | list[int], *, created_by: int | None,
              scopes: str = "read", revoked: bool = False) -> str:
    """Mint a key the way the profile page does, and hand back the raw value.
    Only its hash is ever stored, so this is the one moment it exists.

    `portfolio_id` is one portfolio or several. `created_by` has no default: a
    key works only while the person who made it still has access
    (auth.key_reach), so a test has to say who that is — None is a key nobody
    vouches for, and it is refused."""
    from app.models import Portfolio

    ids = portfolio_id if isinstance(portfolio_id, list) else [portfolio_id]
    raw, key_hash, prefix = auth_mod.new_api_key()
    with session_factory() as s:
        key = ApiKey(name="test key", key_hash=key_hash, prefix=prefix, scopes=scopes,
                     created_by=created_by,
                     portfolios=[s.get(Portfolio, pid) for pid in ids])
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


# --------------------------------------------------------------------------- #
# A key can do no more than its creator can do now — GHSA-cjcx-g69x-63jq.
# They saw the raw key when it was issued, so a key that outlived their access
# was their access, kept.
# --------------------------------------------------------------------------- #

def _set_active(session_factory, user_id: int, active: bool) -> None:
    from app.models import User
    with session_factory() as s:
        s.get(User, user_id).is_active = active
        s.commit()


def _set_role(session_factory, portfolio_id: int, user_id: int, role: str | None) -> None:
    """Change someone's role, or remove them with None."""
    from app.models import PortfolioMember
    with session_factory() as s:
        member = s.scalars(select(PortfolioMember).where(
            PortfolioMember.portfolio_id == portfolio_id,
            PortfolioMember.user_id == user_id)).one()
        if role is None:
            s.delete(member)
        else:
            member.role = role
        s.commit()


def test_a_key_stops_when_its_creator_is_deactivated(client, session_factory, furnished):
    portfolio_id, user_id = furnished
    raw = issue_key(session_factory, portfolio_id, created_by=user_id)
    assert client.get("/api/v1/portfolio", headers=bearer(raw)).status_code == 200

    _set_active(session_factory, user_id, False)
    stopped = client.get("/api/v1/portfolio", headers=bearer(raw))

    assert stopped.status_code == 401
    assert "no longer has an active account" in stopped.json()["detail"]


def test_reactivating_the_creator_brings_their_keys_back(client, session_factory, furnished):
    """Nothing was revoked: the key follows its creator's access, both ways."""
    portfolio_id, user_id = furnished
    raw = issue_key(session_factory, portfolio_id, created_by=user_id)
    _set_active(session_factory, user_id, False)

    _set_active(session_factory, user_id, True)

    assert client.get("/api/v1/portfolio", headers=bearer(raw)).status_code == 200


def test_a_key_stops_when_its_creator_leaves_the_portfolio(client, session_factory, furnished):
    """Removed from this portfolio, still an active account, and still an owner
    of a portfolio of their own — membership HERE is what counts. The other
    owner's key is untouched."""
    portfolio_id, owner_id = furnished
    with session_factory() as s:
        from app.models import Portfolio
        second = fac.make_user(s, "second@example.test")
        fac.add_member(s, s.get(Portfolio, portfolio_id), second, role="owner")
        fac.make_portfolio(s, "Their own", owner=second)
        s.commit()
        second_id = second.id
    theirs = issue_key(session_factory, portfolio_id, created_by=second_id)
    mine = issue_key(session_factory, portfolio_id, created_by=owner_id)

    _set_role(session_factory, portfolio_id, second_id, None)
    refused = client.get("/api/v1/portfolio", headers=bearer(theirs))

    # 403, not 401: the key itself is fine and still reaches whatever else its
    # creator belongs to — just not this one.
    assert refused.status_code == 403
    assert "no longer has access" in refused.json()["detail"]
    assert client.get("/api/v1/portfolio", headers=bearer(mine)).status_code == 200


def test_a_write_key_reads_only_once_its_creator_cannot_write(
        client, session_factory, furnished):
    portfolio_id, user_id = furnished
    raw = issue_key(session_factory, portfolio_id, scopes="read,write", created_by=user_id)

    _set_role(session_factory, portfolio_id, user_id, "viewer")
    refused = client.post("/api/v1/trades", headers=bearer(raw),
                          json={"ticker": "ALPHA", "type": "buy", "date": "2026-07-01",
                                "units": "1", "unit_price": "10.00"})

    assert refused.status_code == 403
    assert "can no longer write" in refused.json()["detail"]
    assert client.get("/api/v1/portfolio", headers=bearer(raw)).status_code == 200


def test_a_write_key_still_writes_for_a_member(client, session_factory, furnished):
    """Owner to member is not a loss of write access, so the key keeps it."""
    portfolio_id, user_id = furnished
    raw = issue_key(session_factory, portfolio_id, scopes="read,write", created_by=user_id)

    _set_role(session_factory, portfolio_id, user_id, "member")
    resp = client.post("/api/v1/trades", headers=bearer(raw),
                       json={"ticker": "ALPHA", "type": "buy", "date": "2026-07-01",
                             "units": "1", "unit_price": "10.00"})

    assert resp.status_code == 201, resp.text


def test_a_key_nobody_made_is_refused(client, session_factory, furnished):
    """`created_by` is SET NULL if the account is deleted. A key nobody vouches
    for is not a key anybody should be able to use."""
    portfolio_id, _user_id = furnished
    raw = issue_key(session_factory, portfolio_id, created_by=None)

    assert client.get("/api/v1/portfolio", headers=bearer(raw)).status_code == 401


# --------------------------------------------------------------------------- #
# A key reaching several portfolios names one per request — decisions.md #128
# --------------------------------------------------------------------------- #

@pytest.fixture
def two(session_factory, furnished):
    """The furnished portfolio and a second, empty one, both owned by the same
    person. Returns (first_id, second_id, user_id)."""
    from app.models import User
    first, user_id = furnished
    with session_factory() as s:
        second = fac.make_portfolio(s, "Second", owner=s.get(User, user_id)).id
        s.commit()
    return first, second, user_id


def test_a_key_reaching_two_portfolios_must_say_which(client, session_factory, two):
    first, second, user_id = two
    raw = issue_key(session_factory, [first, second], created_by=user_id)

    resp = client.get("/api/v1/holdings", headers=bearer(raw))

    assert resp.status_code == 400
    assert "?portfolio=" in resp.json()["detail"]


def test_naming_a_portfolio_chooses_it(client, session_factory, two):
    first, second, user_id = two
    raw = issue_key(session_factory, [first, second], created_by=user_id)

    for pid in (first, second):
        got = client.get(f"/api/v1/portfolio?portfolio={pid}", headers=bearer(raw))
        assert got.status_code == 200
        assert got.json()["portfolio"]["id"] == pid
    # Only the furnished one holds anything — the data really is that portfolio's.
    assert client.get(f"/api/v1/holdings?portfolio={first}", headers=bearer(raw)).json()
    assert client.get(f"/api/v1/trades?portfolio={second}",
                      headers=bearer(raw)).json()["trades"] == []


@pytest.mark.parametrize("given", ["stranger", "nope", "-1"])
def test_a_portfolio_the_key_was_not_given_is_a_404(client, session_factory, two, given):
    """A real portfolio somebody else owns answers exactly as a made-up id
    does, so a key cannot be used to learn which ids exist."""
    first, _second, user_id = two
    with session_factory() as s:
        stranger = fac.make_user(s, "stranger@example.test")
        theirs = fac.make_portfolio(s, "Theirs", owner=stranger).id
        s.commit()
    raw = issue_key(session_factory, first, created_by=user_id)
    value = str(theirs) if given == "stranger" else given

    resp = client.get(f"/api/v1/portfolio?portfolio={value}", headers=bearer(raw))

    assert resp.status_code == 404


def test_one_portfolio_lost_leaves_the_others_working(client, session_factory, two):
    first, second, user_id = two
    raw = issue_key(session_factory, [first, second], created_by=user_id)

    _set_role(session_factory, second, user_id, None)

    assert client.get(f"/api/v1/portfolio?portfolio={second}",
                      headers=bearer(raw)).status_code == 403
    assert client.get(f"/api/v1/portfolio?portfolio={first}",
                      headers=bearer(raw)).status_code == 200


def test_a_write_names_its_portfolio_too(client, session_factory, two):
    first, second, user_id = two
    raw = issue_key(session_factory, [first, second], scopes="read,write",
                    created_by=user_id)
    body = {"ticker": "ALPHA", "type": "buy", "date": "2026-07-01", "units": "1",
            "unit_price": "10.00"}

    assert client.post("/api/v1/trades", json=body, headers=bearer(raw)).status_code == 400
    assert client.post(f"/api/v1/trades?portfolio={first}", json=body,
                       headers=bearer(raw)).status_code == 201
    with bound(session_factory, second, user_id) as s:
        assert s.scalars(select(Trade)).all() == [], "the write landed in the other one"


def test_the_portfolios_endpoint_lists_what_the_key_reaches_now(client, session_factory, two):
    """How a client learns the ids. One whose creator lost access is left out;
    one where they can only read says so."""
    from app.models import Portfolio, User
    first, second, user_id = two
    with session_factory() as s:
        other = fac.make_user(s, "other@example.test")
        viewing = fac.make_portfolio(s, "Viewing", owner=other)
        fac.add_member(s, viewing, s.get(User, user_id), role="viewer")
        s.commit()
        viewing_id = viewing.id
    raw = issue_key(session_factory, [first, second, viewing_id], scopes="read,write",
                    created_by=user_id)
    _set_role(session_factory, second, user_id, None)

    listed = client.get("/api/v1/portfolios", headers=bearer(raw)).json()["portfolios"]

    with session_factory() as s:
        first_name = s.get(Portfolio, first).name
    assert listed == sorted([
        {"id": first, "name": first_name, "access": "read,write"},
        {"id": viewing_id, "name": "Viewing", "access": "read"},
    ], key=lambda p: p["name"])


def test_the_portfolios_endpoint_still_refuses_a_dead_key(client, session_factory, two):
    first, _second, user_id = two
    raw = issue_key(session_factory, first, created_by=user_id)
    _set_active(session_factory, user_id, False)

    assert client.get("/api/v1/portfolios", headers=bearer(raw)).status_code == 401


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


@freeze_time(ref.TODAY)
def test_a_free_parcel_is_accepted_and_a_negative_price_is_not(
    client, session_factory, furnished
):
    """A price of zero is legitimate — a bonus issue or a reward-plan grant.
    Negative is not, and `units` keeps its own floor either way.

    Here rather than in `test_trade_prefill.py` because the key-minting helpers
    are here; the rule itself is the forms' rule, held in step by
    `test_api_parity`.
    """
    portfolio_id, user_id = furnished
    raw = issue_key(session_factory, portfolio_id, scopes="read,write",
                    created_by=user_id)

    def record(**overrides):
        body = {"ticker": "ALPHA", "type": "buy", "date": "2026-07-01",
                "units": "10", "unit_price": "0"}
        body.update(overrides)
        return client.post("/api/v1/trades", json=body, headers=bearer(raw))

    assert record().status_code == 201
    assert record(unit_price="-1").status_code == 422
    assert record(units="0").status_code == 422


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


@freeze_time(ref.TODAY)
def test_the_fy_endpoint_names_what_its_totals_leave_out(client, session_factory, furnished):
    """A sale in a currency with no exchange rate anywhere stays out of the
    gain and is named, rather than counted as if a pound were a dollar."""
    portfolio_id, user_id = furnished
    with bound(session_factory, portfolio_id, user_id) as s:
        nova = fac.make_instrument(s, "NOVA", exchange="LSE", currency="GBP",
                                   asset_class="share")
        fac.add_trade(s, nova, "2023-08-01", "buy", 100, "10.00")
        fac.add_trade(s, nova, "2023-09-01", "sell", 100, "12.00")
        s.commit()
    raw = issue_key(session_factory, portfolio_id, created_by=user_id)

    payload = client.get(f"/api/v1/fy/{ref.FY2024}", headers=bearer(raw)).json()

    assert payload["withheld"] == ["NOVA"]
    assert Decimal(str(payload["cgt"]["net_capital_gain"])) == ref.FY2024_NET_CAPITAL_GAIN


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
    assert payload["upcoming"][0] == {"ticker": "ALPHA", "exchange": "ASX",
                                      "due_date": "2026-07-06", "amount": 500.0}


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


# --------------------------------------------------------------------------- #
# Figures too big for their column
# --------------------------------------------------------------------------- #

def _trade_count(session_factory, portfolio_id: int) -> int:
    with bound(session_factory, portfolio_id) as s:
        return len(s.scalars(select(Trade)).all())


@pytest.mark.parametrize("field, value", [
    ("units", "1E+999999"), ("units", "1000000000000"),
    ("unit_price", "1E+999999"), ("brokerage", "1E+999999"), ("fx_rate", "1E+999999"),
])
def test_a_trade_with_a_figure_too_big_for_its_column_is_refused(
        client, session_factory, furnished, field, value):
    """pydantic refuses NaN and Infinity, but `1E+999999` is finite: it used to
    save as Infinity and then fail every read of the portfolio."""
    portfolio_id, user_id = furnished
    raw = issue_key(session_factory, portfolio_id, scopes="read,write", created_by=user_id)
    body = {"ticker": "ALPHA", "type": "buy", "date": "2026-07-01", "units": "1",
            "unit_price": "10.00", field: value}
    before = _trade_count(session_factory, portfolio_id)

    resp = client.post("/api/v1/trades", json=body, headers=bearer(raw))

    assert resp.status_code == 422
    assert _trade_count(session_factory, portfolio_id) == before
    assert client.get("/api/v1/portfolio", headers=bearer(raw)).status_code == 200


@pytest.mark.parametrize("field, value", [
    ("cash_amount", "1E+999999"), ("cash_amount", "10000000000"),
    ("franking_credits", "1E+999999"), ("fx_rate", "1E+999999"),
])
def test_a_dividend_with_a_figure_too_big_for_its_column_is_refused(
        client, session_factory, furnished, field, value):
    portfolio_id, user_id = furnished
    raw = issue_key(session_factory, portfolio_id, scopes="read,write", created_by=user_id)
    body = {"ticker": "ALPHA", "date": "2026-07-01", "cash_amount": "10.00", field: value}

    resp = client.post("/api/v1/dividends", json=body, headers=bearer(raw))

    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# The values a client really sends
# --------------------------------------------------------------------------- #
# A mutation run found these unguarded: the refusal of an over-sale counted
# every holding's units (the test sold more than the whole portfolio held), a
# rate of 0.65 or a 50-cent dividend could be refused, an AUD row's rate and
# a US row's could be anything, and a trade dated today could be turned away.

@freeze_time(ref.TODAY)
def test_a_sale_is_held_to_its_own_holdings_units(client, session_factory, furnished):
    """GAMMA holds 100. 150 is more than that and less than the portfolio holds
    across every instrument, which is the case a mixed-up count would allow."""
    portfolio_id, user_id = furnished
    raw = issue_key(session_factory, portfolio_id, scopes="read,write", created_by=user_id)

    resp = client.post("/api/v1/trades", headers=bearer(raw), json={
        "ticker": "GAMMA", "type": "sell", "date": "2026-07-01", "units": "150",
        "unit_price": "5.00"})

    assert resp.status_code == 409
    with bound(session_factory, portfolio_id) as s:
        others = sum(t.quantity if t.type != "sell" else -t.quantity
                     for t in s.scalars(select(Trade)) if t.instrument.ticker != "GAMMA")
        assert others > 150, "the premise: the other holdings could have covered it"


@freeze_time(ref.TODAY)
@pytest.mark.parametrize("ticker, rate, stored", [("NOVA", "0.65", Decimal("0.65")),
                                                  ("NOVA", None, None),
                                                  ("ALPHA", "0.65", Decimal(1))])
def test_a_trades_rate_is_the_one_sent_and_an_aud_one_is_1(client, session_factory, furnished,
                                                           ticker, rate, stored):
    portfolio_id, user_id = furnished
    raw = issue_key(session_factory, portfolio_id, scopes="read,write", created_by=user_id)
    with bound(session_factory, portfolio_id, user_id) as s:
        if s.scalar(select(Instrument).where(Instrument.ticker == "NOVA")) is None:
            fac.hold(s, fac.make_instrument(s, "NOVA", exchange="NASDAQ", currency="USD"))
            s.commit()

    body = {"ticker": ticker, "type": "buy", "date": str(ref.TODAY), "units": "1",
            "unit_price": "10.00", "brokerage": "0"}
    if rate:
        body["fx_rate"] = rate
    resp = client.post("/api/v1/trades", headers=bearer(raw), json=body)

    assert resp.status_code == 201, resp.text
    with bound(session_factory, portfolio_id) as s:
        latest = s.scalars(select(Trade).order_by(Trade.id.desc())).first()
        assert (latest.fx_rate, latest.date.isoformat()) == (stored, str(ref.TODAY))


@freeze_time(ref.TODAY)
def test_a_small_dividend_with_no_franking_and_a_rate_is_accepted(client, session_factory,
                                                                  furnished):
    portfolio_id, user_id = furnished
    raw = issue_key(session_factory, portfolio_id, scopes="read,write", created_by=user_id)
    with bound(session_factory, portfolio_id, user_id) as s:
        fac.hold(s, fac.make_instrument(s, "NOVA", exchange="NASDAQ", currency="USD"))
        s.commit()

    resp = client.post("/api/v1/dividends", headers=bearer(raw), json={
        "ticker": "NOVA", "date": str(ref.TODAY), "cash_amount": "0.50",
        "franking_credits": "0", "fx_rate": "0.65"})

    assert resp.status_code == 201, resp.text
    with bound(session_factory, portfolio_id) as s:
        row = s.scalars(select(Dividend).order_by(Dividend.id.desc())).first()
        assert (row.cash_amount, row.franking_credits, row.fx_rate, row.note) == (
            Decimal("0.50"), Decimal(0), Decimal("0.65"), "via API")


def test_since_includes_its_own_day(client, session_factory, furnished):
    portfolio_id, user_id = furnished
    raw = issue_key(session_factory, portfolio_id, created_by=user_id)
    with bound(session_factory, portfolio_id) as s:
        first_trade = min(t.date for t in s.scalars(select(Trade))).isoformat()
        first_dividend = min(d.date for d in s.scalars(select(Dividend))).isoformat()

    trades = client.get("/api/v1/trades", params={"since": first_trade},
                        headers=bearer(raw)).json()["trades"]
    dividends = client.get("/api/v1/dividends", params={"since": first_dividend},
                           headers=bearer(raw)).json()["dividends"]

    assert first_trade in {t["date"] for t in trades}
    assert first_dividend in {d["date"] for d in dividends}


def test_the_fy_endpoints_grossed_up_income_is_cash_plus_franking(client, session_factory,
                                                                  furnished):
    portfolio_id, user_id = furnished
    raw = issue_key(session_factory, portfolio_id, created_by=user_id)
    with bound(session_factory, portfolio_id, user_id) as s:
        alpha = s.scalar(select(Instrument).where(Instrument.ticker == "ALPHA"))
        fac.add_dividend(s, alpha, "2024-03-15", "40.00", franking_credits="17.14")
        s.commit()

    income = client.get(f"/api/v1/fy/{ref.FY2024}", headers=bearer(raw)).json()["income"]

    assert income["franking_credits"] > 0, "the premise: there is franking to add"
    assert income["grossed_up"] == pytest.approx(income["cash"] + income["franking_credits"])

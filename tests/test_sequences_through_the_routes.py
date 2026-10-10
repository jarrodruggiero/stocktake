"""Generated sequences of what people do, through the real routes, checked
after every step for what must always hold.

A route test makes one request against a state it set up. What test users ran
into came from sequences instead: a ticker removed while it was in the plan, a
currency corrected after trades were recorded under the old one, a holding
page left behind once its last trade moved away. Hypothesis drives the app
through generated sequences across two portfolios, and shrinks a failing one
to the shortest sequence that still fails.

After every step:

- nothing answered 500, and every page of the active portfolio renders;
- no instrument's units go below zero anywhere in its timeline, whichever route
  wrote the rows — the rule every write route is meant to keep;
- a refused request changed nothing;
- the holdings export lists exactly the open positions the stored trades make;
- nothing done in one portfolio changed the other's rows, except a move into it.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import os
import shutil
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule, run_state_machine_as_test
from sqlalchemy import select, text

import factories as fac
from app import auth as auth_mod
from app import clock, pricefeed, queries, tenancy
from app.models import (
    MARKET_OPEN,
    Dividend,
    HoldingPref,
    Instrument,
    InvestmentPlan,
    Trade,
    User,
    trade_order,
)
from appcore import make_session_factory
from appcore.config import DatabaseSettings
from test_routes import PASSWORD, pre_auth_csrf

HTML = {"accept": "text/html"}
TICKERS = ["ACME", "NOVA", "WIDGET", "ALPHA"]
NEW_TICKERS = ["ZULU", "GAMMA"]
# What a lookup says for a new one, so adding never reaches the network.
YAHOO = {"ZULU.AX": {"longName": "Zulu Ltd", "currency": "AUD", "quoteType": "EQUITY"},
         "GAMMA": {"longName": "Gamma Inc", "currency": "USD", "quoteType": "EQUITY"}}


class Walk(RuleBasedStateMachine):
    def __init__(self, env: SimpleNamespace):
        super().__init__()
        self.env = env
        self.factory = env.fresh()
        app = env.app_module
        self._restore = (app.SessionLocal, app.app.state.session_factory)
        app.SessionLocal = self.factory
        app.app.state.session_factory = self.factory
        for cache in (queries._series_cache, queries._holdings_cache, queries._grouped_cache):
            cache.clear()
        self.client = TestClient(app.app)
        with self.factory() as s:
            user = fac.make_user(s, "walker@example.test",
                                 password_hash=auth_mod.hash_password(PASSWORD))
            first = fac.make_portfolio(s, "First", owner=user)
            second = fac.make_portfolio(s, "Second", owner=user)
            day = clock.today() - dt.timedelta(days=400)
            for ticker, exchange, currency in (("ACME", "ASX", "AUD"),
                                               ("NOVA", "NASDAQ", "USD"),
                                               ("WIDGET", "ASX", "AUD"),
                                               ("ALPHA", "ASX", "AUD")):
                inst = fac.make_instrument(s, ticker, exchange=exchange, currency=currency,
                                           asset_class="share")
                fac.add_prices(s, inst, [(day, "10.00"), (clock.today(), "11.00")])
            fac.add_fx_series(s, "USDAUD", [(day, "1.50"), (clock.today(), "1.55")])
            for ticker, portfolio in (("ACME", first), ("NOVA", first), ("WIDGET", second)):
                inst = s.scalar(select(Instrument).where(Instrument.ticker == ticker))
                fac.hold(s, inst, portfolio_id=portfolio.id)
            s.commit()
            self.user_id, self.portfolios = user.id, [first.id, second.id]
        token = pre_auth_csrf(self.client)
        self.client.post("/login", data={"email": "walker@example.test", "password": PASSWORD,
                                         "_csrf": token}, headers=HTML, follow_redirects=False)
        self.active = self.portfolios[0]
        self._post("/portfolio/switch", {"portfolio_id": str(self.active)})

    def teardown(self):
        app = self.env.app_module
        app.SessionLocal, app.app.state.session_factory = self._restore
        self.factory.kw["bind"].dispose()

    # ------------------------------------------------------------------ #
    # Reading what is stored, across both portfolios
    # ------------------------------------------------------------------ #

    def _rows(self) -> dict:
        with self.factory() as s:
            tenancy.allow_unscoped(s)
            trades = sorted(
                (t.id, t.portfolio_id, t.instrument_id, t.date, t.time or MARKET_OPEN, t.type,
                 t.quantity, t.unit_price, t.brokerage, t.fx_rate)
                for t in s.scalars(select(Trade)))
            dividends = sorted((d.id, d.portfolio_id, d.instrument_id, d.date, d.cash_amount,
                                d.fx_rate) for d in s.scalars(select(Dividend)))
            prefs = sorted((p.portfolio_id, p.instrument_id, p.drp, p.note)
                           for p in s.scalars(select(HoldingPref)))
            plans = sorted((p.portfolio_id, tuple(e.instrument_id for e in p.entries),
                            p.interval_days) for p in s.scalars(select(InvestmentPlan)))
            instruments = sorted((i.id, i.ticker, i.exchange, i.currency, i.asset_class, i.name,
                                  i.yahoo_symbol, i.active) for i in s.scalars(select(Instrument)))
        return {"trades": trades, "dividends": dividends, "prefs": prefs, "plans": plans,
                "instruments": instruments}

    def _mine(self, rows: dict, portfolio: int) -> set[int]:
        """Instrument ids a portfolio has (decisions.md #139)."""
        return ({t[2] for t in rows["trades"] if t[1] == portfolio}
                | {d[2] for d in rows["dividends"] if d[1] == portfolio}
                | {p[1] for p in rows["prefs"] if p[0] == portfolio}
                | {i for p in rows["plans"] if p[0] == portfolio for i in p[1]})

    def _of(self, rows: dict, portfolio: int) -> dict:
        """One portfolio's own rows, rates left out (a currency change resets them
        everywhere, on purpose)."""
        return {"trades": [t[:9] for t in rows["trades"] if t[1] == portfolio],
                "dividends": [d[:5] for d in rows["dividends"] if d[1] == portfolio],
                "prefs": [p for p in rows["prefs"] if p[0] == portfolio],
                "plans": [p for p in rows["plans"] if p[0] == portfolio]}

    def _id(self, ticker: str) -> int | None:
        return next((i[0] for i in self._rows()["instruments"] if i[1] == ticker), None)

    # ------------------------------------------------------------------ #
    # Requests
    # ------------------------------------------------------------------ #

    def _csrf(self) -> str:
        from app.models import UserSession
        with self.factory() as s:
            return s.scalars(select(UserSession).order_by(UserSession.id.desc())).first().csrf_token

    def _post(self, path: str, data: dict):
        resp = self.client.post(path, data={"_csrf": self._csrf(), **data}, headers=HTML,
                                follow_redirects=False)
        assert resp.status_code < 500, (path, data, resp.text[:300])
        return resp

    def _changing(self, path: str, data: dict, *, moves_to: int | None = None) -> bool:
        """Post, and check what a refusal and an acceptance may each change."""
        before = self._rows()
        resp = self._post(path, data)
        after = self._rows()
        accepted = resp.status_code == 303 and "error=" not in resp.headers.get("location", "")
        if not accepted:
            assert after == before, (path, data, resp.status_code, "a refusal changed rows")
        for other in self.portfolios:
            if other not in (self.active, moves_to):
                assert self._of(after, other) == self._of(before, other), (path, data)
        return accepted

    @staticmethod
    def _day(days_ago: int) -> str:
        return (clock.today() - dt.timedelta(days=days_ago)).isoformat()

    # ------------------------------------------------------------------ #
    # What people do
    # ------------------------------------------------------------------ #

    @rule()
    def switch(self):
        self.active = next(p for p in self.portfolios if p != self.active)
        self._post("/portfolio/switch", {"portfolio_id": str(self.active)})

    @rule(ticker=st.sampled_from(TICKERS + NEW_TICKERS), kind=st.sampled_from(["buy", "sell"]),
          days_ago=st.integers(0, 900), units=st.sampled_from(["1", "2.5", "10", "0.0001"]),
          price=st.sampled_from(["0", "1.25", "40"]), brokerage=st.sampled_from(["0", "9.95"]))
    def record(self, ticker, kind, days_ago, units, price, brokerage):
        rows = self._rows()
        inst_id = self._id(ticker)
        listed = inst_id is not None and inst_id in self._mine(rows, self.active)
        data = {"type": kind, "trade_date": self._day(days_ago), "quantity": units,
                "unit_price": price, "brokerage": brokerage}
        if listed:
            data["instrument_id"] = str(inst_id)
        else:
            exchange = "NASDAQ" if ticker in ("NOVA", "GAMMA") else "ASX"
            data.update(instrument_id="new", new_ticker=ticker, new_exchange=exchange,
                        new_asset_class="share",
                        new_currency="USD" if exchange == "NASDAQ" else "AUD")
        held = sum((t[6] if t[5] != "sell" else -t[6] for t in rows["trades"]
                    if t[1] == self.active and t[2] == inst_id), Decimal(0))
        accepted = self._changing("/trade/new", data)
        if kind == "buy" and listed:
            assert accepted, ("a buy of something listed is never refused", data)
        if kind == "sell" and Decimal(units) > held:
            assert not accepted, ("more than is held cannot be sold", data, held)

    @rule(pick=st.integers(0, 10**6), days_ago=st.integers(0, 900),
          units=st.sampled_from(["1", "5", "0.5"]))
    def edit(self, pick, days_ago, units):
        mine = [t for t in self._rows()["trades"] if t[1] == self.active]
        if not mine:
            return
        t = mine[pick % len(mine)]
        self._changing(f"/trade/{t[0]}/edit", {
            "type": t[5], "trade_date": self._day(days_ago), "quantity": units,
            "unit_price": str(t[7]), "brokerage": str(t[8])})

    @rule(pick=st.integers(0, 10**6))
    def delete(self, pick):
        mine = [t for t in self._rows()["trades"] if t[1] == self.active]
        if mine:
            self._changing(f"/trade/{mine[pick % len(mine)][0]}/delete", {})

    @rule(pick=st.integers(0, 10**6))
    def move_everything(self, pick):
        rows = self._rows()
        mine = [t for t in rows["trades"] if t[1] == self.active]
        if not mine:
            return
        inst = mine[pick % len(mine)][2]
        target = next(p for p in self.portfolios if p != self.active)
        picked = [f"trade:{t[0]}" for t in mine if t[2] == inst] + [
            f"dividend:{d[0]}" for d in rows["dividends"] if d[1] == self.active and d[2] == inst]
        self._changing(f"/trade/{mine[pick % len(mine)][0]}/move",
                       {"target": str(target), "row": picked}, moves_to=target)

    @rule(ticker=st.sampled_from(TICKERS + NEW_TICKERS))
    def add_holding(self, ticker):
        exchange = "NASDAQ" if ticker in ("NOVA", "GAMMA") else "ASX"
        self._changing("/holdings/add", {"ticker": ticker, "exchange": exchange,
                                         "asset_class": "share"})

    @rule(ticker=st.sampled_from(TICKERS + NEW_TICKERS), everything=st.booleans())
    def remove_holding(self, ticker, everything):
        inst = self._id(ticker)
        if inst is None:
            return
        if everything:
            self._changing(f"/holdings/{inst}/delete-trades", {})
        self._changing(f"/holdings/{inst}/remove", {})

    @rule(ticker=st.sampled_from(TICKERS + NEW_TICKERS),
          currency=st.sampled_from(["AUD", "USD"]), cls=st.sampled_from(["etf", "share"]))
    def correct_details(self, ticker, currency, cls):
        inst = self._id(ticker)
        if inst is None:
            return
        self._changing(f"/holdings/{inst}/pref", {
            "catalogue": "1", "name": f"{ticker} corrected", "asset_class": cls,
            "currency": currency, "yahoo_symbol": "", "note": "kept", "drp": "1"})

    @rule(picks=st.lists(st.sampled_from(TICKERS + NEW_TICKERS), max_size=3, unique=True),
          interval=st.sampled_from([7, 14, 28]))
    def plan(self, picks, interval):
        self._changing("/schedule/save", {
            "name": "Plan", "interval_days": str(interval), "amount": "500",
            "brokerage": "9.95", "start_date": "", "tickers": ",".join(picks)})

    # ------------------------------------------------------------------ #
    # What must hold after every step
    # ------------------------------------------------------------------ #

    @invariant()
    def no_timeline_goes_below_zero(self):
        rows = self._rows()
        by_holding: dict = {}
        for t in rows["trades"]:
            by_holding.setdefault((t[1], t[2]), []).append(t)
        for key, trades in by_holding.items():
            held = Decimal(0)
            for t in sorted(trades, key=lambda r: trade_order(SimpleNamespace(
                    date=r[3], time=r[4], id=r[0]))):
                held += -t[6] if t[5] == "sell" else t[6]
                assert held >= 0, (key, trades)

    @invariant()
    def every_page_renders(self):
        rows = self._rows()
        tickers = {i[1] for i in rows["instruments"] if i[0] in self._mine(rows, self.active)}
        pages = ["/", "/holdings", "/charts", "/schedule", "/export", "/trade/new"]
        pages += [f"/holding/{t}" for t in sorted(tickers)]
        with self.factory() as s:
            tenancy.bind(s, self.active, self.user_id)
            from app import fyreport
            pages += [f"/?fy={fy}" for fy in fyreport.available_fys(s)]
        for page in pages:
            resp = self.client.get(page, headers=HTML)
            assert resp.status_code == 200, (page, resp.status_code, resp.text[:300])

    @invariant()
    def the_export_lists_the_open_positions(self):
        rows = self._rows()
        units: dict = {}
        for t in rows["trades"]:
            if t[1] == self.active:
                units[t[2]] = units.get(t[2], Decimal(0)) + (-t[6] if t[5] == "sell" else t[6])
        active = {i[0]: i for i in rows["instruments"]}
        want = {active[i][1]: u.quantize(Decimal("0.00000001")) for i, u in units.items()
                if u > 0 and active[i][7]}
        resp = self.client.get("/export/download?report=holdings&fmt=csv")
        assert resp.status_code == 200
        got = {r["Ticker"]: Decimal(r["Units"]) for r in csv.DictReader(io.StringIO(
            resp.content.decode("utf-8-sig")))}
        assert got == want


def _yahoo(monkeypatch):
    class Ticker:
        def __init__(self, symbol):
            self.symbol = symbol

        def get_info(self):
            return dict(YAHOO.get(self.symbol, {}))

    monkeypatch.setattr(pricefeed, "_yf", SimpleNamespace(Ticker=Ticker))


@pytest.fixture
def env(app_module, template_db, tmp_path, monkeypatch):
    _yahoo(monkeypatch)
    made = []

    def fresh():
        if os.environ.get("STOCKTAKE_TEST_DB", "sqlite") == "postgres":
            factory = make_session_factory(DatabaseSettings(
                type="postgres", host=os.environ["APP_DATABASE__HOST"],
                port=int(os.environ["APP_DATABASE__PORT"]),
                name=os.environ["APP_DATABASE__NAME"], user=os.environ["APP_DATABASE__USER"],
                password=os.environ["APP_DATABASE__PASSWORD"]))
            with factory() as s:
                names = [r[0] for r in s.execute(text(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                    "AND tablename <> 'alembic_version'"))]
                s.execute(text("TRUNCATE " + ", ".join(f'"{n}"' for n in names)
                               + " RESTART IDENTITY CASCADE"))
                s.commit()
        else:
            path = tmp_path / f"walk{len(made)}.db"
            shutil.copy(template_db, path)
            factory = make_session_factory(DatabaseSettings(type="sqlite", path=str(path)))
        tenancy.install(factory)
        made.append(factory)
        return factory

    return SimpleNamespace(app_module=app_module, fresh=fresh)


def test_generated_sequences_keep_what_must_always_hold(env):
    run_state_machine_as_test(lambda: Walk(env), settings=settings(
        max_examples=settings.default.max_examples // 4 or 1, stateful_step_count=12))


def test_the_walk_starts_where_its_rules_expect(env):
    """The checks prove nothing if the starting state is not what the rules
    assume: two portfolios, each with a holding, and a session in the first."""
    walk = Walk(env)
    try:
        rows = walk._rows()
        assert [len(walk._mine(rows, p)) for p in walk.portfolios] == [2, 1]
        assert walk.client.get("/holdings", headers=HTML).status_code == 200
        with walk.factory() as s:
            assert s.scalar(select(User.email)) == "walker@example.test"
        walk.no_timeline_goes_below_zero()
        walk.every_page_renders()
        walk.the_export_lists_the_open_positions()
    finally:
        walk.teardown()

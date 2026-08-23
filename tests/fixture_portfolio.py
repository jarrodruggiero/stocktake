"""The reference portfolio: one synthetic dataset exercising every scenario
that has ever produced a bug, with every expected figure hand-computed here.

Deliberately small and stepped rather than realistic. Prices are step
functions, so the value on any date can be worked out in your head, and every
constant below is derived by hand in the comments — an assertion that merely
records what the code returned today would be worthless as a regression test.

Scenarios covered:
  ALPHA  DRP reinvestment — the double-count regression
  BETAX  multi-parcel FIFO with a partial sell straddling the 12-month
         CGT discount boundary (one parcel discountable, one not)
  GAMMA  a loss position still held
  OMEGA  a non-AUD instrument with its own FX history
  ZULU   a fully-closed position whose realised loss drives loss-ordering
  (a backdated sell is built unsaved via `factories.build_trade` — it must be
   refused by `balance_after`, so it never becomes fixture state)

Everything is AUD unless stated. Brokerage is included in cost, per the app.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from app.models import Instrument
from factories import (
    add_dividend,
    add_drp,
    add_fx_series,
    add_prices,
    add_trade,
    daily,
    make_instrument,
    monthly,
    stepped,
)

# The frozen "today" every date below is positioned against (see conftest).
TODAY = dt.date(2026, 8, 2)


@dataclass
class Reference:
    alpha: Instrument
    betax: Instrument
    gamma: Instrument
    omega: Instrument
    zulu: Instrument

    @property
    def all(self) -> list[Instrument]:
        return [self.alpha, self.betax, self.gamma, self.omega, self.zulu]


def price_calendar() -> list[dt.date]:
    """Monthly for the long history, daily for the last two months.

    Daily at the end is what makes the 1-day and 1-month performance windows
    meaningful — with monthly points only, "1 month" would span a single row.
    """
    return monthly("2019-01-01", "2026-05-01") + daily("2026-06-01", "2026-08-02")


def build_reference(db) -> Reference:
    """Build the reference portfolio into a portfolio-bound session (`pf`)."""
    cal = price_calendar()

    # --- ALPHA: an ETF held with distributions reinvested ------------------- #
    # 2026-08-01 close 14.00 → 2026-08-02 close 15.00 gives a non-zero daily
    # move to assert on (105 units × $1 = $105).
    alpha = make_instrument(db, "ALPHA", asset_class="etf", name="Alpha Index Fund",
                            drp=True)
    add_prices(db, alpha, stepped(cal, [("2021-01-01", "10.00"), ("2024-01-01", "12.00"),
                                        ("2026-06-01", "14.00"), ("2026-08-02", "15.00")]))
    add_trade(db, alpha, "2021-01-04", "buy", 100, "10.00", brokerage="10.00")
    # Distribution taken as units: $50 cash became 5 units at $10.
    add_drp(db, alpha, "2021-07-01", "50.00", 5, "10.00")
    # Distribution taken as cash — this one IS income.
    add_dividend(db, alpha, "2022-07-01", "60.00")

    # --- BETAX: two parcels, one sell across the discount boundary ---------- #
    betax = make_instrument(db, "BETAX", asset_class="share", name="Betax Holdings")
    add_prices(db, betax, stepped(cal, [("2023-01-01", "5.00"), ("2024-06-01", "8.00"),
                                        ("2026-06-01", "9.00")]))
    add_trade(db, betax, "2023-01-10", "buy", 100, "5.00", brokerage="10.00")
    add_trade(db, betax, "2023-09-10", "buy", 100, "6.00", brokerage="10.00")
    add_trade(db, betax, "2024-06-10", "sell", 150, "8.00", brokerage="10.00")

    # --- GAMMA: bought high, still held, underwater ------------------------- #
    gamma = make_instrument(db, "GAMMA", asset_class="share", name="Gamma Mining")
    add_prices(db, gamma, stepped(cal, [("2024-03-01", "20.00"), ("2026-06-01", "5.00")]))
    add_trade(db, gamma, "2024-03-01", "buy", 100, "20.00", brokerage="10.00")

    # --- OMEGA: USD-denominated, with an FX series -------------------------- #
    omega = make_instrument(db, "OMEGA", exchange="NASDAQ", asset_class="share",
                            currency="USD", name="Omega Corp")
    add_prices(db, omega, stepped(cal, [("2022-02-01", "100.00"), ("2026-06-01", "120.00")]))
    add_fx_series(db, "USDAUD", stepped(cal, [("2022-01-01", "1.50"), ("2026-06-01", "1.60")]))
    add_trade(db, omega, "2022-02-01", "buy", 10, "100.00", brokerage="5.00",
              fx_rate="1.50")

    # --- ZULU: bought, sold at a loss, fully exited ------------------------- #
    zulu = make_instrument(db, "ZULU", asset_class="share", name="Zulu Resources")
    add_prices(db, zulu, stepped(cal, [("2023-02-01", "10.00"), ("2024-06-01", "9.00")]))
    add_trade(db, zulu, "2023-02-01", "buy", 200, "10.00", brokerage="10.00")
    add_trade(db, zulu, "2024-06-20", "sell", 200, "9.00", brokerage="10.00")

    db.commit()
    return Reference(alpha=alpha, betax=betax, gamma=gamma, omega=omega, zulu=zulu)


# --------------------------------------------------------------------------- #
# Hand-computed expectations
#
# Work every one of these by hand before changing it. If the code disagrees,
# the code is what's wrong until proven otherwise — that is the whole point of
# this file.
# --------------------------------------------------------------------------- #

# ---- per-holding, at TODAY -------------------------------------------------
# ALPHA  units 100 bought + 5 DRP           = 105  @ 15.00 = 1575.00
#        cost = 100 × 10.00 + 10.00 brokerage = 1010.00   (DRP units cost nothing)
# BETAX  units 100 + 100 − 150 sold          =  50  @  9.00 =  450.00
#        cost = (100×5 + 10) + (100×6 + 10)  = 1120.00
#        NB cost is all-time outlay, not the cost of the units still held —
#        the app's "Total Spent" semantics, inherited from the spreadsheet.
# GAMMA  units 100                           @  5.00 =  500.00
#        cost = 100 × 20.00 + 10.00          = 2010.00
# OMEGA  units 10 @ 120 USD × 1.60 fx        = 1920.00
#        cost = (10 × 100 + 5) USD × 1.50 fx = 1507.50
# ZULU   units 0 → a closed position, excluded from the open-position totals
UNITS = {"ALPHA": Decimal("105"), "BETAX": Decimal("50"), "GAMMA": Decimal("100"),
         "OMEGA": Decimal("10"), "ZULU": Decimal("0")}
VALUE_AUD = {"ALPHA": Decimal("1575.00"), "BETAX": Decimal("450.00"),
             "GAMMA": Decimal("500.00"), "OMEGA": Decimal("1920.00")}
COST_AUD = {"ALPHA": Decimal("1010.00"), "BETAX": Decimal("1120.00"),
            "GAMMA": Decimal("2010.00"), "OMEGA": Decimal("1507.50")}

# ---- dashboard totals over OPEN positions ---------------------------------
# cost  = 1010.00 + 1120.00 + 2010.00 + 1507.50 = 5647.50
# value = 1575.00 +  450.00 +  500.00 + 1920.00 = 4445.00
TOTAL_COST_AUD = Decimal("5647.50")
TOTAL_VALUE_AUD = Decimal("4445.00")
TOTAL_GAIN_AUD = Decimal("-1202.50")   # 4445.00 − 5647.50

# Only ALPHA moved on the last day: (15.00 − 14.00) × 105 units = 105.00
TOTAL_DAY_CHANGE_AUD = Decimal("105.00")
ALPHA_DAY_PCT = Decimal("1") / Decimal("14")   # (15 − 14) / 14 = 0.0714285…

# ---- the daily series at its final point ----------------------------------
# invested = every BUY, incl. brokerage, at trade-date FX:
#            ALPHA 1010.00 + BETAX 1120.00 + GAMMA 2010.00
#          + OMEGA 1507.50 + ZULU 2010.00                     = 7657.50
# proceeds = BETAX (150×8.00 − 10) 1190.00 + ZULU (200×9.00 − 10) 1790.00
#                                                             = 2980.00
# cash income = ALPHA's 2022 distribution only                =   60.00
#            (the 2021 one was reinvested — it is already units in `value`)
# value                                                       = 4445.00
# gain = 4445.00 + 2980.00 + 60.00 − 7657.50                  = −172.50
SERIES_INVESTED = Decimal("7657.50")
SERIES_PROCEEDS = Decimal("2980.00")
SERIES_CASH_DIV = Decimal("60.00")
SERIES_VALUE = Decimal("4445.00")
SERIES_GAIN = Decimal("-172.50")
# If reinvested distributions were counted as income again (the bug fixed in
# this would read −122.50. That difference is the regression test.
SERIES_GAIN_IF_DRP_DOUBLE_COUNTED = Decimal("-122.50")

# ---- FY2024 capital gains (1 Jul 2023 → 30 Jun 2024) -----------------------
# Two disposals land in this year: BETAX on 2024-06-10 and ZULU on 2024-06-20.
#
# BETAX sell 150 @ 8.00 less 10.00 brokerage → unit proceeds 1190/150 = 7.93333…
#   parcel A  100 units acquired 2023-01-10, unit cost (100×5+10)/100 = 5.10
#             held > 12 months at 2024-06-10 → DISCOUNTABLE
#             cost base 510.00, proceeds 793.33, gain  283.33
#   parcel B   50 units acquired 2023-09-10, unit cost (100×6+10)/100 = 6.10
#             held < 12 months at 2024-06-10 → not discountable
#             cost base 305.00, proceeds 396.67, gain   91.67
# ZULU sell 200 @ 9.00 less 10.00 → unit proceeds 1790/200 = 8.95
#             cost base 2010.00, proceeds 1790.00, LOSS 220.00
#
# Netting, in the taxpayer-favourable order the app implements:
#   losses (220.00) hit non-discountable gains first → 91.67 absorbed
#   remainder 128.33 then reduces the discountable gains → 283.33 − 128.33 = 155.00
#   50% discount applies to what survives                → 77.50
#   net capital gain = (91.67 − 91.67) + 155.00 − 77.50   = 77.50
#
# The other order absorbs 220.00 of the discountable gains instead and nets
# 123.335, so this fixture pins the ORDER, not just the arithmetic.
FY2024 = 2024
FY2024_GAINS_DISCOUNTABLE = Decimal("283.33")
FY2024_GAINS_OTHER = Decimal("91.67")
FY2024_LOSSES = Decimal("220.00")
FY2024_DISCOUNT = Decimal("77.50")
FY2024_NET_CAPITAL_GAIN = Decimal("77.50")
FY2024_NET_CAPITAL_LOSS = Decimal("0")
FY2024_NET_IF_LOSSES_APPLIED_TO_DISCOUNTABLE_FIRST = Decimal("123.335")
FY2024_DISCOUNT_IF_LOSSES_APPLIED_TO_DISCOUNTABLE_FIRST = Decimal("31.665")

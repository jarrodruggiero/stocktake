"""OFX import.

The highest-leverage importer in the app: a broker that exports OFX needs no
template and no column mapping, which makes it the most likely thing to work
for somebody in a country nobody has written a broker format for.

Two fixtures, deliberately: **OFX 1.x is SGML with unclosed tags** and 2.x is
XML. Real brokers emit both, and a reader that only handles the tidy one fails
on the older, more common exports.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app import ofx

# OFX 1.x: no closing tags on leaf elements. This is not malformed — it is what
# the specification says, and what most brokers still produce.
SGML = """OFXHEADER:100
DATA:OFXSGML
VERSION:102

<OFX>
<INVSTMTMSGSRSV1>
<INVSTMTTRNRS>
<INVSTMTRS>
<CURDEF>AUD
<INVTRANLIST>
<BUYSTOCK>
<INVBUY>
<INVTRAN>
<FITID>1001
<DTTRADE>20260302120000.000[+11:AEDT]
<MEMO>Bought 100 ALPHA
</INVTRAN>
<SECID>
<UNIQUEID>AU000000ALP1
<UNIQUEIDTYPE>ISIN
</SECID>
<UNITS>100
<UNITPRICE>10.50
<COMMISSION>9.50
<TOTAL>-1059.50
</INVBUY>
<BUYTYPE>BUY
</BUYSTOCK>
<SELLSTOCK>
<INVSELL>
<INVTRAN>
<FITID>1002
<DTTRADE>20260610000000
</INVTRAN>
<SECID>
<UNIQUEID>AU000000ALP1
</SECID>
<UNITS>-40
<UNITPRICE>12.25
<COMMISSION>9.50
</INVSELL>
<SELLTYPE>SELL
</SELLSTOCK>
<INCOME>
<INVTRAN>
<FITID>1003
<DTTRADE>20260401000000
</INVTRAN>
<SECID>
<UNIQUEID>AU000000ALP1
</SECID>
<INCOMETYPE>DIV
<TOTAL>42.00
</INCOME>
</INVTRANLIST>
</INVSTMTRS>
</INVSTMTTRNRS>
</INVSTMTMSGSRSV1>
<SECLISTMSGSRSV1>
<SECLIST>
<STOCKINFO>
<SECINFO>
<SECID>
<UNIQUEID>AU000000ALP1
<UNIQUEIDTYPE>ISIN
</SECID>
<SECNAME>Alpha Australian Shares
<TICKER>ALPHA.AX
</SECINFO>
</STOCKINFO>
</SECLIST>
</SECLISTMSGSRSV1>
</OFX>
"""

# OFX 2.x: well-formed XML. Same content, different syntax.
XML = """<?xml version="1.0" encoding="UTF-8"?>
<OFX>
  <INVSTMTMSGSRSV1><INVSTMTTRNRS><INVSTMTRS>
    <CURDEF>AUD</CURDEF>
    <INVTRANLIST>
      <BUYMF>
        <INVBUY>
          <INVTRAN><FITID>2001</FITID><DTTRADE>20260305</DTTRADE></INVTRAN>
          <SECID><UNIQUEID>AU000000BET1</UNIQUEID></SECID>
          <UNITS>50</UNITS><UNITPRICE>8.00</UNITPRICE><COMMISSION>5.00</COMMISSION>
        </INVBUY>
      </BUYMF>
    </INVTRANLIST>
  </INVSTMTRS></INVSTMTTRNRS></INVSTMTMSGSRSV1>
  <SECLISTMSGSRSV1><SECLIST><MFINFO><SECINFO>
    <SECID><UNIQUEID>AU000000BET1</UNIQUEID></SECID>
    <TICKER>BETAX</TICKER>
  </SECINFO></MFINFO></SECLIST></SECLISTMSGSRSV1>
</OFX>
"""


# --------------------------------------------------------------------------- #
# Both dialects
# --------------------------------------------------------------------------- #

def test_the_sgml_dialect_with_unclosed_tags_parses():
    """The one that breaks XML parsers, and the one most brokers emit."""
    result = ofx.parse_ofx(SGML)

    assert result.errors == []
    assert [(c.ticker, c.type) for c in result.candidates] == [
        ("ALPHA", "buy"), ("ALPHA", "sell")
    ]


def test_the_xml_dialect_parses_too():
    result = ofx.parse_ofx(XML)

    assert result.errors == []
    assert [(c.ticker, c.type) for c in result.candidates] == [("BETAX", "buy")]


# --------------------------------------------------------------------------- #
# Reading a transaction
# --------------------------------------------------------------------------- #

def test_a_buy_carries_every_figure_the_app_needs():
    buy = ofx.parse_ofx(SGML).candidates[0]

    assert buy.date == dt.date(2026, 3, 2)
    assert buy.quantity == Decimal("100")
    assert buy.unit_price == Decimal("10.50")
    assert buy.brokerage == Decimal("9.50")
    assert buy.currency == "AUD"


def test_a_sell_s_negative_units_become_a_positive_quantity():
    """OFX signs UNITS by direction. This app stores a magnitude with the
    direction in `type` — keeping the sign would make a negative holding."""
    sell = ofx.parse_ofx(SGML).candidates[1]

    assert sell.type == "sell"
    assert sell.quantity == Decimal("40")


def test_the_timezone_suffix_is_discarded_not_applied():
    """"20260302120000.000[+11:AEDT]". A trade date is the date the broker says
    it is; re-interpreting it would move trades across financial years."""
    assert ofx.parse_ofx(SGML).candidates[0].date == dt.date(2026, 3, 2)


@pytest.mark.parametrize("raw,expected", [
    ("20260302", dt.date(2026, 3, 2)),
    ("20260302120000", dt.date(2026, 3, 2)),
    ("20260302120000.000[+11:AEDT]", dt.date(2026, 3, 2)),
    ("2026", None),
    ("notadate", None),
    (None, None),
])
def test_the_date_shapes_ofx_actually_uses(raw, expected):
    assert ofx._date(raw) == expected


def test_a_reinvestment_becomes_a_drp_not_a_buy():
    """REINVEST is units in without money out. Recording it as a buy would
    inflate the amount invested and understate every return."""
    text = SGML.replace("<BUYSTOCK>", "<REINVEST>").replace("</BUYSTOCK>", "</REINVEST>")

    kinds = {c.type for c in ofx.parse_ofx(text).candidates}

    assert "drp" in kinds


# --------------------------------------------------------------------------- #
# Securities
# --------------------------------------------------------------------------- #

def test_securities_are_resolved_from_the_security_list():
    """Transactions reference a security by CUSIP or ISIN, never by ticker.
    Without the list every row is unidentifiable."""
    assert ofx.security_tickers(SGML) == {"AU000000ALP1": "ALPHA"}


def test_an_exchange_suffix_is_stripped_from_the_ticker():
    """Some brokers write "ALPHA.AX"; the app stores the bare code with the
    exchange held separately."""
    assert ofx.security_tickers(SGML)["AU000000ALP1"] == "ALPHA"


def test_a_transaction_for_an_unlisted_security_is_reported_not_dropped():
    """Silently losing a trade is the worst outcome — the import looks like it
    worked and the balance is wrong."""
    text = SGML.replace("<TICKER>ALPHA.AX", "<TICKER>")

    result = ofx.parse_ofx(text)

    assert result.candidates == []
    assert any("security list" in s for s in result.skipped)


def test_a_transaction_missing_a_figure_says_which_one():
    text = SGML.replace("<UNITPRICE>10.50", "<UNITPRICE>")

    result = ofx.parse_ofx(text)

    assert any("missing its price" in s for s in result.skipped)


# --------------------------------------------------------------------------- #
# What is not a trade
# --------------------------------------------------------------------------- #

def test_income_is_skipped_by_name_rather_than_ignored():
    """A dividend is real money that this importer cannot record as a trade.
    Naming it is the difference between "handled" and "lost"."""
    result = ofx.parse_ofx(SGML)

    assert any("income" in s for s in result.skipped)
    assert any("2026-04-01" in s for s in result.skipped)


def test_a_cash_only_export_says_so_usefully():
    """A bank OFX has no investment transactions. The message has to say that,
    or somebody re-exports the same file three times."""
    result = ofx.parse_ofx("<OFX>\n<BANKMSGSRSV1>\n</BANKMSGSRSV1>\n</OFX>")

    assert result.candidates == []
    assert any("cash-only" in e for e in result.errors)


def test_a_file_that_is_not_ofx_is_refused_with_a_hint():
    result = ofx.parse_ofx("Date,Ticker,Units\n2026-03-02,ALPHA,100\n")

    assert any("does not look like an OFX file" in e for e in result.errors)
    assert any("Money/Quicken" in e for e in result.errors)


# --------------------------------------------------------------------------- #
# Safety
# --------------------------------------------------------------------------- #

def test_an_external_entity_is_inert():
    """This reads a file somebody uploaded. Handing that to an XML parser opens
    external-entity expansion — reading /etc/passwd into the document — which
    is live in Python's stdlib parsers unless deliberately disabled. A reader
    that understands only tags has no entity machinery to abuse.

    Do not "improve" the module by swapping in ElementTree.
    """
    hostile = XML.replace(
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<?xml version="1.0"?>\n'
        '<!DOCTYPE ofx [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>',
    ).replace("<TICKER>BETAX</TICKER>", "<TICKER>&xxe;</TICKER>")

    result = ofx.parse_ofx(hostile)

    # The entity is never expanded: it stays the literal text it was.
    assert "root:" not in str(result.candidates)
    assert all("/etc/passwd" not in (c.ticker or "") for c in result.candidates)


def test_a_deeply_nested_document_does_not_blow_up():
    """The other XML-parser hazard: entity expansion as a denial of service.
    Nothing here recurses, so depth is just characters."""
    result = ofx.parse_ofx("<OFX>" + "<A>" * 20000 + "</OFX>")

    assert result.errors  # no transactions, reported normally


def test_an_empty_file_is_an_error_not_a_crash():
    assert ofx.parse_ofx("").errors


# --------------------------------------------------------------------------- #
# Through the import flow
# --------------------------------------------------------------------------- #

def test_an_ofx_upload_previews_like_a_csv(client, session_factory):
    """OFX shares the whole preview-and-commit path — the only thing special
    about it is that there is no format to choose."""
    import factories as fac
    from test_routes import bind_to_only_portfolio, make_login, session_csrf

    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        fac.make_instrument(s, "ALPHA")
        s.commit()

    resp = client.post(
        "/imports-exports/csv",
        files={"file": ("export.ofx", SGML.encode(), "application/x-ofx")},
        data={"broker": "ofx", "_csrf": session_csrf(session_factory)},
        headers={"accept": "text/html"},
    )

    assert resp.status_code == 200
    assert "ALPHA" in resp.text
    assert "10.50" in resp.text


def test_the_imports_page_offers_ofx_for_any_broker(client, session_factory):
    from test_routes import make_login

    make_login(client, session_factory)

    page = client.get("/imports-exports", headers={"accept": "text/html"}).text

    assert 'value="ofx"' in page
    assert "any broker" in page

"""The broker CSV column mapper.

A CSV's fields are already delimited, so there is no pattern to infer — only
which column is which. The interesting parts are guessing the mapping from the
header names, and inferring the date format from the VALUES rather than the
header (decisions.md #86).

The output is exactly the YAML the shipped formats are written in, so it can be
installed, reused, or sent as a pull request unchanged.
"""

from __future__ import annotations

import pytest

from app import brokerdesign

SELFWEALTH = (
    "Trade Date,Buy/Sell,Code,Units,Price,Brokerage\n"
    "05/01/2026,Buy,ALPHA,100,89.50,9.50\n"
    "17/02/2026,Sell,BETAX,25,112.30,9.50\n"
)

AMBIGUOUS_DATES = (
    "Date,Side,Symbol,Qty,Unit Price,Fee\n"
    "01/02/2026,B,ALPHA,10,89.50,9.50\n"
    "03/04/2026,S,ALPHA,10,90.50,9.50\n"
)


# --------------------------------------------------------------------------- #
# Reading the file
# --------------------------------------------------------------------------- #

def test_it_reads_the_headers_and_some_rows():
    read = brokerdesign.read_csv(SELFWEALTH)

    assert read.headers == ["Trade Date", "Buy/Sell", "Code", "Units", "Price", "Brokerage"]
    assert len(read.rows) == 2
    assert read.rows[0]["Code"] == "ALPHA"


def test_headers_are_stripped():
    """Exports are full of `"Trade Date", "Code"` with a space after the comma,
    and a mapping keyed on ` Code` matches nothing at import time."""
    read = brokerdesign.read_csv("Trade Date , Code \n05/01/2026,ALPHA\n")

    assert read.headers == ["Trade Date", "Code"]


def test_a_byte_order_mark_is_not_part_of_the_first_header():
    """Every CSV a Windows tool exports starts with one, and it makes the first
    column's name invisibly different from what it looks like."""
    read = brokerdesign.read_csv("﻿Trade Date,Code\n05/01/2026,ALPHA\n")

    assert read.headers[0] == "Trade Date"


def test_only_a_few_rows_are_kept():
    """This is a preview, and the file may be years of trading. Reading it all
    into memory to show five lines is the same mistake as an unbounded upload."""
    big = "Date,Code\n" + "".join(f"05/01/2026,ALPHA{i}\n" for i in range(500))

    assert len(brokerdesign.read_csv(big).rows) == brokerdesign.SAMPLE_ROWS


def test_an_empty_file_is_reported_not_raised():
    read = brokerdesign.read_csv("")

    assert read.headers == []
    assert read.problem


def test_a_file_with_headers_and_no_rows_is_reported():
    """A header-only export is a real thing to do by accident, and mapping it
    would produce a format nobody could check."""
    read = brokerdesign.read_csv("Trade Date,Code\n")

    assert read.problem


# --------------------------------------------------------------------------- #
# Guessing the mapping
# --------------------------------------------------------------------------- #

def test_it_guesses_a_familiar_export():
    guess = brokerdesign.guess(["Trade Date", "Buy/Sell", "Code", "Units", "Price", "Brokerage"])

    assert guess == {
        "date": "Trade Date", "action": "Buy/Sell", "ticker": "Code",
        "units": "Units", "price": "Price", "brokerage": "Brokerage",
    }


def test_it_guesses_a_differently_worded_export():
    """Nobody agrees on these names. Guessing has to cover the vocabulary, not
    one broker's spelling of it."""
    guess = brokerdesign.guess(["Date", "Side", "Symbol", "Qty", "Unit Price", "Fee"])

    assert guess["date"] == "Date"
    assert guess["action"] == "Side"
    assert guess["ticker"] == "Symbol"
    assert guess["units"] == "Qty"
    assert guess["price"] == "Unit Price"
    assert guess["brokerage"] == "Fee"


def test_guessing_is_case_and_space_insensitive():
    guess = brokerdesign.guess(["TRADE DATE", "buy/sell", "  Code  "])

    assert guess["date"] == "TRADE DATE"
    assert guess["ticker"] == "  Code  "


def test_a_field_with_no_plausible_column_is_left_unset():
    """Better an empty dropdown than a confident wrong answer: a mis-guessed
    price column produces trades that look right and are not."""
    guess = brokerdesign.guess(["Foo", "Bar", "Baz"])

    assert guess["price"] is None
    assert guess["date"] is None


def test_a_column_is_not_used_for_two_fields():
    """`Price` matching both `price` and `brokerage` would silently make every
    trade's fee equal to its unit price."""
    guess = brokerdesign.guess(["Date", "Type", "Ticker", "Amount"])

    used = [v for v in guess.values() if v is not None]
    assert len(used) == len(set(used))


def test_total_value_is_never_guessed_as_the_unit_price():
    """The single most damaging mis-guess available: a "Value" or "Consideration"
    column is quantity times price, and reading it as the price overstates every
    holding by the number of units."""
    guess = brokerdesign.guess(["Date", "Side", "Code", "Units", "Value", "Consideration"])

    assert guess["price"] is None


# --------------------------------------------------------------------------- #
# The date format — unguessable from a header, and silently wrong if wrong
# --------------------------------------------------------------------------- #

def test_an_unambiguous_day_first_date_is_recognised():
    assert brokerdesign.guess_date_format(["17/02/2026", "05/01/2026"]) == "%d/%m/%Y"


def test_an_unambiguous_month_first_date_is_recognised():
    assert brokerdesign.guess_date_format(["02/17/2026", "01/05/2026"]) == "%m/%d/%Y"


def test_an_iso_date_is_recognised():
    assert brokerdesign.guess_date_format(["2026-01-05", "2026-02-17"]) == "%Y-%m-%d"


def test_a_written_month_is_recognised():
    assert brokerdesign.guess_date_format(["05 Jan 2026", "17 Feb 2026"]) == "%d %b %Y"


def test_wholly_ambiguous_dates_fall_back_to_the_australian_reading():
    """`01/02/2026` is both. The app is AU-first, so day-first is the right
    default — but the form says so out loud rather than deciding quietly, which
    is what `ambiguous` is for."""
    result = brokerdesign.guess_date_format(["01/02/2026", "03/04/2026"])

    assert result == "%d/%m/%Y"
    assert brokerdesign.dates_are_ambiguous(["01/02/2026", "03/04/2026"]) is True


def test_dates_that_settle_it_are_not_flagged_ambiguous():
    assert brokerdesign.dates_are_ambiguous(["17/02/2026", "05/01/2026"]) is False


def test_an_unrecognised_date_format_is_none_rather_than_a_guess():
    assert brokerdesign.guess_date_format(["last Tuesday"]) is None


# --------------------------------------------------------------------------- #
# What comes out
# --------------------------------------------------------------------------- #

def test_the_yaml_is_the_shape_the_app_already_loads():
    from ruamel.yaml import YAML

    body = brokerdesign.broker_yaml(
        name="SelfWealth", exchange="ASX", currency="AUD", date_format="%d/%m/%Y",
        columns={"date": "Trade Date", "action": "Buy/Sell", "ticker": "Code",
                 "units": "Units", "price": "Price", "brokerage": "Brokerage"},
    )
    loaded = YAML(typ="safe").load(body)

    assert loaded["kind"] == "mapped"
    assert loaded["name"] == "SelfWealth"
    assert loaded["columns"]["ticker"] == "Code"
    assert loaded["date_format"] == "%d/%m/%Y"


def test_the_date_format_is_quoted_so_yaml_keeps_the_percent_signs():
    body = brokerdesign.broker_yaml(
        name="X", exchange="ASX", currency="AUD", date_format="%d/%m/%Y", columns={})

    assert 'date_format: "%d/%m/%Y"' in body


def test_the_yaml_round_trips_into_a_working_format():
    """The end of the chain: what the mapper writes must parse the file it was
    built from. A template that loads and then reads nothing is the failure
    this whole feature exists to prevent."""
    from ruamel.yaml import YAML

    from app import brokercsv
    from app.settings import BrokerFormat

    read = brokerdesign.read_csv(SELFWEALTH)
    body = brokerdesign.broker_yaml(
        name="SelfWealth", exchange="ASX", currency="AUD",
        date_format=brokerdesign.guess_date_format([r["Trade Date"] for r in read.rows]),
        columns=brokerdesign.guess(read.headers),
    )
    fmt = BrokerFormat(**{k: v for k, v in YAML(typ="safe").load(body).items()
                          if k != "name"})
    result = brokercsv.parse_csv(SELFWEALTH, "selfwealth", fmt)

    assert result.errors == []
    assert [c.ticker for c in result.candidates] == ["ALPHA", "BETAX"]
    assert str(result.candidates[0].unit_price) == "89.50"


def test_the_yaml_explains_itself():
    """It is meant to arrive as a pull request. A file that turns up with no
    comment about where it came from gets reviewed worse."""
    body = brokerdesign.broker_yaml(
        name="X", exchange="ASX", currency="AUD", date_format="%d/%m/%Y", columns={})

    assert body.lstrip().startswith("#")
    assert "recipe-broker" in body


# --------------------------------------------------------------------------- #
# Checking the mapping before it is saved
# --------------------------------------------------------------------------- #

def test_a_complete_mapping_previews_real_trades():
    preview = brokerdesign.preview(
        SELFWEALTH, exchange="ASX", currency="AUD", date_format="%d/%m/%Y",
        columns={"date": "Trade Date", "action": "Buy/Sell", "ticker": "Code",
                 "units": "Units", "price": "Price", "brokerage": "Brokerage"})

    assert preview.errors == []
    assert len(preview.candidates) == 2


def test_the_wrong_date_format_is_caught_here_and_not_at_import():
    preview = brokerdesign.preview(
        SELFWEALTH, exchange="ASX", currency="AUD", date_format="%Y-%m-%d",
        columns={"date": "Trade Date", "action": "Buy/Sell", "ticker": "Code",
                 "units": "Units", "price": "Price", "brokerage": "Brokerage"})

    assert preview.errors


def test_an_incomplete_mapping_says_which_field_is_missing():
    preview = brokerdesign.preview(
        SELFWEALTH, exchange="ASX", currency="AUD", date_format="%d/%m/%Y",
        columns={"date": "Trade Date", "action": "Buy/Sell"})

    assert any("ticker" in e for e in preview.errors)


@pytest.mark.parametrize("field", ["date", "action", "ticker", "units", "price"])
def test_every_required_field_is_required(field):
    columns = {"date": "Trade Date", "action": "Buy/Sell", "ticker": "Code",
               "units": "Units", "price": "Price"}
    del columns[field]

    preview = brokerdesign.preview(SELFWEALTH, exchange="ASX", currency="AUD",
                                   date_format="%d/%m/%Y", columns=columns)

    assert any(field in e for e in preview.errors)


def test_brokerage_is_optional():
    """Not every export has one, and a missing fee is zero rather than a
    reason to refuse the file."""
    preview = brokerdesign.preview(
        SELFWEALTH, exchange="ASX", currency="AUD", date_format="%d/%m/%Y",
        columns={"date": "Trade Date", "action": "Buy/Sell", "ticker": "Code",
                 "units": "Units", "price": "Price"})

    assert preview.errors == []


# --------------------------------------------------------------------------- #
# Through the routes
# --------------------------------------------------------------------------- #

from test_routes import make_login, session_csrf  # noqa: E402

HTML = {"accept": "text/html"}


def rendered_yaml(page: str) -> str:
    """The format file as shown on the page.

    Unescaped, because Jinja turns `date_format: "%d/%m/%Y"` into
    `date_format: &#34;%d/%m/%Y&#34;` — asserting on the raw page silently
    matches nothing, which is a trap this project has hit before with
    apostrophes.
    """
    import html
    import re
    block = re.search(r"<pre[^>]*>(.*?)</pre>", page, re.S)
    return html.unescape(block.group(1)) if block else ""


def upload(client, factory, body=SELFWEALTH, **fields):
    return client.post(
        "/imports-exports/broker/design",
        data={"_csrf": session_csrf(factory), **fields},
        files={"file": ("selfwealth-trades.csv", body.encode(), "text/csv")},
        headers=HTML,
    )


def test_uploading_a_csv_shows_the_guessed_mapping(client, session_factory):
    make_login(client, session_factory)

    page = upload(client, session_factory)

    assert page.status_code == 200
    assert 'value="Trade Date" selected' in page.text
    assert 'value="Code" selected' in page.text


def test_the_first_pass_shows_no_preview(client, session_factory):
    """Nothing has been confirmed yet. Showing trades before anyone has looked
    at the mapping invites them to trust the guess."""
    make_login(client, session_factory)

    page = upload(client, session_factory)

    assert "What that mapping produces" not in page.text


def test_confirming_the_mapping_previews_the_trades(client, session_factory):
    make_login(client, session_factory)

    page = client.post(
        "/imports-exports/broker/design",
        data={"_csrf": session_csrf(session_factory), "text": SELFWEALTH,
              "name": "SelfWealth", "exchange": "ASX", "currency": "AUD",
              "date_format": "%d/%m/%Y",
              "col_date": "Trade Date", "col_action": "Buy/Sell",
              "col_ticker": "Code", "col_units": "Units", "col_price": "Price",
              "col_brokerage": "Brokerage"},
        headers=HTML)

    assert page.status_code == 200
    assert "2 trade(s) read" in page.text
    assert ">ALPHA<" in page.text and ">BETAX<" in page.text


def test_the_generated_format_appears_on_the_page(client, session_factory):
    make_login(client, session_factory)

    page = client.post(
        "/imports-exports/broker/design",
        data={"_csrf": session_csrf(session_factory), "text": SELFWEALTH,
              "name": "SelfWealth", "date_format": "%d/%m/%Y",
              "col_date": "Trade Date", "col_action": "Buy/Sell",
              "col_ticker": "Code", "col_units": "Units", "col_price": "Price"},
        headers=HTML)

    assert "kind: mapped" in page.text
    assert "ticker: Code" in page.text


def test_a_column_that_is_not_in_the_file_is_refused(client, session_factory):
    """A stale form, or a hand-posted field. It must not reach the parser
    naming a column that is not there."""
    make_login(client, session_factory)

    page = client.post(
        "/imports-exports/broker/design",
        data={"_csrf": session_csrf(session_factory), "text": SELFWEALTH,
              "name": "X", "date_format": "%d/%m/%Y",
              "col_date": "Trade Date", "col_action": "Buy/Sell",
              "col_ticker": "../../etc/passwd", "col_units": "Units",
              "col_price": "Price"},
        headers=HTML)

    assert page.status_code == 200
    assert "ticker" in page.text  # reported as unset, not passed through
    assert "etc/passwd" not in page.text


def test_the_ambiguous_date_warning_appears_when_it_should(client, session_factory):
    make_login(client, session_factory)

    page = upload(client, session_factory, body=AMBIGUOUS_DATES)

    assert "could be read either way" in page.text


def test_no_ambiguity_warning_on_a_file_that_settles_it(client, session_factory):
    make_login(client, session_factory)

    page = upload(client, session_factory)

    assert "could be read either way" not in page.text


def test_a_csv_with_no_rows_is_explained(client, session_factory):
    make_login(client, session_factory)

    page = upload(client, session_factory, body="Trade Date,Code\n")

    assert "no data rows" in page.text


def test_the_mapper_needs_a_session(client):
    response = client.post("/imports-exports/broker/design", data={"text": SELFWEALTH},
                           headers=HTML, follow_redirects=False)

    assert response.status_code in (302, 303, 401, 403)


def test_the_mapper_needs_the_csrf_token(client, session_factory):
    make_login(client, session_factory)

    response = client.post("/imports-exports/broker/design", data={"text": SELFWEALTH},
                           headers=HTML)

    assert response.status_code == 403


def test_the_format_downloads_under_the_shipped_naming_convention(client, session_factory):
    """`selfwealth.yaml`, so it drops into app/formats/brokers/ in a pull
    request without being renamed."""
    make_login(client, session_factory)

    response = client.post(
        "/imports-exports/broker/design/export",
        data={"_csrf": session_csrf(session_factory), "body": "name: X\nkind: mapped\n",
              "filename": "SelfWealth Trades"},
        headers=HTML)

    assert response.status_code == 200
    assert 'filename="selfwealth-trades.yaml"' in response.headers["content-disposition"]


def test_the_format_installs_straight_from_the_designer(client, session_factory,
                                                        tmp_path, monkeypatch, app_module):
    """T26d's second half: the designer offered a download and nothing else, so
    a template you designed could not be used without putting a file on the
    container's disk yourself."""
    monkeypatch.setattr(app_module.settings.imports, "templates_dir", str(tmp_path))
    make_login(client, session_factory)
    body = brokerdesign.broker_yaml(
        name="SelfWealth", exchange="ASX", currency="AUD", date_format="%d/%m/%Y",
        columns={"date": "Trade Date", "action": "Buy/Sell", "ticker": "Code",
                 "units": "Units", "price": "Price"})

    response = client.post(
        "/imports-exports/formats",
        data={"_csrf": session_csrf(session_factory), "kind": "broker",
              "body": body, "filename": "SelfWealth"},
        headers=HTML, follow_redirects=False)

    assert response.status_code == 303
    assert (tmp_path / "brokers" / "selfwealth.yaml").exists()


def test_an_installed_broker_format_is_offered_on_the_imports_page(
        client, session_factory, tmp_path, monkeypatch, app_module):
    """The end of the chain. A format that installs but never appears in the
    dropdown is a file, not a feature."""
    monkeypatch.setattr(app_module.settings.imports, "templates_dir", str(tmp_path))
    make_login(client, session_factory)
    body = brokerdesign.broker_yaml(
        name="Acme Broker", exchange="ASX", currency="AUD", date_format="%d/%m/%Y",
        columns={"date": "Trade Date", "action": "Buy/Sell", "ticker": "Code",
                 "units": "Units", "price": "Price"})
    client.post("/imports-exports/formats",
                data={"_csrf": session_csrf(session_factory), "kind": "broker",
                      "body": body, "filename": "Acme Broker"},
                headers=HTML)

    page = client.get("/imports-exports", headers=HTML).text

    assert "acme-broker" in page


def test_an_installed_format_actually_parses_a_file(client, session_factory,
                                                    tmp_path, monkeypatch, app_module):
    """And the real end: upload the same CSV under the format just designed
    from it, and get the trades back."""
    monkeypatch.setattr(app_module.settings.imports, "templates_dir", str(tmp_path))
    make_login(client, session_factory)
    body = brokerdesign.broker_yaml(
        name="Acme", exchange="ASX", currency="AUD", date_format="%d/%m/%Y",
        columns={"date": "Trade Date", "action": "Buy/Sell", "ticker": "Code",
                 "units": "Units", "price": "Price", "brokerage": "Brokerage"})
    client.post("/imports-exports/formats",
                data={"_csrf": session_csrf(session_factory), "kind": "broker",
                      "body": body, "filename": "Acme"},
                headers=HTML)

    page = client.post(
        "/imports-exports/csv",
        data={"_csrf": session_csrf(session_factory), "broker": "acme"},
        files={"file": ("t.csv", SELFWEALTH.encode(), "text/csv")},
        headers=HTML)

    assert page.status_code == 200
    assert "ALPHA" in page.text and "BETAX" in page.text


def test_a_header_with_a_currency_suffix_is_still_found():
    """Real exports carry "Unit Price (AUD)" and "Trade Date (local)". Exact
    matching alone misses the common case."""
    guess = brokerdesign.guess(
        ["Trade Date (local)", "Buy/Sell", "ASX Code", "Units", "Unit Price (AUD)"])

    assert guess["date"] == "Trade Date (local)"
    assert guess["price"] == "Unit Price (AUD)"
    assert guess["ticker"] == "ASX Code"


def test_a_total_column_never_claims_the_price_even_on_the_loose_pass():
    """The guard that makes the loose pass safe. "Total Price" and "Price
    Value" both contain the word `price` and are neither of them a unit price;
    reading one as such overstates every holding by its quantity."""
    guess = brokerdesign.guess(["Date", "Side", "Code", "Units", "Total Price"])

    assert guess["price"] is None


def test_an_exact_match_beats_a_loose_one():
    """Two columns both containing the word, the precise one listed second.
    The loose pass must not get there first — otherwise which column is chosen
    depends on the order the broker happened to write its headers in."""
    guess = brokerdesign.guess(["Date", "Side", "Code", "Units",
                                "Price (AUD, incl. GST)", "Price"])

    assert guess["price"] == "Price"


# --------------------------------------------------------------------------- #
# Saying the date format yourself
# --------------------------------------------------------------------------- #
#
# Inference is a convenience, not an authority. It reads the sample values, and
# a sample can be genuinely undecidable — so the answer is a box to type the
# real one in, not a cleverer guess.

def test_a_custom_date_format_is_used_as_given(client, session_factory):
    make_login(client, session_factory)

    page = client.post(
        "/imports-exports/broker/design",
        data={"_csrf": session_csrf(session_factory), "text": AMBIGUOUS_DATES,
              "name": "US Broker", "custom_date": "on", "date_format_custom": "%m/%d/%Y",
              "col_date": "Date", "col_action": "Side", "col_ticker": "Symbol",
              "col_units": "Qty", "col_price": "Unit Price"},
        headers=HTML)

    assert 'date_format: "%m/%d/%Y"' in rendered_yaml(page.text)
    # 01/02/2026 read month-first is 2 January.
    assert "2026-01-02" in page.text


def test_the_custom_box_wins_over_the_dropdown(client, session_factory):
    make_login(client, session_factory)

    page = client.post(
        "/imports-exports/broker/design",
        data={"_csrf": session_csrf(session_factory), "text": AMBIGUOUS_DATES,
              "name": "X", "date_format": "%d/%m/%Y",
              "custom_date": "on", "date_format_custom": "%m/%d/%Y",
              "col_date": "Date", "col_action": "Side", "col_ticker": "Symbol",
              "col_units": "Qty", "col_price": "Unit Price"},
        headers=HTML)

    assert 'date_format: "%m/%d/%Y"' in rendered_yaml(page.text)


def test_the_dropdown_is_used_when_the_box_is_not_ticked(client, session_factory):
    make_login(client, session_factory)

    page = client.post(
        "/imports-exports/broker/design",
        data={"_csrf": session_csrf(session_factory), "text": AMBIGUOUS_DATES,
              "name": "X", "date_format": "%d/%m/%Y",
              "date_format_custom": "%m/%d/%Y",
              "col_date": "Date", "col_action": "Side", "col_ticker": "Symbol",
              "col_units": "Qty", "col_price": "Unit Price"},
        headers=HTML)

    assert 'date_format: "%d/%m/%Y"' in rendered_yaml(page.text)


def test_a_custom_format_that_does_not_parse_is_reported_on_the_form(client, session_factory):
    """Typing one by hand is exactly where a typo lands, and the preview is the
    right place to find out — not the import, later, against a file you have
    put away."""
    make_login(client, session_factory)

    page = client.post(
        "/imports-exports/broker/design",
        data={"_csrf": session_csrf(session_factory), "text": SELFWEALTH,
              "name": "X", "custom_date": "on", "date_format_custom": "%Y-%m-%d",
              "col_date": "Trade Date", "col_action": "Buy/Sell", "col_ticker": "Code",
              "col_units": "Units", "col_price": "Price"},
        headers=HTML)

    assert page.status_code == 200
    assert "does not match" in page.text.lower() or "line 2" in page.text


def test_nonsense_in_the_custom_box_does_not_crash_the_page(client, session_factory):
    make_login(client, session_factory)

    page = client.post(
        "/imports-exports/broker/design",
        data={"_csrf": session_csrf(session_factory), "text": SELFWEALTH,
              "name": "X", "custom_date": "on", "date_format_custom": "%Q-%Z-%",
              "col_date": "Trade Date", "col_action": "Buy/Sell", "col_ticker": "Code",
              "col_units": "Units", "col_price": "Price"},
        headers=HTML)

    assert page.status_code == 200


def test_the_pages_builtin_word_list_matches_the_importers():
    """brokerdrag.js carries its own copy so it can grey the words that need no
    mapping. Two lists in two languages is exactly what drifts."""
    import re
    from pathlib import Path

    from app import brokercsv

    js = (Path(__file__).resolve().parent.parent / "app/static/brokerdrag.js").read_text()
    block = re.search(r"var BUILTIN = \{(.*?)\};", js, re.S).group(1)
    in_js = dict(re.findall(r'(\w+):\s*"(\w+)"', block))
    assert in_js == brokercsv.BUILTIN_ACTIONS


def test_the_back_button_goes_where_you_came_from_not_somewhere_fixed():
    """The same rule as the holdings back button: the origin travels with the
    request, and an off-site one is refused because it ends up in an href."""
    from app import navigation

    assert navigation.safe_path("/holdings", "/imports-exports") == "/holdings"
    assert navigation.back_label("/holdings", "Imports") == "Holdings"
    # The open-redirect cases, which look like paths and are not.
    for hostile in ("//evil.test", "/\\evil.test", "https://evil.test", ""):
        assert navigation.safe_path(hostile, "/imports-exports") == "/imports-exports"
    # Unnamed but valid: the fallback names it rather than the template guessing.
    assert navigation.back_label("/holding/ALPHA", "Imports") == "Imports"

# Recipe: support a new broker's CSV export

!!! tip "There is a tool for this now"
    **Imports → Map a new broker CSV** does everything below from the file
    itself: upload the CSV, check the columns it guessed, see the trades the
    mapping would read, and install or download the finished format. It also
    works out the date format from the values, which is the one thing a header
    cannot tell you and which is silently wrong for the first twelve days of
    every month if you get it backwards.

    The rest of this page is what the tool writes, and is worth reading if your
    export needs something the mapper cannot express.

**Most brokers need no code at all.** If the export has one row per trade with
columns for date, buy/sell, ticker, units, price and brokerage — which is the
common case — you add a block to `config.yaml` and you are done.

!!! tip "Start with the test"
    Even on the no-code path: drop your **redacted** sample into
    `tests/formats/samples/` with its expected rows *first*, and run
    `pytest tests/test_format_samples.py -q`. It will fail saying it cannot
    parse the file — which is the specification for the format block you are
    about to write, in the app's own words. See
    [Testing](testing.md#write-the-test-first).

## The no-code path

Add your broker under `imports.brokers`:

```yaml
imports:
  brokers:
    mybroker:                    # the name shown in the import dropdown
      kind: mapped
      exchange: ASX
      currency: AUD
      date_format: "%d/%m/%Y"    # how THIS broker writes dates
      columns:
        date: Trade Date         # ← your CSV's exact column headings
        action: Buy/Sell         # values must read as buy / sell
        ticker: Code
        units: Units
        price: Price
        brokerage: Brokerage
```

Restart, open **Imports**, pick your broker, upload the file. The preview shows
every parsed row before anything is written — check it, then commit.

Two things that trip people up:

- **`date_format` is Python's `strftime` syntax**, and it must match the file
  exactly. `%d/%m/%Y` is `06/03/2024`; `%Y-%m-%d` is `2024-03-06`.
- **Unknown tickers fail the preview** unless `imports.allow_new_instruments`
  is true. That default is deliberate — a typo in a ticker column would
  otherwise silently create a new instrument.

**If that works, contribute it.** Open a PR adding your block to the example
`config.yaml` with a comment naming the broker and the export it came from.
That is a complete, welcome contribution.

## When code is needed

Some exports are not one-row-per-trade. CommSec's transaction export, for
example, is a bank-statement layout: a `Details` column with the trade
described in prose, and Debit/Credit columns instead of a buy/sell flag. That
needs a parser, which is what `kind:` selects.

### 1. Write the parser

In `app/brokercsv.py`, alongside `parse_commsec_transactions`:

```python
def parse_mybroker(rows: list[dict], cfg: BrokerConfig) -> list[ParsedTrade]:
    """One paragraph on what this export looks like and why it needs code.

    Name the quirk. The next person will be looking at a file that does not
    match the docstring and will need to know whether their file is wrong or
    the broker changed the format.
    """
```

Return `ParsedTrade` objects. Raise `ValueError` with a message a person can
act on — it is shown in the preview, so "row 14: could not read '3/13/2024' as
a date, expected DD/MM/YYYY" beats "invalid date".

### 2. Register the kind

Add it to the dispatch table so `kind: mybroker` finds it.

### 3. Write the test

Add a fixture CSV to `tests/` — **a redacted one**. Change the amounts, use
fictional tickers, remove account numbers. Then:

```python
def test_mybroker_export_parses_a_buy_and_a_sell():
    rows = brokercsv.parse(MYBROKER_SAMPLE, config)

    assert [(r.type, r.ticker, r.units) for r in rows] == [
        ("buy", "ALPHA", Decimal("100")),
        ("sell", "ALPHA", Decimal("40")),
    ]
```

Then a test for the thing that made it need code in the first place. If the
format has a footer row, a running balance, or a line for dividends mixed in
with trades, that is the test that matters — the happy path is the easy half.

## What good looks like

- Every quirk of the format has a test, not just the happy path.
- The fixture is redacted and obviously synthetic.
- Failure messages name the row and say what was expected.
- The docstring says which broker and which export produced it, because
  brokers change their formats and the next person needs to know what yours
  looked like in 2026.

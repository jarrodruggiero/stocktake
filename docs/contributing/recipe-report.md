# Recipe: add an export report

Every download on the Imports page is a function in `app/exports.py` returning
`(headers, rows)`. The CSV and workbook writers, the filename, and the UI entry
all come from that.

!!! tip "Start with the test"
    Go to [Test it](#test-it) and write it now, with the figures worked out by
    hand from a small fixture. A report tested after the fact asserts what the
    code computed; this is money going to an accountant. See
    [Testing](testing.md#write-the-test-first).

## 1. Write the function

```python
def dividends_by_year(session: Session, fy: int | None = None, **_) -> Report:
    """One line on what question this answers for whom.

    Say if it is shaped for a particular purpose — "the figures an accountant
    needs for the dividend schedule" tells the next person what they may and
    may not change.
    """
    headers = ["Financial year", "Ticker", "Cash (AUD)", "Franking credits (AUD)"]
    fxbook = queries.FxBook(session)          # never convert at 1:1 by hand
    rows = []
    for dividend in session.scalars(select(Dividend)):
        rate = fxbook.of(dividend, dividend.instrument.currency)
        rows.append([
            fyreport.fy_label(...),
            dividend.instrument.ticker,
            _d(_aud(dividend.cash_amount, rate)),   # blank if unconvertible
            _d(dividend.franking_credits),
        ])
    rows.sort(key=lambda r: (r[0], r[1]))
    return headers, rows
```

`**_` in the signature is not noise: every report is called with the same
keyword arguments (`fy`, `ticker`) and ignores the ones it does not use.

## 2. Register it

Two places, both in `exports.py`:

- `REPORTS` — the key maps to the function.
- The label dictionary — what the checkbox says on the page.

## 3. Write its ABOUT text

The workbook ships an About sheet explaining every report. Add yours. This is
where you say what the numbers mean and what they do **not** — the existing
entries are the tone to match, and they are the app's most honest documentation
of its own limits.

## The rules that matter

**Never fabricate a number to fill a cell.** If a figure cannot be produced
honestly, the cell is empty. A blank is a question; a wrong number gets totalled
by a spreadsheet and lands in a tax return. Concretely:

- Convert through `queries.FxBook`, which returns `None` for a currency with no
  stored rate rather than falling back to 1:1.
- `_d(None)` renders as `""`. Use it.
- If a total depends on a missing component, the total is blank too — do not
  quietly sum the parts you happen to have.

**Put the unit in the header.** `Cost base (AUD)`, not `Cost base`. Someone
will paste this into a sheet with columns from three sources.

**Sort deterministically.** Two runs over unchanged data must produce
byte-identical files, or nobody can diff an export to see what changed.

## Test it

```python
@freeze_time(ref.TODAY)
def test_dividends_by_year_groups_and_totals(portfolio):
    headers, rows = exports.dividends_by_year(portfolio)

    assert column(headers, rows, "Ticker") == ["ALPHA", "BETAX"]
    assert cell(headers, rows[0], "Cash (AUD)") == Decimal("60.00")
```

`tests/test_exports.py` has `column` and `cell` helpers so assertions name the
header rather than an index — a column inserted in the middle then breaks
nothing.

Cover: the ordinary case against the reference fixture, an empty portfolio (a
report that raises on no data is a report that breaks a fresh install), and a
value that cannot be converted, asserting the cell is blank rather than wrong.

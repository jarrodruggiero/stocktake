# Recipe: add a chart field or measure

Charts are built from a **vocabulary**, not from bespoke code per chart. A
saved chart is a spec — a grain, an x axis, some measures, a type — and the
builder page offers whatever the vocabulary declares. Adding a measure makes it
available to every chart, the builder UI, and the table view, at once.

!!! tip "Start with the test"
    Write the assertion in `tests/test_charts.py` for the number your field
    should produce, from a fixture you can compute by hand, and watch it fail.
    Doing it in this order is what stops a chart field from being "whatever the
    code returned". Details in [Testing it](#testing) below and in
    [Testing](testing.md#write-the-test-first).

## The pieces

| File | Holds |
| --- | --- |
| `app/fields.py` | The vocabulary: grains, and the fields each one offers |
| `app/charts_build.py` | Running a spec against the data |
| `app/chart_templates.py` | The built-in charts, expressed as specs |

**Grains** are the shapes data can come in: `daily` (the time series),
`holdings` (one row per instrument), `periods` (the performance table). A field
belongs to a grain because it only means something at that shape — "day change"
is a holdings idea, not a daily-series one.

## Adding a measure to an existing grain

In `fields.py`, add it to that grain's field list:

```python
Field(
    key="h_yield_on_cost",
    label="Yield on cost",
    blurb="Distributions received, over what you paid. Shown per holding.",
    kind="percent",          # money | percent | number | text
    value=lambda h: (h.dividends_cash / h.cost) if h.cost else None,
),
```

- **`key`** is stored in saved chart specs. Once shipped it is permanent —
  changing it breaks everyone's saved charts. Prefix by grain (`h_` for
  holdings) so two grains can both have a "value".
- **`blurb`** is shown in the builder. Say what it means, not what it is
  called.
- **`kind`** drives formatting, axis choice and whether it can be stacked.
- **`value`** returns `None` when the measure does not apply. Never zero — a
  zero yield and an unknowable yield are different facts, and only one of them
  should be plotted.

That is usually the whole change. The builder, the renderer and the table view
all read the vocabulary.

## Things that will catch you

**Ratios cannot be stacked.** Stacking two percentages produces a number that
means nothing. If your measure is a ratio, make sure `kind="percent"` — the
renderer uses that to refuse.

**One axis, always.** This project does not do dual-axis charts. If a measure
cannot share a scale with the others, it belongs on its own chart. (There is a
known open defect where a split positions chart stacks a ratio measure — do not
copy that pattern.)

**Money must be blurrable.** Values rendered into the table view go through the
`m` class so the idle overlay can obscure them. If you add a renderer path, keep
that.

**FX honesty applies here too.** If your measure converts to AUD, go through
`queries.FxBook`. An instrument whose currency has no stored rate is *excluded*
and named in the series' `excluded` list, not silently valued at 1:1.

## Testing

Test the value function directly against the reference fixture, where the
expected number can be worked out by hand:

```python
def test_yield_on_cost_is_distributions_over_outlay(portfolio):
    holdings = {h.instrument.ticker: h for h in queries.all_holdings(portfolio)}

    field = fields.by_key("h_yield_on_cost")

    # ALPHA: 60.00 distributions on 1010.00 outlay = 5.94%
    assert field.value(holdings["ALPHA"]) == pytest.approx(Decimal("0.0594"), abs=1e-4)
```

Then the `None` case, and one builder test that the field appears for its grain
and not for the others.

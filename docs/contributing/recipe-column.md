# Recipe: add a column to the holdings table

The holdings table on the dashboard is driven by `queries.Holding` — a
dataclass built once per instrument by `build_holding` — and rendered from the
registry in `app/columns.py`. Adding a column is a computation, one registry
entry, and a test.


!!! tip "Start with the test"
    Jump to [4. Test it](#4-test-it), write that test now, and watch it fail on
    its assertion. The column's value is a number somebody will make decisions
    with — deciding what it should be *before* writing the code that produces
    it is the whole point. See [Testing](testing.md#write-the-test-first).

## 1. Compute it

In `app/queries.py`, add the field to `Holding` and fill it in `build_holding`:

```python
@dataclass
class Holding:
    ...
    days_held: int | None = None   # None when nothing is held
```

```python
first_buy = min((t.date for t in buys), default=None)
days_held = (clock.today() - first_buy).days if (first_buy and units) else None
```

Two things to get right:

- **`None` means "not applicable", not zero.** A closed position has no days
  held; a zero would sort and total as though it did. The templates render
  `None` as an em dash for exactly this reason.
- **Use `clock.today()`, never `dt.date.today()`.** In a UTC container the
  latter is yesterday for most of an Australian morning.

## 2. Declare it

**Do not touch the template.** Since the column chooser landed, the header row
and the cells are both generated from one registry in `app/columns.py`, so a
column is declared once — which is what makes it impossible for the two to
drift apart, and what makes this recipe three lines instead of a template hunt.

Add an entry to `COLUMNS`:

```python
Column("days_held", "Days held", GROUP_POSITION, lambda h: h.days_held,
       render="qty",
       help="How long the oldest parcel still held has been held."),
```

The fields that matter:

- **`render`** picks the formatter: `money`, `qty`, `pct`, `text`, `ticker` or
  `gain`. Money renders inside `<span class="m">` so it blurs with everything
  else when the idle overlay appears — another reason not to hand-write cells.
- **`numeric`** (default `True`) right-aligns and uses tabular figures. Set it
  `False` for text.
- **`help`** becomes the tooltip on both the header and the chooser. Worth
  writing: the chooser is a list of twenty names, and a column nobody
  understands is a column nobody turns on.
- **`locked`** is for columns that cannot be turned off. There is exactly one
  (`ticker`), and adding a second needs a better reason than "it is important".
  Locked means it cannot be *hidden*, not that it must come first — somebody
  who moves it into the middle of their table gets it there.
- **`sort`** is only needed when ordering by the column differs from what it
  displays. Two do: `ticker` hands the renderer the whole holding (the cell is
  a link plus a currency badge, and holdings do not compare), and `drp` renders
  "yes" or nothing — and nothing reads as *blank*, which would push every
  non-reinvesting holding to the bottom in both directions and make one of the
  two clicks do nothing. Everything else sorts by what it shows.

**Where it appears in `COLUMNS` is only the starting order.** New columns
render in declaration order, but the position is a per-user preference from
there: the chooser has an Order list, and the stored preference is a list whose
order is the table's.

### Should it be on by default?

Only add the key to `DEFAULTS` if the answer is genuinely yes for most people.
That list is what everybody who has never opened the chooser sees, so changing
it changes their dashboard — treat it as a decision, not a default. A wide
column (`name`) almost certainly should not be.

## 3. Export it, if it belongs there

If the column is useful to an accountant, add it to the matching report in
`app/exports.py` — usually `holdings`. Keep the header wording identical to
the on-screen label so the two can be reconciled, and put the unit in the
header (`Cost (AUD)`, not `Cost`).

## 4. Test it

Test the computation, not the HTML:

```python
@freeze_time(ref.TODAY)
def test_days_held_counts_from_the_first_buy(pf):
    alpha = fac.make_instrument(pf, "ALPHA")
    fac.add_trade(pf, alpha, "2024-03-02", "buy", 100, "5.00")
    fac.add_trade(pf, alpha, "2025-06-01", "buy", 50, "6.00")

    holding = queries.build_holding(pf, alpha)

    # From the FIRST buy, not the most recent — a top-up doesn't reset it.
    assert holding.days_held == (ref.TODAY - dt.date(2024, 3, 2)).days
```

Then the edge case that makes it interesting. Here it is a fully sold position:

```python
def test_days_held_is_blank_once_the_position_is_closed(pf):
    ...
    assert queries.build_holding(pf, alpha).days_held is None
```

One route test that the column renders is plenty — the value is proven above.
Assert on `>Days held</th>`, not on the bare words: the chooser below the table
renders every column's *label* whether it is showing or not, so matching the
word alone passes no matter what the table did.

And add the key to the chooser test in `tests/test_columns.py` if it belongs in
`DEFAULTS` — that file pins the exact default set, deliberately, so that
changing what everybody sees cannot happen by accident.

## Where it will bite you

- **`build_holding` runs per instrument, per page load**, behind the
  fingerprint cache. Do not put a query inside it: you will turn one page load
  into N. If you need data from another table, load it once outside the loop
  and pass it in, the way `prefs_by_instrument` does.
- **A column that only makes sense for open positions** must handle closed
  ones. `split_positions` separates them and both tables share the dataclass.

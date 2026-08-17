"""What the holdings table can show, declared once.

A column is one entry — what it is called, how to get it, how to render it —
rather than a `<th>` and a `<td>` in a template that have to agree. That is what
makes adding one a single change, and what makes letting people choose possible
at all.

Two things this buys beyond the chooser:

  * **Adding a column is a small pull request** against one list, which is what
    `docs/contributing/recipe-column.md` promises.
  * **Header and cell cannot drift apart**, because they are the same entry.

## Choosing

Choices are per **user**, not per portfolio: two people sharing a portfolio
each get their own. Stored like theme and accent, for the same reason.

`DEFAULTS` is exactly what the page showed before any of this existed, so
nobody's dashboard changes until they touch it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from . import money

# Groups exist so the chooser is scannable rather than a wall of twenty
# checkboxes. They carry no behaviour.
GROUP_POSITION = "Position"
GROUP_VALUE = "Value"
GROUP_PERFORMANCE = "Performance"
GROUP_INCOME = "Income"


@dataclass(frozen=True)
class Column:
    key: str
    label: str
    group: str
    value: Callable            # Holding -> anything the renderer understands
    render: str = "money"      # money | qty | pct | text | ticker | gain
    numeric: bool = True       # right-aligned, tabular figures
    # How to ORDER by this column when that differs from what it displays.
    # Only `ticker` and `drp` need it — decisions.md #52. See `sort_key()`.
    sort: Callable | None = None
    # Which currency this column's figures are in, when they are money:
    #   "reporting" — converted to the portfolio's reporting currency (AUD today)
    #   "native"    — the instrument's own currency, which VARIES BY ROW
    #   None        — not money (units, percentages, text)
    # The header renders the code from this rather than carrying it in `label`,
    # so that T28b's per-portfolio reporting currency is a change in ONE place
    # instead of a search for the string "AUD" across twenty labels.
    currency: str | None = None
    # A header that cycles through more than one sort key, with the mark each
    # one shows. Gain and Today display an amount AND a percentage in one cell,
    # and both are worth ordering by — "what made me the most" and "what grew
    # fastest" are different questions. See `sorting.Sort.cycle`.
    sort_pair: tuple[str, ...] = ()
    sort_marks: tuple[str, ...] = ()
    # A column nobody can turn off. `ticker` is the row's identity and the link
    # to its ledger — a table without it is a list of anonymous numbers.
    locked: bool = False
    help: str = ""

    def heading(self, native: str | None = None, *, mark: bool = True) -> str:
        """The label with its currency — for the table header AND the chooser.

        The currency lives here rather than in every cell: a symbol repeated
        down two hundred right-aligned cells is noise and fights the alignment
        that makes a numeric column scannable.

        **`mark=False` when the whole table is one currency**, which is almost
        every portfolio — seven headers each saying "(AUD)" is the same noise
        moved up a row. The chooser always marks, because `cost` and
        `cost_reporting` share the label "Cost" and are told apart only by
        currency; two identical checkboxes would be a coin toss.

        `native` is the one currency the holdings trade in, or None when they
        trade in several — in which case the column cannot name one, and says so.
        """
        if not mark or self.currency is None:
            return self.label
        if self.currency == "reporting":
            return f"{self.label} ({money.REPORTING})"
        return f"{self.label} ({native})" if native else f"{self.label} (native)"

    @property
    def is_money(self) -> bool:
        return self.currency is not None


def one_currency_everywhere(native: str | None) -> bool:
    """Whether every money figure in the table is in the same currency.

    True when the holdings all trade in the reporting currency: native and
    reporting columns then show the same thing, so nothing needs distinguishing
    and the table can name its currency once.
    """
    return native is not None and native == money.REPORTING


COLUMNS: list[Column] = [
    Column("ticker", "Ticker", GROUP_POSITION, lambda h: h, render="ticker",
           numeric=False, locked=True, sort=lambda h: h.instrument.ticker,
           help="The holding, linking to its full ledger."),
    Column("asset_class", "Class", GROUP_POSITION, lambda h: h.instrument.asset_class,
           render="text", numeric=False,
           help="ETF, share or crypto."),
    Column("name", "Name", GROUP_POSITION, lambda h: h.instrument.name or "",
           render="text", numeric=False,
           help="The full name. Off by default — it is wide."),
    Column("exchange", "Exchange", GROUP_POSITION, lambda h: h.instrument.exchange,
           render="text", numeric=False),
    Column("units", "Units", GROUP_POSITION, lambda h: h.units, render="qty"),

    Column("avg_price", "Avg price", GROUP_VALUE, lambda h: h.avg_price, currency="native",
           help="What you paid per unit on average, in the instrument's own currency."),
    Column("cost", "Cost", GROUP_VALUE, lambda h: h.cost, currency="native",
           help="Outlay including brokerage, in the instrument's own currency."),
    Column("price", "Price", GROUP_VALUE, lambda h: h.price, currency="native",
           help="Latest stored close, in the instrument's own currency."),
    # The reporting-currency set, declared in the same order as the native one
    # above so the chooser reads as two parallel lists rather than a pile. Only
    # the chooser's order comes from here — what the table renders is the order
    # the person picked, and `DEFAULTS` names its own.
    Column("avg_price_reporting", "Avg price", GROUP_VALUE, lambda h: h.avg_price_aud,
           currency="reporting",
           help="What you paid per unit on average, each purchase converted at "
                "its own exchange rate."),
    Column("cost_reporting", "Cost", GROUP_VALUE, lambda h: h.cost_aud, currency="reporting",
           help="Outlay converted at each trade's own exchange rate."),
    Column("price_reporting", "Price", GROUP_VALUE, lambda h: h.price_aud,
           currency="reporting",
           help="The latest stored close at the latest exchange rate."),
    Column("value_reporting", "Value", GROUP_VALUE, lambda h: h.value_aud, currency="reporting",
           help="Today's value at the latest exchange rate."),

    Column("gain_reporting", "Gain", GROUP_PERFORMANCE, lambda h: h.gain_aud, currency="reporting",
           render="gain",
           sort_pair=("gain_reporting", "gain_pct"), sort_marks=("$", "%"),
           help="Value less cost, with the percentage underneath. Clicking cycles "
                "the sort: amount up, % up, amount down, % down."),
    Column("gain_pct", "Gain %", GROUP_PERFORMANCE, lambda h: h.gain_pct, render="pct"),
    Column("day_change", "Today", GROUP_PERFORMANCE, lambda h: h.day_change_aud, currency="reporting",
           render="gain",
           sort_pair=("day_change", "day_pct"), sort_marks=("$", "%"),
           help="Movement since the previous close. Clicking cycles the sort: "
                "amount up, % up, amount down, % down."),
    Column("day_pct", "Today %", GROUP_PERFORMANCE, lambda h: h.day_pct, render="pct"),

    Column("dividends", "Dividends", GROUP_INCOME, lambda h: h.dividends_cash, currency="native",
           help="Cash distributions received, in the instrument's own currency."),
    Column("dividends_reporting", "Dividends", GROUP_INCOME, lambda h: h.dividends_aud,
           currency="reporting"),
    Column("drp", "DRP", GROUP_INCOME, lambda h: "yes" if h.drp else "",
           render="text", numeric=False, sort=lambda h: bool(h.drp),
           help="Whether distributions are reinvested."),
    # The registry allots whole units only, so the remainder is money it holds
    # until the next distribution — on no statement total and in no other
    # column. A BALANCE, never income: decisions.md #8.
    Column("residual", "DRP residual", GROUP_INCOME, lambda h: h.drp_residual,
           currency="native",
           help="Cash the registry is holding from your last distribution — too "
                "little to buy a whole unit, and carried into the next one. A "
                "balance, not income; blank means none was ever recorded."),
]

BY_KEY = {c.key: c for c in COLUMNS}

# Exactly what the dashboard showed before it was configurable. Changing this
# list changes what every user who has never opened the chooser sees, so treat
# it as a decision rather than a default.
DEFAULTS = [
    "ticker", "asset_class", "units", "avg_price", "cost",
    "price", "value_reporting", "gain_reporting", "day_change", "dividends",
]


def sort_key(column: Column) -> Callable:
    """How to order by this column — its own `sort` if it declared one,
    otherwise the value it displays."""
    return column.sort or column.value


def sortable_keys() -> set[str]:
    """The allow-list `sorting.read()` checks a query parameter against."""
    return {c.key for c in COLUMNS}


def chosen(preference: list[str] | None) -> list[Column]:
    """The columns to render, **in the order they were chosen**.

    Unknown keys are dropped and duplicates collapse; a locked column keeps
    whatever position it was given. Why each of those: decisions.md #62.
    """
    keys = list(preference) if preference else list(DEFAULTS)
    seen: set[str] = set()
    ordered = [k for k in keys
               if k in BY_KEY and not (k in seen or seen.add(k))]
    missing_locked = [c.key for c in COLUMNS if c.locked and c.key not in seen]
    return [BY_KEY[k] for k in missing_locked + ordered]


def move(preference: list[str], key: str, direction: str) -> list[str]:
    """`preference` with `key` swapped one place up or down.

    A no-op at either end, for a key that is not there, or for a direction that
    is not one of the two — all three are the stale-form case (two tabs, one of
    which has already saved something else), and a quiet no-op is a better
    answer there than a 500 or a reordering of whatever happened to be nearby.
    """
    if direction not in ("up", "down") or key not in preference:
        return list(preference)
    index = preference.index(key)
    target = index - 1 if direction == "up" else index + 1
    if not 0 <= target < len(preference):
        return list(preference)
    out = list(preference)
    out[index], out[target] = out[target], out[index]
    return out


def groups() -> list[tuple[str, list[Column]]]:
    """Columns grouped for the chooser, in declaration order within a group."""
    out: list[tuple[str, list[Column]]] = []
    for column in COLUMNS:
        if not out or out[-1][0] != column.group:
            existing = next((g for g in out if g[0] == column.group), None)
            if existing is None:
                out.append((column.group, [column]))
                continue
            existing[1].append(column)
        else:
            out[-1][1].append(column)
    return out


def clean(keys: list[str]) -> list[str]:
    """A submitted selection, filtered to what exists and always answerable.

    An empty selection would leave a table of nothing, so it falls back to the
    defaults rather than rendering a page that looks broken.

    **Order is preserved**, because it carries meaning. Sorting here
    would undo every reorder on the next save.
    """
    valid = [k for k in keys if k in BY_KEY]
    if not [k for k in valid if not BY_KEY[k].locked]:
        return list(DEFAULTS)
    return valid

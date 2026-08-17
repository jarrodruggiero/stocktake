"""Click a column header to sort. Shared by every table that wants it.

The holdings table has a registry behind it (`app/columns.py`) because its
columns are chosen and reordered. The FY report has three tables of three
different shapes and wants only the ordering — so ordering lives here and the
registry stays about columns.

**Sorting is server-side.** The values are `Decimal`s and some are `None`; in
the browser they are already strings, where "1,234.50" sorts as text and an em
dash sorts wherever the browser feels like.

**Blanks last, in both directions.** `sorted(reverse=True)` reverses the blanks
too, so descending would put the rows with no data on top. They are the least
interesting rows either way.

**Which column a table is sorted by lives in the URL**, not the user record. It
survives a reload, can be linked and bookmarked, and two people looking at one
portfolio do not fight over it — and it is the honest scope, since sorting is
something you do while reading, unlike *which columns exist*. Parameters carry a
table-name prefix because the FY report puts three sortable tables on one page.
Natural order is one click on the nav link, which carries no query.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlencode

UP = "▲"
DOWN = "▼"


def _blank(value: Any) -> bool:
    """Missing, as opposed to merely falsy.

    `None` and the empty string are absences. **Zero and False are not** —
    treating falsiness as absence would drop every position that has not moved
    today to the bottom of a sort on "Today", which is precisely the set of rows
    somebody sorting that column is looking at.
    """
    return value is None or value == ""


def apply(rows: Sequence, getter: Callable, *, descending: bool) -> list:
    """`rows` ordered by `getter`, with the blanks kept at the end.

    Partition rather than a clever key function: a key that encodes "blank" as
    a tuple has to invent a value to pair it with, and there isn't one that
    compares against both Decimals and strings. Two lists is less code and
    cannot be wrong.

    Both halves stay stable, so equal values keep the order the page already
    had — which makes the natural order the secondary sort for free.
    """
    present = [r for r in rows if not _blank(getter(r))]
    absent = [r for r in rows if _blank(getter(r))]
    try:
        present.sort(key=getter, reverse=descending)
    except TypeError:
        # A column whose values are not comparable to one another. Rendering the
        # table unsorted is a worse page; a 500 is no page at all.
        return list(rows)
    return present + absent


@dataclass(frozen=True)
class Sort:
    """One table's sort state, and the links its headers point at."""

    table: str
    key: str | None = None
    descending: bool = False
    # Every other query parameter on the current URL, so a header link keeps
    # them. `?fy=2026` is the one that matters: losing it sorts a different
    # financial year, silently, with entirely plausible numbers.
    others: tuple[tuple[str, str], ...] = field(default=())

    def on(self, key: str) -> bool:
        return self.key == key

    def arrow(self, key: str) -> str:
        """The glyph for this header — empty for every column but the sorted
        one. An arrow on all of them is decoration that stops saying which is
        active, which is the whole job."""
        if not self.on(key):
            return ""
        return DOWN if self.descending else UP

    def aria(self, key: str) -> str:
        """`aria-sort`, which is what a screen reader reads. The arrow is a
        glyph and says nothing to one."""
        if not self.on(key):
            return "none"
        return "descending" if self.descending else "ascending"

    def href(self, key: str) -> str:
        """Where this header links: the same page, sorted by `key`.

        Ascending first; clicking the column that is already sorted flips it.
        """
        params = list(self.others)
        params.append((f"{self.table}_sort", key))
        if self.on(key) and not self.descending:
            params.append((f"{self.table}_dir", "desc"))
        return "?" + urlencode(params)

    # Headers that cycle through a pair of keys: value ↑, % ↑, value ↓, % ↓.
    # Why four states rather than a second control, and why `unit()` has to
    # render which one you are in — decisions.md #25.

    def _states(self, keys: Sequence[str]) -> list[tuple[str, bool]]:
        """The cycle, in order: every key ascending, then every key descending."""
        return [(key, descending) for descending in (False, True) for key in keys]

    def cycle(self, keys: Sequence[str]) -> str:
        """Where a paired header links: one step around the cycle."""
        states = self._states(keys)
        try:
            index = states.index((self.key, self.descending))
        except ValueError:
            index = -1          # not sorted by this pair: start at the first
        key, descending = states[(index + 1) % len(states)]

        params = list(self.others)
        params.append((f"{self.table}_sort", key))
        if descending:
            params.append((f"{self.table}_dir", "desc"))
        return "?" + urlencode(params)

    def on_any(self, keys: Sequence[str]) -> bool:
        return self.key in set(keys)

    def unit(self, keys: Sequence[str], marks: Sequence[str]) -> str:
        """Which of the pair is active, as the header shows it — `$` or `%`.

        Empty when this pair is not the sorted one, so exactly one header on the
        table is wearing a state.
        """
        if not self.on_any(keys):
            return ""
        return marks[list(keys).index(self.key)]

    def pair_arrow(self, keys: Sequence[str]) -> str:
        if not self.on_any(keys):
            return ""
        return DOWN if self.descending else UP

    def pair_aria(self, keys: Sequence[str]) -> str:
        if not self.on_any(keys):
            return "none"
        return "descending" if self.descending else "ascending"


def read(query: Mapping[str, str], table: str, allowed: Iterable[str]) -> Sort:
    """This table's sort state, from the request's query parameters.

    `allowed` is an allow-list and not a suggestion: the parameter names an
    attribute to order by, so it is caller-supplied input arriving at a lookup.
    Anything not on the list is dropped and the table renders in its natural
    order.
    """
    sort_param = f"{table}_sort"
    dir_param = f"{table}_dir"
    key = query.get(sort_param)
    if key not in set(allowed):
        key = None
    return Sort(
        table=table,
        key=key,
        descending=query.get(dir_param) == "desc",
        others=tuple((k, v) for k, v in query.items()
                     if k not in (sort_param, dir_param)),
    )

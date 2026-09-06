# Guides

## Dashboard

Holdings, totals, and the day's movement. Open and closed positions are
separated — a sold-out holding stops cluttering the table but keeps its
realised history.

Money figures blur when a session is about to time out. That is the only time
they blur; there is no manual toggle.

If a holding cannot be converted to AUD — no stored exchange rate for its
currency — it is **excluded from the totals and named**, rather than being
quietly counted at 1:1. It reappears once the feed has the rate.

## Charts

The charts page is a grid of cards you own. Drag a card by its handle to
rearrange; edit any of them, including the ones that were there when you
started.

**Building one:** pick a *grain* (the shape of the data — a daily series, one
row per holding, or the performance table), then an x axis, then measures.
Preview updates as you go. Save it and it joins the grid.

Charts belong to **you**, not to the portfolio: two people sharing a portfolio
each keep their own layout.

Every chart has a table view underneath, which is also how the numbers stay
available to a screen reader.

## Plan

A rotation and a schedule: which instruments, how much, how often. The page
tells you what is next and when it is due.

Recording a planned buy from here advances the schedule; recording the same
trade from **Record a trade** does not, because the app cannot know it was the
scheduled one. Skipping is recorded too, so the history stays honest.

More detail: [DCA Schedule](dca-schedule.md).

## Reports → FY

The Australian tax view for a financial year:

- **Capital gains** — FIFO parcel matching, per-parcel detail, the 50%
  discount where a parcel was held more than twelve months, and losses applied
  in the taxpayer-favourable order.
- **Dividend income** — cash and franking credits, grossed up.
- **A snapshot** of what was held at year end.

The figures are a starting point for your accountant, not a lodgement. See the
[disclaimer](../about/disclaimer.md).

## Imports and exports

**Imports** takes broker CSVs and registry PDF statements. Both preview before
writing. Statements are parsed on your own hardware — no cloud OCR, no upload.

**Exports** produces CSVs or a workbook: transactions, holdings, dividends,
realised and unrealised capital gains, closed positions, performance. The
workbook has an About sheet explaining what each report means and does not
mean — worth reading before sending it on.

Figures that cannot be converted to AUD honestly come out **blank** rather than
converted at 1:1, so nothing wrong gets totalled by a spreadsheet.

## Instruments and the ledger

An instrument's page shows its full ledger: every buy, sell, DRP allocation and
cash distribution, with per-parcel gain at the latest price.

Rows can be edited and deleted here. Two rules apply:

- **Editing re-checks the whole timeline**, not just the row you changed.
  Reducing a buy from three years ago that a later sell depended on is refused,
  and the message names the sell to fix first.
- **A DRP row is edited through its dividend**, because the cash and the units
  are one event.

## Account

Password, appearance (yours alone), two-factor, and **active sessions** — every
device signed in, when it was last used, and a way to sign out one or all of
the others.

## Admin

Users, portfolio members and API keys. Admins can reset a password and clear
someone's two-factor; portfolio owners manage members and keys.

Roles: **owner** (everything), **member** (record and edit), **viewer** (read
everything, change nothing).

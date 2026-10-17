# Guides

## Dashboard

Holdings, totals, and the day's movement. Open and closed positions are
separated — a sold-out holding stops cluttering the table but keeps its
realised history.

Gain is measured two ways, each labelled. A holding's **unrealised gain** is
its value less what the units still held cost, the oldest counted as sold
first — a reinvested unit at what was reinvested. The **Performance** chart is
**total return**: value, plus what sales and cash distributions brought in,
less everything put in.

Money figures blur when a session is about to time out. That is the only time
they blur; there is no manual toggle.

If a holding cannot be converted to AUD — no stored exchange rate for its
currency — it is **excluded from the totals and named**, rather than being
quietly counted at 1:1. It reappears once the feed has the rate.

### Simulate a sale

**Simulate sale** works out what selling some of each holding today would bring
home after capital gains tax. Nothing is recorded. It is a calculator, not
financial or tax advice.

Type the units to sell for each holding, or sell a percentage of the portfolio
(that share of every holding, rounded down to whole units, or to a coin's
eighth decimal place), or all of it.

- **The sale** is at the latest price and today's exchange rate. Brokerage is
  what the trade form would suggest, the portfolio's fee or else the last
  trade's of that kind; type one in to replace it.
- **The cost base** of the units sold is worked out as the FY report does,
  oldest parcels first.
- **The tax** follows the ATO's method: the year's gains, less its losses and
  then losses carried in from earlier years (non-discount gains first), less
  the 50% discount on what is left of gains on units held for at least 12
  months, taxed at your marginal rate. So the sale's tax is what it adds to
  this financial year's net capital gain, with the year's sales so far and
  carried losses counted. A loss lowers the year's tax when there are gains to
  offset, and that saving counts in what the sale brings home; otherwise it
  carries forward.
- **The income tax rate** is your top rate, and the **Medicare levy** is kept
  apart from it (2% for most people). The portfolio's owner saves both in the
  portfolio's settings, and either can be changed for one calculation. One rate
  covers the whole gain, so a gain big enough to cross into a higher tax
  bracket is taxed partly at a rate this does not show. See the ATO's
  [tax rates](https://www.ato.gov.au/tax-rates-and-codes/tax-rates-australian-residents)
  and [how to calculate your CGT](https://www.ato.gov.au/individuals-and-families/investments-and-assets/capital-gains-tax/calculating-your-cgt/how-to-calculate-your-cgt).
- **The rules are an individual's or a trust's.** A complying super fund's
  discount is a third, a company gets none, and neither is indexed from
  1 July 2027.
- **From 1 July 2027** a sale is worked out under the new rules
  ([below](#capital-gains-from-1-july-2027)). The CPI for the quarter of a sale
  today is not published yet, so the latest one stands in, and the page says
  which.

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
  in the taxpayer-favourable order, earlier years' carried forward. From
  1 July 2027, indexation as well (below).
- **Dividend income** — the cash received, and any foreign tax withheld from
  it. A trust's distribution counts in the year its period ends, when it says
  one, so a June quarter paid in July belongs to the year to 30 June. Leave the
  period empty for a company's dividend, which counts when it is paid. Franking
  credits, and the gross-up, are in the dividends export rather than on the
  page.
- **A snapshot** of what was held at year end.

The figures are a starting point for your accountant, not a lodgement. See the
[disclaimer](../about/disclaimer.md).

### Capital gains from 1 July 2027

From 1 July 2027 the 50% CGT discount gives way to indexing the cost base by
the Consumer Price Index (Treasury Laws Amendment (Tax Reform No. 1) Act 2026).
The FY report, Simulate sale and the exports all work out a sale from then this
way.

- **A holding you have on 30 June 2027** counts as sold just before 1 July 2027
  for what it was worth, and bought again on 1 July 2027 for the same. Its gain
  or loss to then waits for the real sale, and is discounted by 50% if by then
  you have held it for more than 12 months. Stocktake values it at the last
  close in the week before 1 July 2027, at 30 June 2027's exchange rate for a
  foreign holding. A holding with no close that week is not valued: the FY
  report names it, and leaves its sales out of the totals until a price for
  that week is recorded.
- **After that, the cost base is indexed** instead of discounted. It is
  multiplied by the CPI for the quarter of the sale over the CPI for the
  quarter the money was spent: for a holding you had on 30 June 2027, the
  September 2027 quarter. The factor is worked to three decimal places and
  never goes below 1. It applies only to a holding bought at least 12 months
  before the sale, counted from when you really bought it, not from 1 July
  2027.
- **Money spent later is indexed from its own quarter, and money back is not
  indexed.** A return of capital comes off the cost base as it is. Where the
  cost base is made of amounts from different quarters, their factors are
  weighed together.
- **Indexing cannot make a loss.** A sale between the cost base and the indexed
  cost base makes no gain and no loss. Below the cost base, the loss is worked
  out from the cost base.
- **Losses** come off the gains to 30 June 2027 first and the later gains after
  them, and the discount comes last, as before.
- **The CPI** is the ABS's All Groups CPI for the eight capital cities, as first
  published for each quarter, about four weeks after it ends. The price feed
  fetches it. Until a quarter's number is out, the FY report names a sale in
  that quarter and leaves it out of its totals. Simulate sale and the
  unrealised gains export do not wait: they use the latest number published.

On the FY page each sale shows what it cost, its value on 30 June 2027, its
indexation and its proceeds: the gain is the proceeds less the cost and the
indexation. Each parcel shows its factor, as ×1.040, and the realised gains
export has the working parcel by parcel.

The Act lets the Minister set another way to value a holding at 30 June 2027.
None has been made, so Stocktake uses the market value. The 30% minimum tax on
gains from 1 July 2027 is not worked out yet.

## Imports and exports

**Imports** takes broker CSVs and registry PDF statements. Both preview before
writing. Statements are parsed on your own hardware — no cloud OCR, no upload.

**Exports** produces CSVs or a workbook: transactions, holdings, dividends,
realised and unrealised capital gains, closed positions, performance. The
workbook has an About sheet explaining what each report means and does not
mean — worth reading before sending it on.

Figures that cannot be converted to AUD honestly come out **blank** rather than
converted at 1:1, so nothing wrong gets totalled by a spreadsheet.

In a CSV, text that starts with `=`, `+`, `-` or `@` has a `'` in front, so a
spreadsheet shows it rather than running it as a formula.

## Instruments and the ledger

An instrument's page shows its full ledger: every buy, sell, DRP allocation and
cash distribution, with the gain at the latest price on the units of each
parcel still held.

Rows can be edited and deleted here. Two rules apply:

- **Editing re-checks the whole timeline**, not just the row you changed.
  Reducing a buy from three years ago that a later sell depended on is refused,
  and the message names the sell to fix first.
- **A DRP row is edited through its dividend**, because the cash and the units
  are one event.

### Splits and capital changes

**Record a split or capital change**, on a holding's page, covers a split or
consolidation, a return of capital, and an AMIT cost base adjustment from an
annual tax statement.

- **A split** is new units for old: 2 for 1 doubles the holding, 1 for 10
  consolidates it. Each parcel keeps its cost and its purchase date, so the
  twelve-month discount clock does not restart.
- **A return of capital** is the cash paid back for the whole holding, in its
  own currency. It lowers the cost base of the units held, and anything beyond
  it is a capital gain on the day.
- **An AMIT adjustment** raises or lowers the cost base, for the whole holding.
  A decrease beyond the cost base is a capital gain on the day, as a return of
  capital is.

### Moving a holding to another portfolio

If a trade went into the wrong portfolio, **Move to another portfolio** on the
edit page hands it over. The control only appears when you belong to a second
portfolio you can write to.

It opens a list of everything that portfolio holds for that instrument —
trades and distributions — and you tick what goes. Nothing is copied and no new
instrument is created: it is the same holding in both places.

Three things to know:

- **Anything a remaining sell depends on arrives already ticked.** You can
  untick it, but the move is then refused — leaving it behind would put the
  holding below zero units, and it names the sell that would be short.
- **A reinvested distribution moves with its units.** They are one event and
  cannot be separated, so they share a single tickbox.
- **Cash distributions are yours to choose.** Nothing forces them either way.

If the destination cannot take the selection — a sell arriving somewhere that
holds nothing — it says so and names the trade, rather than guessing which
buys you meant to bring.

Both portfolios' reports change to match, including for financial years already
past. See the [disclaimer](../about/disclaimer.md).

## Account

Password, appearance (yours alone), two-factor, **active sessions** — every
device signed in, when it was last used, and a way to sign out one or all of
the others — and **API keys** for other apps, each reaching the portfolios you
choose and never doing more than you can.

## Admin

Users and portfolio members. Admins can reset a password and clear someone's
two-factor; portfolio owners manage members and the portfolio's settings.

Roles: **owner** (everything), **member** (record and edit), **viewer** (read
everything, change nothing).

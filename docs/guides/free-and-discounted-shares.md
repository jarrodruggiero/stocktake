# Free and discounted shares

Bonus issues, demerger allocations and shares from an employer's plan all arrive
without an ordinary contract note, and the price to record is not always the
price you paid. This page is about **what to type**.

It is not tax advice — see the [disclaimer](../about/disclaimer.md). Stocktake
does arithmetic on the numbers you give it, and this page is about giving it the
right ones.

## A price of zero is allowed

Record it as an ordinary buy with a price of `0`. Units still have to be more
than zero; the price does not.

A holding whose cost base is genuinely zero has no percentage return — there is
nothing to divide by — so gain columns show **N/A** rather than a number. That
is the arithmetic being honest, not a limitation. The dollar gain still works,
and so do the capital-gains reports.

## Shares from an employer's plan

**A cost base of zero is almost certainly wrong here**, even when the shares
were free.

Under an employee share scheme the discount you received is taxed as *income*,
and for capital-gains purposes you are then treated as having acquired the
shares at their **market value**, so that the same amount is not taxed twice.
Recording `0` — or a token cent — makes Stocktake report a capital gain larger
than the real one by very nearly the whole value of the grant.

What to enter:

| | Date | Price per unit |
| --- | --- | --- |
| **Taxed upfront** — taxed in the year you got the shares | the acquisition date | market value on that date |
| **Tax deferred** — taxed later, at the deferred taxing point | the **taxing point** date, not the grant date | market value at the taxing point |

The deferred case matters twice over: the taxing point also restarts the clock
for the 12-month CGT discount, so a grant date typed in by mistake can make a
sale look discountable when it is not.

**Use the figures from your employer's ESS statement.** It is the document the
ATO expects those numbers to match, and it is authoritative in a way a
market close is not — a statement may use a weighted average or a different
convention than the close Stocktake holds. Where the app offers a price it is
offering the close for that date as a convenience; overwrite it.

A note on the trade — "ESS taxed upfront, market value per employer statement"
— appears in the trades export, which is the one your accountant reads.

The ATO's own pages: [ESS and capital gains
tax](https://www.ato.gov.au/businesses-and-organisations/corporate-tax-measures-and-assurance/employee-share-schemes/employees/ess-and-your-tax/ess-and-capital-gains-tax).

## Bonus issues and demergers

Here zero often *is* what you paid, but the cost base is not zero either — it is
normally apportioned across your original and new holdings, so both parcels
change. Stocktake does not do that apportionment for you: work out the split
from the company's statement and record the parcels accordingly.

## What the price field offers you

When you set a trade date, Stocktake fills the price with the **close it holds
for that date**, and says so underneath — including when it has fallen back to
an earlier day, for a date the market did not trade on.

If it holds no close for that date, it leaves the field empty rather than
offering the nearest figure to hand. The same goes for the exchange rate on a
foreign holding. An empty rate is filled in later by the price feed; a wrong one
would stay wrong, because the feed only fills the blanks.

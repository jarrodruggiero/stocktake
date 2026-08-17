# Disclaimer

**This software is not financial advice, and it is not tax advice.**

It performs arithmetic on numbers you give it. It does not know your
circumstances, it has never met your accountant, and it has no opinion about
what you should buy or sell.

## Specifically

- **Every figure is your responsibility to check.** Prices come from a free,
  unofficial source that will occasionally be wrong, late, or missing. Trades
  are whatever you or an importer put in.
- **The tax calculations implement rules as the authors understood them.** FIFO
  parcel matching, the CGT discount, financial-year boundaries and franking
  credits are implemented in good faith and tested against worked examples —
  but they are general rules applied to a general case, and tax is neither.
- **Australian rules only.** Financial years run July to June, capital gains
  use Australian rules, and franking credits are an Australian concept. If you
  are taxed elsewhere, the tax pages do not apply to you.
- **The rules are changing.** From 1 July 2027 the 50% CGT discount is
  scheduled to be replaced by cost-base indexation and a minimum tax rate. This
  version implements the pre-2027 regime and will tell you rather than guess
  when asked about a later year.
- **Use the exports as a starting point for your accountant, not as a lodgement.**

## No warranty

This software is provided "as is", without warranty of any kind, express or
implied. The authors are not liable for any claim, damages or other liability
arising from its use — including financial loss, an incorrect tax return, or a
decision made on a number this app displayed.

See the LICENSE file for the full terms.

## If a number looks wrong

It might be. Please [open an issue](../contributing/index.md) with enough
detail to reproduce it — the trades involved (amounts changed if you prefer),
what you expected, and what you got. Errors in the tax logic are the highest
priority bugs this project has.

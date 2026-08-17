![Stocktake](assets/wordmark-light.png#only-light)
![Stocktake](assets/wordmark-dark.png#only-dark)

A self-hosted share portfolio tracker, built for Australian investors.

It answers the questions a spreadsheet gets asked and then gets wrong: what do I
hold, what did it cost, what is it worth, what did I earn in distributions, and
what will the ATO want to know in July.

## What it does

- **Holdings and performance** — every figure computed from your trades, never
  stored as a snapshot, so a corrected trade corrects the history.
- **Charts you build** — pick a grain, a split and some measures; save it; drag
  it where you want it on the page.
- **Australian tax** — financial years, FIFO capital gains with per-parcel
  detail, the CGT discount, franking credits, and exports shaped for your
  accountant.
- **A buying plan** — a rotation with a schedule, and a page that tells you
  what is next.
- **Imports** — broker CSV exports (configured, not coded) and dividend
  statement PDFs, parsed locally.
- **An API** — read your own numbers from another program.

## What it does with your data

This matters more here than in most software, so it is stated plainly rather
than buried:

- **Only ticker symbols ever leave your machine.** Prices come from public
  market data, requested by symbol. No quantity, no holding, no balance and no
  identifier is sent anywhere.
- **Statement PDFs are parsed on your own hardware.** No cloud OCR, no LLM, no
  upload.
- **There is no telemetry.** No analytics, no crash reporting, no phone-home,
  no update check.
- **Your database is a file you own.** Back it up, move it, read it with
  `sqlite3`, delete it.

## What it is not

- **Not financial advice, and not tax advice.** It does arithmetic on numbers
  you give it. Every figure it produces is your responsibility to check, and a
  tax return is between you and your accountant.
- **Not a broker.** It cannot place trades and never talks to your broker.
- **Not multi-tenant SaaS.** An instance is for one household or a group of
  people who trust each other — see [the security model](about/security.md) for
  exactly what that means.
- **Not built for other countries yet.** Financial years, CGT rules and
  franking are Australian. The structure would accommodate other rules;
  nobody has written them.

## Where to go next

| I want to… | Go to |
| --- | --- |
| Run it for the first time | [Getting started](getting-started/index.md) |
| Understand a page | [Guides](guides/index.md) |
| Change a setting | [Configuration reference](reference/configuration.md) |
| Get back into a locked account | [Recovery](reference/cli.md) |
| Add a broker, a column, a report | [Contributing](contributing/index.md) |
| Know what it does with my data | [Privacy](about/privacy.md) |

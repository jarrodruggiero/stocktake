<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/wordmark-dark.png">
  <img src="docs/assets/wordmark-light.png" alt="Stocktake" width="600">
</picture>

A self-hosted share portfolio tracker, built for Australian investors.

It answers the questions a spreadsheet gets asked and then gets wrong: what do
I hold, what did it cost, what is it worth, what did I earn in distributions,
and what will the ATO want to know in July.

> **Not financial advice and not tax advice.** It does arithmetic on numbers you
> give it. Check every figure, and talk to your accountant.
> See [the disclaimer](docs/about/disclaimer.md).

## Why this one

**Your holdings stay on your machine.** Only ticker symbols ever leave — the
app asks a public service what `ALPHA.AX` closed at, and nothing else. No
quantities, no balances, no identifiers, no telemetry, no update check.
Dividend statement PDFs are parsed locally: no cloud OCR, no LLM, no upload.
Turn the price feed off and it makes no outbound requests at all.

**It knows about Australian tax.** Financial years, FIFO capital gains with
per-parcel detail, the CGT discount, franking credits, and exports shaped for
an accountant.

**Nothing is a snapshot.** Every figure is computed from your trades, so
correcting a trade from 2022 corrects every number that depended on it,
everywhere, at once.

## What it does

- Holdings, valuations and performance, with open and closed positions separated
- Charts you build yourself from a vocabulary of grains, splits and measures —
  saved, draggable, and yours rather than the portfolio's
- FY reports: capital gains, dividend income, franking, year-end snapshot
- Exports as CSV or a workbook, with an About sheet explaining every number
- A buying plan with a rotation and a schedule
- Broker CSV imports (usually configuration, not code) and registry PDF statements
- Multi-user with roles, two-factor authentication, and a read-only JSON API

## Quick start

```sh
docker run -d --name portfolio -p 8000:8000 \
  -v portfolio-config:/config \
  -v portfolio-data:/data \
  -e TZ=Australia/Melbourne \
  <image>
```

Then open `http://localhost:8000`. There is nothing to configure first: with no
configuration file the app boots into a setup wizard that chooses where to keep
the data, creates your account, and writes `config.yaml` for you.

Full instructions, including what to set behind a reverse proxy, are in
[the deployment guide](docs/deploy/index.md).

## Documentation

| | |
| --- | --- |
| [Getting started](docs/getting-started/index.md) | First run, importing, first look around |
| [Guides](docs/guides/index.md) | What each page does |
| [Deployment](docs/deploy/index.md) | Running it properly |
| [Configuration](docs/reference/configuration.md) | Every setting |
| [Command line](docs/reference/cli.md) | Recovery, the price feed, migrations |
| [Contributing](docs/contributing/index.md) | Add a broker, a column, a report, a price source |
| [Privacy](docs/about/privacy.md) · [Security](docs/about/security.md) | What it does with your data, and what it does not protect against |

## Contributing

The most useful contributions are support for **your** broker, **your**
registry's statements, or a price source for **your** country — and the first
of those is usually a few lines of YAML rather than code.

Each of those has a recipe naming the exact files and the test to write:
[docs/contributing](docs/contributing/index.md).

## Status and scope

Australian rules only: financial years run July to June, capital gains use
Australian rules, and franking credits are an Australian concept. The structure
would accommodate other jurisdictions; nobody has written them.

Prices come from a free, unofficial source that occasionally breaks. FX and
crypto have fallbacks; equities do not, for
[reasons worth reading](docs/contributing/recipe-provider.md) before proposing
one.

## Licence

AGPL-3.0. See [LICENSE](LICENSE).

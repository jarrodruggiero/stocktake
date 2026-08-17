# Privacy

This app exists partly because the alternative is typing your entire financial
position into somebody else's website. So it is worth being precise about what
it does, rather than gesturing at "privacy-focused".

## What leaves your machine

**Ticker symbols, to public market-data services.** That is the complete list.

To fetch a price, the app asks a provider what `ALPHA.AX` closed at. It does not
say how many you hold, what you paid, when you bought, who you are, or that the
question relates to a portfolio at all. A provider sees the same request from
someone idly checking a price.

Providers used by default:

| What | Who | Why them |
| --- | --- | --- |
| Prices, FX, distributions | Yahoo Finance (via `yfinance`) | Broad free coverage including ASX |
| FX fallback | Frankfurter | Central-bank rates, no key, no quotas |
| Crypto fallback | CoinGecko | AUD prices natively, no key |

You can see exactly which source served each figure — the `source` column is
stored per row, and the dashboard says so when a fallback was used.

## What never leaves

- **Holdings, quantities, prices paid, balances, gains.** Never transmitted.
- **Dividend statements.** Parsed by `pdfplumber` in your own process. No cloud
  OCR, no LLM, no upload. This is a hard constraint in the code, not a setting.
- **Broker CSV exports.** Parsed locally, previewed, then written to your
  database.
- **Anything at all, if you turn the price feed off.** Set
  `price_feed.enabled: false` and the app makes no outbound requests
  whatsoever. You keep the app; you enter prices yourself.

## No telemetry

No analytics. No crash reporting. No usage statistics. No update check. No
outbound request that is not fetching a price you asked for.

There is no build flag or setting for this, because there is nothing to turn
off.

## Your data is a file

By default the whole database is one SQLite file. Copy it, back it up, open it
with `sqlite3`, delete it. Nothing is held anywhere else, and there is no
account to close.

If you point it at Postgres instead, the same applies to your database.

## What this does not protect against

Said plainly, because a privacy page that only lists good news is not useful:

- **Anyone who can read the database file can read everything.** Holdings,
  session tokens, TOTP secrets. Per-user encryption would need a key derived
  from your password, which would lock out the price feed and the backups. The
  boundary is "can reach the machine", not "has an account" — see
  [the security model](security.md).
- **Traffic is unencrypted unless you put TLS in front of it.** On a home LAN
  over plain HTTP, anyone on that network can read your pages.
- **A hosting provider or tunnel sees what passes through it.** If you expose
  the app through a service that terminates TLS on your behalf, it can see your
  traffic in the clear at its edge.
- **The people you share a portfolio with can see it.** That is the point of
  sharing, but it is worth stating: there is no partial visibility.

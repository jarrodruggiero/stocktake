# Market data

Stocktake can refresh prices and exchange rates for you, or leave them alone
entirely. Either way, **your holdings never leave the machine.**

## What is sent

A request for public market data contains **ticker symbols and nothing else** —
`BHP.AX`, `VAS.AX`, `AUDUSD=X`. It does not send how many units you hold, what
you paid, when you bought, your account, or anything that identifies you.

The provider learns which listed securities this installation is interested in.
It cannot learn what you own.

## Where it comes from

Yahoo Finance, through the `yfinance` library, is the default and needs no API
key.

There are two separate jobs, because they answer different questions:

| | What it does | Setting |
| --- | --- | --- |
| **Daily close** | Records what each day closed at, once a day | `price_feed.enabled` |
| **Live quotes** | Shows what a holding is worth right now | `price_feed.quotes_enabled` |

Live quotes are fetched only while somebody is signed in **and** an exchange
holding something is open — a closed market has no new number to give. One
batched request serves the whole portfolio, every five minutes by default.

## Turning it off

Untick **Automatically refresh market data** during setup, or in your
configuration file:

```yaml
price_feed:
  enabled: false          # no daily close job
  quotes_enabled: false   # no live quotes either
```

With both off, nothing outbound happens at all. You can still record trades, run
reports and enter prices yourself — the app simply stops asking anyone for
numbers.

You can also just move the schedule rather than disabling it:

```yaml
price_feed:
  enabled: true
  hour: 18       # local time, after your market closes
  minute: 30
```

## If a refresh fails

A provider that errors, or returns nothing, is treated as "no answer today" and
the stored prices stay as they were. Fewer rows than were asked for is normal —
markets close — and is not treated as a failure, because treating it as one
would rewrite the same rows from a different source on every run.

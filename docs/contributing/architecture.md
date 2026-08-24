# Architecture

## The one idea

**Everything is computed from trades.** There is no stored balance, no cached
valuation, no nightly snapshot table. A holding is the sum of its trades; a
chart is a walk over those trades priced against stored daily closes; the FY
report is a FIFO match over the same list.

This is the decision that makes everything else simple. Correct a trade you
entered wrong three years ago and every number in the app corrects itself,
because none of them were written down. The cost is that reads do arithmetic —
which is free, because the dataset is small. A six-year portfolio is a few
hundred rows.

Two consequences worth internalising:

- **Never add a column that stores something derivable.** If you find yourself
  wanting `holding.current_value`, the answer is a function, and if it is slow
  the answer is the cache described below.
- **The cache is fingerprinted, not invalidated.** `queries` keeps the daily
  series against a fingerprint of its inputs (max price date; per table the
  row count, max id and max `updated_at`). Nothing has to remember to clear
  it. If you add a table that feeds the series, add it to the fingerprint —
  see `_series_fingerprint`.

## The modules

### The numbers

| Module | Holds |
| --- | --- |
| `models.py` | Tables. Also `trade_order`, the one definition of timeline order. |
| `queries.py` | The read model: holdings, totals, the daily series, `FxBook`. |
| `fyreport.py` | Financial years, FIFO parcel matching, CGT, franking. |
| `charts_build.py`, `fields.py`, `chart_templates.py` | The chart vocabulary. |
| `exports.py` | Every downloadable report. |
| `plans.py`, `calendarview.py` | The DCA rotation and the month calendar. |
| `money.py` | Rendering an amount so it says which currency it is. |

### Getting data in

| Module | Holds |
| --- | --- |
| `brokercsv.py`, `ofx.py`, `importer.py` | Broker exports, OFX, the one-off workbook import. |
| `imports_web.py` | The upload pages themselves. |
| `statements.py`, `ocr.py`, `pagemap.py` | Dividend statements, including scans. All local. |
| `docformats.py`, `brokerdesign.py`, `contribute.py` | Formats as data, the click-to-map designer, and turning one into a pull request. |
| `pricefeed.py`, `providers.py` | Market data, and what happens when a source is down. |

### Who you are, and what you may see

| Module | Holds |
| --- | --- |
| `auth.py` | Passwords, sessions, CSRF, lockout. The one choke point (below). |
| `tenancy.py` | Per-portfolio filtering, applied automatically (below). |
| `twofactor.py`, `recover.py` | TOTP, recovery codes, and getting back in. |
| `passkeys.py` | WebAuthn: enrolling a passkey, and signing in with one. |
| `api.py` | `/api/v1`, authenticated by key rather than cookie. |

### The application around it

| Module | Holds |
| --- | --- |
| `main.py` | Routes. Big, and deliberately flat. |
| `settings.py`, `configfile.py`, `features.py` | Configuration, editing it from inside, and the parts an install can switch off. |
| `db.py`, `clock.py` | The connection, and the app's idea of "today". |
| `markettime.py` | Which clock a trade time is read on. Stored on the exchange's, shown on whichever the account asked for. |
| `columns.py`, `sorting.py`, `navigation.py`, `theming.py`, `branding.py` | What the tables show, how they sort, and how it all looks. |
| `setupwizard.py` | First run: what each step needs and what it writes. |
| `maintenance.py`, `memory.py`, `metrics.py`, `logbuffer.py`, `lifecycle.py` | Housekeeping, the memory the feed gives back, Prometheus, the in-app log, and restarting from inside. |

## Background work

Two loops, started at application startup and independent on purpose.

- **The price feed** runs once at startup and then daily after the market
  closes. It records **closing** prices only, and it asks each exchange only
  for sessions that exchange has finished — `pricefeed.MARKETS` holds the
  timezone and closing time, because "what day is it here" is a different
  question from "what has that market finished doing".
- **The quote poll** fills in the day that has not closed yet, so a holding is
  not stuck on yesterday's close all afternoon. Those rows are flagged
  `price.provisional`, and the daily run resumes from the last **settled** day
  precisely so it comes back and replaces them. It polls only while somebody is
  signed in and a market they hold is trading.

`maintenance.py` runs on its own daily schedule rather than riding along with
the feed, so turning the feed off does not silently stop sessions expiring.

## The two rules that keep it safe

### Tenancy fails closed

Every query for personal data is filtered to the session's portfolio
automatically, by a SQLAlchemy event in `tenancy.py` — not by each query
remembering a `where` clause. A query that touches a scoped table with **no**
portfolio on the session **raises** rather than returning everything.

If you add a table holding personal data, add it to `SCOPED_MODELS`. That is
the entire registration step, and forgetting it is the one way to leak between
portfolios.

Read the `TRUST BOUNDARY` docstring at the top of `tenancy.py` before assuming
more than this gives you. It separates users *within the application*. It is
not a defence against someone holding the database file.

### Auth decisions live at one choke point

`auth.load_session` is the single place a cookie becomes a session, and it is
where every refusal lives: expiry, the idle window, the absolute cap, and the
half-authenticated state during two-factor login. Routes do not re-check these.

The reason is stated in the code and worth repeating: a check that each route
must remember is a check that will eventually be forgotten.

## Time

Dates are decided in the portfolio's configured timezone via `app/clock.py` —
never `datetime.date.today()`, which in a UTC container is yesterday for most
of an Australian morning. Timestamps (session expiry, audit fields) stay in UTC.

`appkit.ensure_utc()` exists because SQLite returns naive datetimes where
Postgres returns aware ones. Any comparison against a stored timestamp goes
through it, or it works on one backend and raises on the other.

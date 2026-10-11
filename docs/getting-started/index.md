# Getting started

This assumes the app is running — see [Deployment](../deploy/index.md) if it
is not.

## 1. Run the setup wizard

The first visit goes to `/setup`, and there is nothing to prepare before it:
if the app has no configuration it will ask, then write one.

The wizard walks through nine steps, and skips the ones your deployment has
already answered:

1. **Welcome** — what it detected: which database, whether it can write a
   configuration file, and whether stopping would result in starting again.
2. **Database** — only if nothing configured one. SQLite (a file) or Postgres,
   with a **Test connection** button that really connects. Nothing is written
   until it works.
3. **Your account** — the first account, and the administrator. There is no
   default password to change and no seeded user.
4. **Recovery codes** — how you get back in if you lose your password or your
   second factor. Save them before going on: they are not shown again.
5. **Two-factor** — a QR to scan, if you ticked it on the account step. It can
   also be turned on later from your account page, along with passkeys — which
   need HTTPS, so they are offered only once TLS is in front.
6. **First portfolio** — its name, whether to refresh market data
   automatically, and your timezone (it can read that from your browser).
7. **Optional features** — the DCA Schedule, if you want it.
8. **Environment** — trusted proxies and the external URL. All optional.
9. **Finish** — it shows you the YAML before writing it, and says which
   settings need a restart.

If someone else has already run it, the wizard is closed and you need them to
add you from **Admin → Accounts**.

## 2. Add what you hold

Two ways, and most people use both.

### Import a broker export

**Imports & Exports → choose your broker → upload the CSV.** Every parsed row
is shown for checking before anything is written. Nothing is saved until you
confirm.

If your broker is not listed, **Map a new broker CSV** on the same page: upload
one of its exports and say which column is which.

### Enter trades by hand

**Record trade.** Instrument, buy or sell, date, units, price, brokerage. If
the instrument does not exist yet, pick *Something not listed…* and create it
inline — the name and currency fill in from the ticker.

Trades can be entered in any order and backdated freely. The one thing the app
refuses is a timeline that cannot have happened — selling units you did not
hold at the time — and it will tell you exactly which trade is in the way.

## 3. Wait for prices

The price feed runs at startup when data is stale, and daily after that. First
run backfills from `price_feed.backfill_start`, so give it a minute on a fresh
install. The Portfolio page shows when it last ran and whether it worked.

**Only ticker symbols are sent.** Nothing about your holdings leaves.

## 4. Look around

| Page | What it answers |
| --- | --- |
| **Portfolio** | What do I hold, what is it worth, how is it doing |
| **Portfolio → a financial year** | What will the ATO want to know |
| **View charts**, on the Portfolio page | Everything else, however you want to see it |
| **DCA Schedule**, if you turned it on | What am I buying next, and when distributions are expected |
| **Imports & Exports** | Getting data in, and exports out |

## 5. Turn on two-factor

**Settings → Two-factor authentication → Set up**, from the menu under your
name. Scan the QR with any authenticator app, type the code it shows to prove
it worked, and save the recovery codes somewhere that is not the phone holding
the authenticator.

This app shows everything you own. It is worth the ninety seconds.

## Things worth knowing early

- **Nothing is a snapshot.** Every figure is computed from your trades, so
  fixing a trade you entered wrong in 2022 fixes every number that depended on
  it, everywhere, immediately.
- **Sessions end.** After an hour of inactivity you get a warning with a
  countdown, and after seven days you sign in again regardless.
- **DRP is one event, not two.** A reinvested distribution is cash *and* units,
  recorded as a linked pair. Edit or delete it through the dividend, and both
  halves move together.
- **The app never talks to your broker.** It cannot place trades and has no
  credentials.

# Configuration reference

Every setting, its default, and what it does.

Configuration comes from three places, in this order of priority:

1. **Environment variables** — `APP_` prefix, `__` between levels.
   `APP_DATABASE__PASSWORD`, `APP_AUTH__COOKIE_SECURE=true`.
2. **The YAML file** at `APP_CONFIG_FILE`.
3. **Built-in defaults.**

**Secrets belong in environment variables, never in the file.** The database
password is read from `APP_DATABASE__PASSWORD` so it can come from a Docker
secret or a Kubernetes Secret without ever being written to disk in the clear.

!!! tip "The file is optional"
    Every setting has a default, so the app starts with no configuration file
    at all — and when it does, it boots into the
    [setup wizard](../getting-started/index.md), which writes one for you from
    the annotated default below.

    That is also why the wizard offers a database step only when nothing else
    has chosen one. If `APP_DATABASE__*` is set, or the file already has a
    `database:` block, the wizard reports what it found and moves on: the
    process is already connected to it, and offering to change it would be
    offering to lose a database.

!!! warning "Unknown keys are rejected"
    A key the app does not recognise is a startup error, not a warning — a
    typo in a setting should be loud rather than silently ignored. It also
    means **the config file and the application version are a matched pair**:
    a file written for a newer version will be rejected by an older one, and
    vice versa. Move them together.

## The annotated file

This is the shipped `config.yaml`. Every option appears, commented out at its
default — uncomment what you want to change.

```yaml
# ─────────────────────────────────────────────────────────────────────────────
#  Stocktake — configuration
#
#  Every option the application understands is listed here. Options that are
#  commented out are showing their DEFAULT: uncomment one only to change it.
#  Anything left commented behaves exactly as written.
#
#  Three places supply configuration, and they win in this order:
#
#      1. environment variables   (highest — always wins)
#      2. this file
#      3. the built-in defaults   (lowest)
#
#  Environment variables use the prefix APP_ and a double underscore between
#  levels, so `auth.rate_limit.max_attempts` is APP_AUTH__RATE_LIMIT__MAX_ATTEMPTS.
#  That is how secrets reach the application without being written down: the
#  database password arrives as APP_DATABASE__PASSWORD and never appears here.
#
#  NEVER put a secret in this file.
#
#  Most of these can also be changed from Admin → Settings inside the
#  application, which writes back to this file and keeps these comments. If the
#  file is supplied read-only — a Kubernetes ConfigMap, a read-only volume, or
#  baked into an image — that page shows the values but cannot save them, and
#  says so.
#
#  A mistyped KEY here stops the application at startup rather than being
#  ignored, which is deliberate: a silently ignored setting is the failure
#  nobody notices. (A mistyped environment variable is simply not seen — the
#  asymmetry is worth knowing.)
# ─────────────────────────────────────────────────────────────────────────────

# The application's name. Also decides the default database filename, so
# changing it points the app at a different database.
app_name: stocktake

# How much detail reaches the log. DEBUG | INFO | WARNING | ERROR.
# Takes effect at startup, so a change needs a restart.
# log_level: INFO

# Where this portfolio lives, as an IANA timezone name.
#
# EVERY date decision is made in this zone: whether a trade is in the future,
# which financial year is current, where a performance window starts, what the
# calendar highlights. Containers run in UTC, so leaving this wrong makes the
# application a day behind for the first hours of each morning — and the new
# financial year arrive a day late. Timestamps (session expiry, audit fields)
# are stored in UTC regardless; only calendar decisions use this.
timezone: Australia/Melbourne

# ── Database ────────────────────────────────────────────────────────────────
# Only ever changed here — the settings page shows it read-only, because
# editing it from a web form is a way to lose a database.
database:
  # sqlite (default) or postgres.
  #
  # SQLite needs nothing else running and is the right choice for one
  # household. Postgres is worth it for concurrent writers, more than one
  # replica, or a database into the gigabytes.
  type: sqlite

  # Where the SQLite file lives. MUST be on a mounted volume that survives a
  # restart — a container's own filesystem does not. Defaults to
  # /data/<app_name>.db.
  path: /data/stocktake.db

  # For `type: postgres` instead of `path` (name and user are required, and
  # the password should come from APP_DATABASE__PASSWORD, never from here):
  # host: postgres
  # port: 5432
  # name: stocktake
  # user: stocktake
  # sslmode: disable

# ── Prices and exchange rates ───────────────────────────────────────────────
# Daily closes from Yahoo Finance. Only ticker SYMBOLS ever leave this machine
# — never your holdings, quantities or balances. Runs once at startup when the
# data is stale, then daily; also runnable by hand with
# `python -m app.pricefeed`.
price_feed:
  # Turn the feed off to freeze valuations at the last stored close.
  # Takes effect at startup, so a change needs a restart.
  # enabled: true

  # When the daily run happens, in the timezone above. Set it after the market
  # you follow has closed.
  hour: 18
  minute: 30

  # How far back to fetch when an instrument is first seen. Earlier means a
  # longer first run and a longer chart. Defaults to 2020-01-01.
  # backfill_start: 2020-01-01

  # ── Live prices ──────────────────────────────────────────────────────────
  # The daily run above records what each day CLOSED at. This shows what a
  # holding is worth right now, so the app is not stuck on yesterday all day.
  #
  # Live prices are stored beside the closes, flagged as provisional, and the
  # daily run replaces each with the real close once its market makes one — so
  # the history keeps only closing prices.
  #
  # One batched request covers every holding, and it runs only while somebody
  # is signed in AND an exchange you hold is trading. Nobody signed in, or
  # every market shut, and it makes no outbound requests at all.
  # quotes_enabled: true
  # quote_interval_minutes: 5

  # The feed follows the top-level `timezone` unless you set this. Only worth
  # setting if the market you track keeps different hours from the zone you
  # count your days in.
  # timezone: Australia/Melbourne

# ── Sign-in ─────────────────────────────────────────────────────────────────
auth:
  # How long a signed-in session lasts. It slides: every request pushes the
  # expiry out again.
  # session_ttl_days: 30

  # Two timeouts, and the shorter one wins. `session_idle_minutes` is time
  # since you last DID something — background polling deliberately does not
  # count, or a tab left open would keep itself alive forever.
  # `session_absolute_days` is a hard ceiling nothing can push out, which is
  # what bounds a stolen cookie.
  # session_idle_minutes: 60
  # session_absolute_days: 7

  # How long before the idle timeout the page starts warning, in seconds.
  # The overlay counts down and offers to stay signed in.
  # idle_warning_seconds: 120

  # Send the session cookie only over HTTPS. Turn this ON once the application
  # is behind TLS. Leave it off on plain HTTP or nobody can sign in.
  # cookie_secure: false

  # The session cookie's name. Worth changing only if it collides with
  # something else on the same domain.
  # cookie_name: pf_session

  # Which peers may be believed when they send X-Forwarded-For, as addresses
  # or CIDR ranges. EMPTY BY DEFAULT, meaning never: anyone can set that
  # header, so trusting it from an arbitrary peer lets a caller choose their
  # own identity — defeating lockout, or forging someone else's to lock THEM
  # out. Set this to your reverse proxy once there is one in front.
  # trusted_proxies: []
  #   - 10.42.0.0/16

  # Lockout after repeated failures, counted per email address AND source
  # address together, so one attacker cannot lock everybody out. This covers
  # the two-factor code stage as well as the password, so a six-digit code
  # cannot be brute-forced by someone who already has the password.
  # rate_limit:
  #   max_attempts: 8       # failures before the lockout starts
  #   window_minutes: 15    # how long failures are remembered
  #   lockout_minutes: 15   # how long the wait lasts

  # ── Passkeys and security keys — RESERVED, nothing reads this yet ─────────
  # Two things must be true before WebAuthn can be switched on:
  #
  #   1. HTTPS. WebAuthn requires a secure context, so http://stocktake.home
  #      cannot do it. That is a browser rule, not a limit of this app.
  #      Public reachability is NOT required — an internal-only hostname with
  #      DNS-01 certificates works perfectly well.
  #   2. A settled canonical domain. A credential binds permanently to the
  #      rp_id it was registered under, so a passkey enrolled at
  #      stocktake.example.com will not work from stocktake.home. Decide the
  #      name BEFORE anyone enrols.
  #
  # rp_id is declared here and never read from the Host or X-Forwarded-Host
  # header: those are client-controlled, and rp_id is the anchor every
  # credential is bound to.
  #
  # If you terminate TLS at a Cloudflare tunnel, note that Cloudflare sees your
  # traffic in plaintext at its edge — fine for many people, worth knowing for
  # an app that shows everything you own.
  # webauthn:
  #   enabled: false
  #   rp_id: stocktake.example.com
  #   rp_name: Stocktake
  #   origins:
  #     - https://stocktake.example.com

# ── Housekeeping ────────────────────────────────────────────────────────────
# A daily sweep, on its own schedule rather than riding along with the price
# feed — turning the feed off should not silently stop expiring sessions.
# It removes expired sessions, forgets old failed sign-ins, and clears
# half-finished imports left in the staging area.
# maintenance:
#   enabled: true
#   hour: 3
#   minute: 30
#   attempt_retention_days: 30   # failed sign-ins are kept only to drive lockout
#   staged_upload_hours: 24      # a half-finished import holds real trade data

# ── Metrics ─────────────────────────────────────────────────────────────────
# A Prometheus endpoint at /metrics. OFF by default: every other route needs a
# session, and a scraper has none, so this is the one anonymous endpoint in the
# app and turning it on should be a decision.
#
# What it publishes is the application's own health — when prices were last
# fetched, whether the last fetch worked, whether the database answers. It says
# nothing about holdings, values or trades, and a test keeps the list closed so
# it stays that way.
#
# Ready-made alert rules live in deploy/monitoring/. The application has no
# alerting of its own and never sends mail.
# metrics:
#   enabled: false

# ── Broker imports ──────────────────────────────────────────────────────────
imports:
  # Whether a broker CSV may create instruments it does not recognise. Off
  # means an unknown ticker stops the import so you can check it first.
  # allow_new_instruments: false

  # Largest upload accepted, in megabytes. A broker CSV or a statement PDF is
  # a few hundred kilobytes. This bounds what the application reads into
  # memory; limiting what a client can SEND is a job for the proxy in front.
  # max_upload_mb: 10

  # Where the statement designer keeps the layouts you build, and its scratch
  # renderings. `templates_dir` holds work you want to keep, so it belongs on
  # a volume that survives a restart. `visual_dir` is scratch — page images
  # from a document you are mapping, deletable at any time, and it must NOT be
  # inside the data directory that gets backed up.
  # templates_dir: /data/formats
  # visual_dir: /scratch/visual
  # Reading statements that arrive as SCANS — a photograph of a page wrapped in
  # a PDF, with no text in it. On by default; where the tesseract program is
  # missing the app says so per upload and carries on.
  #
  # The work is entirely local. Tesseract is a binary in the container with no
  # network of its own, and there is no cloud OCR option here — a dividend
  # statement carries your name, your address and your holder number.
  #
  # Turn it off if you would rather no subprocess ran over an uploaded file.
  # You are still told why a scan produced nothing; that never depends on this.
  # ocr:
  #   enabled: true

  # Broker CSV formats are FILES now, in app/formats/brokers/ — they ship with
  # the app, are tested in CI, and a new one is a pull request rather than a
  # block in one person's config. The interface can install one too.
  #
  # This key still exists to OVERRIDE a shipped format by name, for the case
  # where your broker's export differs from the one that was verified. Anything
  # here wins over the file, so do not paste a copy of a shipped format in:
  # that pins it forever and a later fix to the file would never reach you.
  #
  # brokers:
  #   selfwealth:
  #     kind: mapped              # or commsec_transactions
  #     exchange: ASX
  #     currency: AUD
  #     date_format: "%d/%m/%Y"
  #     columns:                  # the fields the importer needs -> your headings
  #       date: Trade Date
  #       action: Buy/Sell
  #       ticker: Code
  #       units: Units
  #       price: Price
  #       brokerage: Brokerage
```

## Optional features

Whole sections of the application, switchable. Turning one off removes its
navigation, its pages and its API routes — not just the link — so an install
that does not want a feature is not carrying a door to it.

```yaml
features:
  # The DCA calendar: a rotation of what to buy next, on an interval, for an
  # amount. Off means /schedule is gone and nothing prompts about it.
  # dca_schedule: true
```

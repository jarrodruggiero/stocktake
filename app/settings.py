"""Stocktake settings: shared base + this app's own config keys."""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, field_validator, model_validator

from appkit.config import BaseAppSettings

# Where the portfolio lives. Every date decision is made in this zone (see
# app/clock.py), and the price feed schedules against it. Australian by default
# because the financial years, CGT discount and franking this app computes are.
DEFAULT_TIMEZONE = "Australia/Melbourne"


class RateLimitSettings(BaseModel):
    max_attempts: int = 8       # failures per (email, ip) before lockout
    window_minutes: int = 15
    lockout_minutes: int = 15


class WebauthnSettings(BaseModel):
    """Passkeys and security keys, read by `app/passkeys.py`.

    A credential binds permanently to the `rp_id` it was created under, so
    changing it after anyone has enrolled invalidates every passkey. Needs
    HTTPS (a browser rule) and a canonical domain; public reachability is not
    required.

    `rp_id` is declared HERE and never read off Host or X-Forwarded-Host —
    decisions.md #96.
    """

    enabled: bool = False
    rp_id: str | None = None      # e.g. "portfolio.example.com"
    rp_name: str | None = None    # what the browser prompt calls this site
    origins: list[str] = []       # e.g. ["https://portfolio.example.com"]


class AuthSettings(BaseModel):
    session_ttl_days: int = 30   # sliding expiry
    # Two independent limits, and the absolute one cannot be extended by the
    # sliding renewal — decisions.md #44.
    session_idle_minutes: int = 60
    session_absolute_days: int = 7
    # How long before the idle cutoff the browser warns, in seconds. The
    # overlay blurs the page and counts down; see main.SLIDING_EXEMPT for why
    # a background poll must not reset the clock behind it.
    idle_warning_seconds: int = 120
    cookie_secure: bool = False  # LAN is http; flip true behind TLS
    cookie_name: str = "pf_session"
    # Which peers may be believed when they send X-Forwarded-For, as a list of
    # addresses or CIDR ranges. EMPTY BY DEFAULT, which means never — anyone
    # can set that header, so trusting it from an arbitrary peer lets a caller
    # choose their own identity for lockout purposes. Set it to your reverse
    # proxy once there is one in front.
    trusted_proxies: list[str] = []
    rate_limit: RateLimitSettings = RateLimitSettings()
    webauthn: WebauthnSettings = WebauthnSettings()

    _empty_means_defaults = field_validator("rate_limit", mode="before")(
        lambda v: {} if v is None else v
    )
    _empty_webauthn = field_validator("webauthn", mode="before")(
        lambda v: {} if v is None else v
    )


class MaintenanceSettings(BaseModel):
    """Daily housekeeping. Its own schedule rather than a passenger on the
    price feed, so the two can be tuned — or turned off — independently."""

    enabled: bool = True
    hour: int = 3
    minute: int = 30
    # How long failed sign-in attempts are kept. They exist for lockout, not as
    # an audit log, so there is nothing to gain from keeping them for long.
    attempt_retention_days: int = 30
    # How long a half-finished CSV import may sit in the staging area before it
    # is swept up.
    staged_upload_hours: int = 24


class MetricsSettings(BaseModel):
    """A Prometheus endpoint at /metrics.

    **Off by default, and that is the deliberate part.** Every other route in
    this app needs a session; this one cannot have one, because a scraper has no
    cookie and nothing to log in with. So it is opt-in: an upgrade never starts
    answering an anonymous caller on its own.

    What it publishes is health, not holdings — when prices were last fetched,
    whether the last fetch worked, whether the database answers. See
    `app/metrics.py`; the list is pinned closed by a test.
    """

    enabled: bool = False


class PriceFeedSettings(BaseModel):
    enabled: bool = True
    hour: int = 18
    minute: int = 30
    # Unset, this follows the top-level `timezone` — which is what you want
    # unless the market you track keeps different hours from the zone you count
    # your days in.
    timezone: str | None = None
    backfill_start: dt.date = dt.date(2020, 1, 1)

    # Live prices for the session that has not closed yet. Separate from the
    # daily run above because they answer different questions: that one records
    # what a day closed at, this one shows what a holding is worth right now.
    quotes_enabled: bool = True
    # One batched request serves the whole portfolio, so five minutes is ~12
    # requests an hour — cheap enough that the interval is set by what feels
    # live rather than by what the provider will tolerate.
    quote_interval_minutes: int = 5
    # Only while ANY account has a live session AND an exchange holding
    # something is open. Trading hours live in `pricefeed.MARKETS` beside the
    # close times — a fact about an exchange, not a preference.


class BrokerFormat(BaseModel):
    kind: str  # mapped | commsec_transactions
    exchange: str = "ASX"
    currency: str = "AUD"
    date_format: str = "%d/%m/%Y"
    columns: dict[str, str] = {}  # logical field -> CSV header (kind: mapped)
    # This broker's word for a trade type -> the app's. Only needed where the
    # export says something other than buy/sell: "In" for a DRP allotment,
    # "Purchase", "B". Anything unmapped falls back to the built-in vocabulary.
    actions: dict[str, str] = {}
    # The zone a time in this export is written in, where it is NOT the
    # exchange's. An IANA name ("Australia/Perth"). Left empty the times are
    # taken as the market's own, which is what every export checked so far
    # does — set it only for one that demonstrably does not, because a
    # conversion applied to times that never needed it moves them by hours.
    times_zone: str = ""


class OcrSettings(BaseModel):
    """Reading statements that arrive as scans.

    **On by default, because the alternative is a silent failure**: a scanned
    PDF uploads perfectly well and then produces nothing. Where the binary is
    missing the app says so and carries on, so this costs nothing to anyone who
    has no scans.

    Turn it off if you would rather no subprocess ran over an uploaded file on
    your machine. You will still be told, per upload, why a scan produced
    nothing — that answer never depends on this setting.

    The work is entirely local: Tesseract is a binary in the image with no
    network of its own. There is no cloud OCR option here and there will not
    be one; a dividend statement carries a name, an address and a holder
    number. See `app/ocr.py`.
    """

    enabled: bool = True


class ImportSettings(BaseModel):
    # On, because an import of a broker's own export is normally the FIRST
    # thing an install does, and every ticker in it is unknown at that point.
    # What stops a typo becoming an instrument is the preview's resolve step,
    # which shows each new ticker and exchange for correction — decisions.md #99.
    allow_new_instruments: bool = True
    # Largest upload accepted, in megabytes. A broker CSV or a statement PDF is
    # a few hundred kilobytes; the cap is there so a large file cannot be read
    # into memory on a container with a few hundred megabytes to its name.
    max_upload_mb: int = 10
    # Installed templates, as `<dir>/statements/*.yaml` and `<dir>/brokers/*.yaml`.
    # Under /data, not /config: /config is a read-only ConfigMap on Kubernetes.
    templates_dir: str = "/data/formats"
    # Rendered page images of a statement somebody is mapping: 0700, deleted
    # 30 minutes after last use (app/pagemap.py). `/scratch`, deliberately NOT
    # the backed-up data directory — decisions.md #22.
    visual_dir: str = "/scratch/visual"
    # Broker CSV formats. Anything here is layered OVER the formats shipped in
    # app/formats/brokers/, per broker name — so this is for adding your own or
    # correcting a shipped one, not for redeclaring the lot. Read it through
    # `brokers_available()`, never directly, or you get the overrides without
    # the built-ins.
    brokers: dict[str, BrokerFormat] = {}
    ocr: OcrSettings = OcrSettings()

    def user_dir(self, kind: str):
        """`<templates_dir>/statements` or `.../brokers`, as a Path."""
        from pathlib import Path

        return Path(self.templates_dir) / ("statements" if kind == "statement"
                                           else "brokers")

    def brokers_available(self) -> dict[str, BrokerFormat]:
        """Every broker format this install knows: shipped, then installed
        through the interface, then overridden by config.

        The config layer stays last — a value written down in config.yaml is
        the most deliberate of the three and should win."""
        from . import docformats

        merged = docformats.load_broker_formats(extra_dir=self.user_dir("broker"))
        merged.update({k: v.model_dump() for k, v in self.brokers.items()})
        return {k: BrokerFormat(**v) for k, v in merged.items()}


class FeatureSettings(BaseModel):
    """Parts of the app an install can switch off.

    Every field here must have a matching entry in `app/features.py` — the
    registry is what the wizard, the settings page, the nav and the route guard
    all read, and a field with no entry would be a toggle nothing honours.

    Default ON: an install that has never heard of this setting keeps every
    feature it had before the setting existed.
    """

    dca_schedule: bool = True


class PortfolioSettings(BaseAppSettings):
    # `log_level` and `database` come from BaseAppSettings, where `app_name` is
    # REQUIRED. Defaulting it here is what lets the app boot with no config
    # file at all, so the setup wizard can run — decisions.md #33.
    app_name: str = "stocktake"
    timezone: str = DEFAULT_TIMEZONE
    price_feed: PriceFeedSettings = PriceFeedSettings()
    imports: ImportSettings = ImportSettings()
    auth: AuthSettings = AuthSettings()
    maintenance: MaintenanceSettings = MaintenanceSettings()
    metrics: MetricsSettings = MetricsSettings()
    features: FeatureSettings = FeatureSettings()

    # A block whose every line is commented out parses as null, not as an
    # absent key — which is exactly what happens when someone comments out the
    # last option under a heading. Treat it as "use the defaults" instead of
    # refusing to start, because the alternative is a config file that breaks
    # by being tidied.
    _empty_means_defaults = field_validator(
        "price_feed", "imports", "auth", "maintenance", "metrics", "features",
        mode="before",
    )(lambda v: {} if v is None else v)

    @model_validator(mode="after")
    def _feed_follows_the_app_timezone(self) -> PortfolioSettings:
        if self.price_feed.timezone is None:
            self.price_feed.timezone = self.timezone
        return self

"""First-run setup: the steps, what each needs, and what it writes.

  1. welcome     — what was detected: database, config file, restartability.
  2. database    — only when nothing configured one.
  3. account     — the security gate. Everything after runs in a session.
  4. recovery    — codes shown once, before 2FA (decisions.md #66).
  5. 2fa         — only when the account step asked for it.
  6. portfolio   — first portfolio, market data, timezone.
  7. environment — trusted proxies and the external URL. All optional.
  8. finish      — write config.yaml; restart if anything needs one.

Each step is gated on what the one before established: 3 cannot run before 2,
and 4-6 cannot run before 3. The draft lives in memory and nothing is written
until it has been shown to work — decisions.md #65.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from appcore.config import DatabaseSettings

log = logging.getLogger(__name__)

# The steps, in order. The wizard's progress bar and the "which is next"
# question both read this rather than hard-coding an order in two places.
STEPS: tuple[tuple[str, str], ...] = (
    ("welcome", "Welcome"),
    ("database", "Database"),
    ("account", "Your account"),
    ("recovery", "Recovery codes"),
    # Offered only when the account step asked for it, so the rail leaves it
    # out and renumbers otherwise. AFTER the codes on purpose — decisions.md
    # #66 and #49.
    ("2fa", "Two-factor"),
    ("portfolio", "First portfolio"),
    ("features", "Optional features"),
    ("environment", "Environment"),
    ("finish", "Finish"),
)
STEP_KEYS = [key for key, _ in STEPS]


# --------------------------------------------------------------------------- #
# The draft
# --------------------------------------------------------------------------- #

@dataclass
class Draft:
    """Everything the wizard has been told but not yet written down."""

    started: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
    # Step 2 — set only when the wizard chose the database itself. When the
    # deployment configured it, this stays None and nothing is written for it.
    database: DatabaseSettings | None = None
    # Step 4 — the codes as issued, so returning to the step re-shows THEM
    # rather than minting a replacement set. Regenerating on a revisit is
    # silent invalidation: somebody writes them down, presses back, and the
    # list in their hand is dead with nothing on screen saying so.
    recovery_codes: list[str] = field(default_factory=list)
    # Step 3 — whether the account step asked to enrol a second factor. The
    # step is left out of the rail entirely when it did not.
    want_2fa: bool = False
    # Step 5
    portfolio_name: str = ""
    price_feed: bool = True
    timezone: str | None = None
    # Step 6 — which optional features are on. Keyed by `features.Feature.key`;
    # absent means the default, which is on.
    features: dict[str, bool] = field(default_factory=dict)
    # Step 7 — all optional; a blank is "not set", not "set to empty".
    trusted_proxies: list[str] = field(default_factory=list)
    external_url: str | None = None
    # Progress
    done: set[str] = field(default_factory=set)

    def completed(self, step: str) -> None:
        self.done.add(step)

    def has(self, step: str) -> bool:
        return step in self.done


_draft: Draft | None = None


def draft() -> Draft:
    """The wizard's working state, created on first use."""
    global _draft
    if _draft is None:
        _draft = Draft()
    return _draft


def discard() -> None:
    """Forget the draft — the wizard finished, or a test wants a clean one."""
    global _draft
    _draft = None


def in_progress() -> bool:
    return _draft is not None


# --------------------------------------------------------------------------- #
# Which step is next
# --------------------------------------------------------------------------- #

def skipped() -> set[str]:
    """Steps this run does not offer, so the rail and the routing cannot
    disagree about what exists — which is how the loop in decisions.md #109
    was built out of three individually reasonable answers.
    """
    return set() if draft().want_2fa else {"2fa"}


def next_step(*, database_configured: bool, account_exists: bool) -> str:
    """The first step that still has something to do.

    Derived rather than stored, so a reload or a back button lands somewhere
    sensible instead of resuming a position the state no longer supports.
    """
    current = draft()
    if not current.has("welcome"):
        return "welcome"
    if not database_configured:
        return "database"
    if not account_exists:
        return "account"
    # Derived from STEPS, not a second list. The hardcoded one still carried
    # "2fa" after that stopped being a step (decisions.md #49), so this could
    # name a step with no page — harmless while nothing turned the answer into
    # a URL, and a 404 the moment something did.
    skip = skipped()
    for step in STEP_KEYS[STEP_KEYS.index("account") + 1:]:
        if step not in skip and not current.has(step):
            return step
    return STEP_KEYS[-1]


def progress(active: str, *, skip: set[str] | None = None) -> list[dict]:
    """The step list as the template renders it: name, state, number.

    A step that does not apply is left out and the rest are numbered as shown;
    `done` beats `skipped`, which is the bug that was hiding here —
    decisions.md #60.
    """
    skip = skip or set()
    current = draft()
    out = []
    for index, (key, label) in enumerate(STEPS):
        # Completed explicitly, or simply behind us — but "behind us" does not
        # apply to a step that was never offered. Without that second clause a
        # skipped step earlier in the list counts as done and can never be
        # left out, which is the whole point of `skip`.
        done = current.has(key) or (
            STEP_KEYS.index(active) > index and key not in skip)
        if key == active:
            state = "active"
        elif done:
            state = "done"
        elif key in skip:
            continue        # not applicable here: leave it out entirely
        else:
            state = "todo"
        out.append({"key": key, "label": label, "state": state,
                    "number": len(out) + 1})
    return out


# --------------------------------------------------------------------------- #
# Step 2 — the database form
# --------------------------------------------------------------------------- #

def sqlite_settings(path: str) -> DatabaseSettings:
    return DatabaseSettings(type="sqlite", path=path.strip() or "/data/portfolio.db")


def postgres_settings(*, host: str, port: str, name: str, user: str,
                      password: str) -> DatabaseSettings:
    """Build Postgres settings from the form, raising ValueError with something
    worth reading rather than a pydantic dump."""
    host = host.strip()
    name = name.strip()
    user = user.strip()
    if not host:
        raise ValueError("Enter the database server's hostname or address.")
    if not name:
        raise ValueError("Enter the name of the database to use.")
    if not user:
        raise ValueError("Enter the username to connect as.")
    try:
        port_number = int(str(port).strip() or "5432")
    except ValueError:
        raise ValueError("The port must be a whole number.") from None
    if not 1 <= port_number <= 65535:
        raise ValueError("The port must be between 1 and 65535.")
    return DatabaseSettings(type="postgres", host=host, port=port_number,
                            name=name, user=user, password=password)


def database_config(db: DatabaseSettings) -> dict:
    """The `database:` block to write for a chosen database.

    The password is included because there is nowhere else for it to go on a
    first run, and the block is only written when the wizard chose the database.

    A deployment that would rather keep the password in a Secret sets
    `APP_DATABASE__PASSWORD`, which wins at runtime because environment beats
    the config file. It does NOT change this page: the field is still shown and
    whatever is typed is still written here, so leave it blank to keep the
    password out of the file. "Test connection" probes with the form's values
    rather than the environment, so with the field blank it will fail against a
    server that wants one — trust the app's own startup instead.
    """
    if db.type == "sqlite":
        return {"type": "sqlite", "path": db.path}
    return {"type": "postgres", "host": db.host, "port": db.port,
            "name": db.name, "user": db.user, "password": db.password}


# --------------------------------------------------------------------------- #
# Step 5 — timezone
# --------------------------------------------------------------------------- #

def timezones() -> list[str]:
    """Every IANA zone this machine knows, sorted for a searchable dropdown."""
    return sorted(available_timezones())


def timezone_problem(name: str) -> str | None:
    name = (name or "").strip()
    if not name:
        return "Choose a timezone."
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return (f"{name!r} is not a timezone this system knows. Pick one from "
                f"the list, or use an IANA name like Australia/Melbourne.")
    return None


# --------------------------------------------------------------------------- #
# Step 6 — environment
# --------------------------------------------------------------------------- #

def parse_proxies(raw: str) -> list[str]:
    """Split a textarea of addresses and CIDR ranges, validating each.

    Validated rather than trusted because the consequence of a typo is silent:
    an unparseable entry would simply never match, and the app would go on
    reading `X-Forwarded-For` from nobody while looking configured.
    """
    entries = [part.strip() for part in re.split(r"[,\s]+", raw or "") if part.strip()]
    out: list[str] = []
    for entry in entries:
        try:
            ipaddress.ip_network(entry, strict=False)
        except ValueError:
            raise ValueError(
                f"{entry!r} is not an IP address or CIDR range. Use something "
                f"like 10.1.0.0/16 or 172.18.0.5."
            ) from None
        out.append(entry)
    return out


@dataclass(frozen=True)
class ExternalUrl:
    """A URL the app will be reached at, and what follows from it."""

    url: str
    scheme: str
    host: str
    port: int | None

    @property
    def secure(self) -> bool:
        return self.scheme == "https"

    @property
    def display(self) -> str:
        return self.url


def parse_external_url(raw: str) -> ExternalUrl | None:
    """The address people will type, checked for the things that break silently.

    Only http and https: the app is reached over one of those, and accepting
    anything else would produce a redirect at the end of the wizard that goes
    nowhere.
    """
    raw = (raw or "").strip().rstrip("/")
    if not raw:
        return None
    if "://" not in raw:
        raise ValueError("Include the scheme, e.g. https://portfolio.example.com")
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise ValueError("The URL must start with http:// or https://")
    if not parts.hostname:
        raise ValueError("That URL has no hostname.")
    if parts.path:
        raise ValueError(
            "Use the base URL only — this app is served from the root of a "
            "hostname, not from a path underneath one."
        )
    return ExternalUrl(url=raw, scheme=parts.scheme, host=parts.hostname,
                       port=parts.port)


def tls_note(url: ExternalUrl | None) -> str:
    """What an HTTPS URL implies, said before it is chosen rather than after.

    The app never terminates TLS. Saying so here is not pedantry: somebody who
    expects it to will put https:// in this box, get a secure cookie, reach the
    app over plain HTTP anyway, and be unable to sign in — with nothing on
    screen connecting the two.
    """
    if url is None or not url.secure:
        return ""
    return (
        "This app does not terminate TLS itself — a reverse proxy (Caddy, "
        "Caddy, nginx, or your ingress) does, and forwards to this container "
        "over plain HTTP. Setting an https:// URL here marks the session cookie "
        "secure, which means the browser will only send it over HTTPS: reach "
        "the app directly on its HTTP port after this and you will not be able "
        "to sign in."
    )


# --------------------------------------------------------------------------- #
# Step 7 — what gets written
# --------------------------------------------------------------------------- #

def config_values(current: Draft) -> dict:
    """The config.yaml the wizard produces, as a nested dict.

    Only what was actually chosen. Everything omitted keeps its documented
    default, which is why the shipped file is almost entirely comments — a
    config listing every value would make every default a decision somebody has
    to maintain.
    """
    values: dict = {}
    if current.database is not None:
        values["database"] = database_config(current.database)
    if current.timezone:
        values["timezone"] = current.timezone
    values["price_feed"] = {"enabled": current.price_feed}
    auth: dict = {}
    if current.trusted_proxies:
        auth["trusted_proxies"] = list(current.trusted_proxies)
    if current.external_url is not None:
        url = parse_external_url(current.external_url)
        if url is not None and url.secure:
            auth["cookie_secure"] = True
    if auth:
        values["auth"] = auth
    # Only what was switched OFF. Writing `dca_schedule: true` would put a line
    # in the file for a default nobody changed, and the shipped config is
    # almost all comments precisely so that every value in it is a decision.
    off = {key: False for key, on in current.features.items() if not on}
    if off:
        values["features"] = off
    return values


def needs_restart(current: Draft) -> list[str]:
    """Which of the chosen values will not take effect until the process
    restarts. Named individually, because "some settings need a restart" tells
    nobody whether the thing they came for is one of them.
    """
    pending = []
    if current.database is not None:
        # The connection is live already — the config file is what makes it
        # survive, and that is the part a restart proves.
        pending.append("the database connection (already in use; the restart "
                       "confirms it is written down correctly)")
    if not current.price_feed:
        pending.append("turning the market-data feed off")
    if current.trusted_proxies:
        pending.append("trusted proxies")
    url = parse_external_url(current.external_url or "")
    if url is not None and url.secure:
        pending.append("HTTPS-only session cookies")
    return pending

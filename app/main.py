"""Portfolio tracker entrypoint.

Run locally:
    APP_CONFIG_FILE=./config.yaml APP_DATABASE__PATH=./portfolio.db \
        uvicorn app.main:app --reload

Access control has two independent layers, on purpose:
  * `require_login` (middleware) — nothing but the probes, the static files and
    the login/setup pages is reachable logged out.
  * `tenancy` — every query for personal data is filtered to the session's user,
    so even a route that forgets to filter cannot serve another portfolio.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import threading
from contextlib import asynccontextmanager, contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

from fastapi import Form, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session as DbSession

from appkit import (
    create_app,
    ensure_utc,
    load_config,
)
from appkit.config import DatabaseSettings

from . import (
    auth,
    branding,
    brokerdesign,
    calendarview,
    chart_templates,
    charts_build,
    clock,
    columns,
    configfile,
    exports,
    features,
    fields,
    fyreport,
    lifecycle,
    logbuffer,
    maintenance,
    markettime,
    memory,
    metrics,
    money,
    navigation,
    plans,
    pricefeed,
    queries,
    setupwizard,
    sorting,
    tenancy,
    theming,
    twofactor,
)
from . import (
    db as database,
)
from .api import router as api_router
from .imports_web import router as imports_router
from .models import (
    MARKET_OPEN,
    MEMBER_ROLES,
    NAV_STYLES,
    ROLE_BLURBS,
    ROLE_LABELS,
    THEMES,
    TIME_ZONES_SHOWN,
    ApiKey,
    Dividend,
    HoldingPref,
    Instrument,
    InvestmentPlan,
    PlannedPurchase,
    Portfolio,
    PortfolioMember,
    Price,
    SavedChart,
    Trade,
    User,
    UserSession,
    exchange_problem,
    ticker_problem,
)
from .settings import PortfolioSettings

log = logging.getLogger(__name__)
APP_DIR = Path(__file__).parent

# Where the published documentation lives. ONE constant rather than a URL typed
# into each template that links out, because a search-and-replace across
# templates is how one gets missed.
DOCS_URL = "https://jarrodruggiero.github.io/stocktake"

settings = load_config(PortfolioSettings)
# Before anything can ask what day it is: a container runs in UTC, and every
# date decision in this app belongs to the portfolio's own zone.
clock.configure(settings.timezone)
# Root-logger setup so app INFO lines (price feed runs, warnings) reach the pod
# logs — uvicorn only configures its own loggers, leaving ours invisible.
logging.basicConfig(
    level=str(settings.log_level).upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# Keep the last few hundred WARNING-and-above lines in memory for the Settings
# page's console. Installed here rather than in the lifespan so it is
# collecting before the first request — startup is exactly when the
# interesting failures happen.
logbuffer.install()
# Where the database comes from, and whether there is one at all. Connecting is
# deliberately conditional so a fresh install can serve the wizard —
# decisions.md #41.
database.set_migrations(str(APP_DIR / "migrations"))
db_source = database.source(settings)
if db_source.configured:
    database.connect(settings.database)


def SessionLocal():
    """A database session.

    A function rather than the sessionmaker itself, because the factory behind
    it is built at connect time and can be replaced by the wizard. Every call
    site keeps working unchanged; the tests still repoint this name directly.
    """
    return database.session()

# Reachable without a session: probes, the login/setup flow, and the assets
# those pages need. `/login/code`, `/session/status` and `/metrics` are public
# to the MIDDLEWARE only, and each guards itself — decisions.md #29.
PUBLIC_PATHS = {"/healthz", "/readyz", "/login", "/login/code", "/login/recover",
                "/setup", "/session/status", "/metrics"}
# Two prefixes the login middleware waves through, each of which guards
# itself. **Both failure modes are silent publication of an endpoint:**
#   * a route under /api/ that does not open with `auth.api_session(...)`
#   * a route under /setup/ that does not open with `_wizard_step()`
PUBLIC_PREFIXES = ("/static/", "/api/", "/setup/")
# The subset that must answer with NO database at all. Everything else — /login
# included — needs one to say anything useful, and gets sent to the wizard
# instead of a 500 from its first query.
NO_DATABASE_PATHS = {"/healthz", "/readyz", "/setup", "/metrics"}
NO_DATABASE_PREFIXES = ("/static/", "/setup/")


def _db_ready() -> bool:
    """Readiness, which is not the same question as "is there a database".

    An unconfigured install is *ready*: it can serve the wizard, which is the
    only thing there is to serve and the only way to make it into anything
    more. Reporting not-ready would be a deadlock — Kubernetes would keep
    traffic away from the page that fixes it.
    """
    if not database.is_ready():
        return True
    try:
        with SessionLocal() as s:
            s.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Price feed
# --------------------------------------------------------------------------- #

# Last feed outcome, shown on the dashboard so a stalled feed is visible
# instead of silently serving stale prices. In-memory is fine: one process.
feed_status: dict = {"running": False, "finished": None, "ok": None, "error": None,
                     "counts": None, "degraded": {}, "quoted": None, "quotes": 0}
_feed_lock = threading.Lock()


def _run_feed() -> bool:
    if not _feed_lock.acquire(blocking=False):
        return True  # already in flight (manual refresh raced the schedule)
    feed_status["running"] = True
    try:
        # Market data is shared and the FX repair pass spans every user's
        # trades, so this is the one place tenancy is deliberately bypassed.
        with tenancy.unscoped_session(SessionLocal) as s:
            counts = pricefeed.run_feed(s, settings)
        # Only the symbols a fallback served, or that nobody could serve. The
        # normal case is silence; a list here means Yahoo let us down for
        # something, which is exactly what goes unnoticed otherwise.
        feed_status.update(
            ok=True,
            error=None,
            counts=counts,
            degraded={
                key: summary
                for key, summary in pricefeed.last_run_sources.items()
                if "failed" in summary or "no rows" in summary
            },
        )
        return True
    except Exception as exc:
        log.exception("price feed run failed")
        feed_status.update(ok=False, error=f"{type(exc).__name__}: {exc}", counts=None)
        return False
    finally:
        # In `finally` so a run is counted whichever branch returned. Reads the
        # outcome back off `feed_status` rather than tracking a second flag,
        # because both branches have just written it and two sources of the
        # same truth is how they drift.
        metrics.record_feed_run(ok=bool(feed_status["ok"]))
        feed_status.update(
            running=False,
            finished=dt.datetime.now(ZoneInfo(settings.price_feed.timezone)),
        )
        _feed_lock.release()


_quote_lock = threading.Lock()


def _run_quotes() -> int:
    """One batched live-price refresh. Never raises: a quote is a nicety."""
    if not _quote_lock.acquire(blocking=False):
        return 0
    try:
        with tenancy.unscoped_session(SessionLocal) as s:
            written = pricefeed.refresh_quotes(s, settings)
        if written:
            # The panel says "Last refreshed <time>", and before this it only
            # knew about the daily close run — so a dashboard whose prices were
            # five minutes old reported them as an hour old, and the one place
            # somebody checks that question answered it wrongly. A mechanism
            # nobody can see is indistinguishable from one that is not running.
            feed_status.update(
                quoted=dt.datetime.now(ZoneInfo(settings.price_feed.timezone)),
                quotes=written,
            )
        return written
    except Exception:
        # A failed quote does NOT set `ok=False`: that flag drives the alert on
        # the thing which keeps history correct, and a missing live price makes
        # nothing wrong — the page falls back to the last close. Raising the
        # alarm here would train exactly the habit this design removed.
        log.warning("quote refresh failed", exc_info=True)
        return 0
    finally:
        _quote_lock.release()


def _live_exchanges(open_positions: list) -> list[str]:
    """Which exchanges are trading right now, among things actually HELD.

    Built from the positions the page is showing, not the instrument table —
    decisions.md #16. Held crypto counts; unheld crypto cannot speak for it.
    """
    return sorted({
        h.instrument.exchange
        for h in open_positions
        if h.instrument.yahoo_symbol and pricefeed.is_trading(h.instrument.exchange)
    })


def _anyone_signed_in(db: DbSession) -> bool:
    """Whether ANY account currently has a live session.

    "Anyone", not "this person": one user signing out while another is still
    signed in must not stop the polling. Logging out deletes the row.

    Timeouts are recomputed the way `auth` does rather than trusting
    `expires_at`, which carries only the sliding cap. Compared in Python via
    `ensure_utc` because SQLite returns naive datetimes.
    """
    now = dt.datetime.now(dt.timezone.utc)
    idle = dt.timedelta(minutes=settings.auth.session_idle_minutes)
    absolute = dt.timedelta(days=settings.auth.session_absolute_days)
    for last_seen, created, awaiting in db.execute(
        select(UserSession.last_seen_at, UserSession.created_at,
               UserSession.awaiting_totp)
    ).all():
        if awaiting:  # half-way through sign-in is not signed in
            continue
        if ensure_utc(last_seen) + idle > now and ensure_utc(created) + absolute > now:
            return True
    return False


def _should_poll_quotes() -> bool:
    """Both conditions, in one session: somebody here AND a market moving."""
    try:
        with tenancy.unscoped_session(SessionLocal) as s:
            return _anyone_signed_in(s) and pricefeed.any_market_trading(s)
    except Exception:
        log.warning("could not decide whether to poll quotes", exc_info=True)
        return False


async def _quote_loop() -> None:
    """Live prices while somebody is signed in and something is trading.

    Nobody there means nobody can see the answer; every market shut means it
    cannot have changed. Either way the app goes quiet until the evening run.
    """
    loop = asyncio.get_running_loop()
    await _await_database("live quotes")
    interval = max(1, settings.price_feed.quote_interval_minutes) * 60
    while True:
        try:
            if await loop.run_in_executor(None, _should_poll_quotes):
                await loop.run_in_executor(None, _run_quotes)
        except Exception:
            log.exception("quote loop iteration failed")
        await asyncio.sleep(interval)


def refresh_quotes_soon() -> None:
    """Kick off a live refresh without waiting for it.

    Called at sign-in: the first thing somebody does is look at what their
    holdings are worth, and the poll below would otherwise leave them on the
    last close for up to a whole interval.
    """
    if not settings.price_feed.quotes_enabled:
        return
    try:
        asyncio.get_running_loop().run_in_executor(None, _run_quotes)
    except RuntimeError:  # no loop (CLI/tests) — the poll will catch it
        pass


async def _await_database(what: str) -> None:
    """Block until the wizard has chosen a database.

    Both background loops start at process startup, and on a fresh install
    there is nothing for them to talk to yet — the wizard has not run. Without
    this they fire immediately and fill the very first page of the log with
    tracebacks, which is a poor first impression and buries the one line that
    matters ("Uvicorn running on…").

    Polling rather than an event because the connection can also be made from
    outside this loop's world, and a poll cannot miss an edge.
    """
    if database.is_ready():
        return
    log.info("%s waiting: no database configured yet (finish the setup wizard)", what)
    while not database.is_ready():
        await asyncio.sleep(2)
    log.info("%s starting: database is now configured", what)


async def _feed_loop() -> None:
    loop = asyncio.get_running_loop()
    await _await_database("price feed")
    # Catch-up on boot (first deploy backfills to 2020), then daily at hh:mm.
    ok = await loop.run_in_executor(None, _run_feed)
    tz = ZoneInfo(settings.price_feed.timezone)
    while True:
        now = dt.datetime.now(tz)
        target = now.replace(
            hour=settings.price_feed.hour, minute=settings.price_feed.minute,
            second=0, microsecond=0,
        )
        if target <= now:
            target += dt.timedelta(days=1)
        if not ok:
            # A failed run retries every 30 min rather than leaving prices
            # stale until tomorrow; the daily fire time still wins if sooner.
            target = min(target, now + dt.timedelta(minutes=30))
        await asyncio.sleep((target - now).total_seconds())
        ok = await loop.run_in_executor(None, _run_feed)


def _next_run(now: dt.datetime, hour: int, minute: int) -> dt.datetime:
    """The next occurrence of hh:mm, today or tomorrow."""
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return target if target > now else target + dt.timedelta(days=1)


async def _maintenance_loop() -> None:
    """Housekeeping, once at startup and daily thereafter.

    Its own loop rather than a step inside the feed's: the two answer to
    different things, and turning the feed off should not silently stop
    expiring sessions.
    """
    loop = asyncio.get_running_loop()
    await _await_database("maintenance")
    tz = ZoneInfo(settings.price_feed.timezone or settings.timezone)
    while True:
        try:
            await loop.run_in_executor(
                None, maintenance.run, SessionLocal, settings)
        except Exception:
            log.exception("maintenance run failed")
        now = dt.datetime.now(tz)
        target = _next_run(now, settings.maintenance.hour, settings.maintenance.minute)
        await asyncio.sleep((target - now).total_seconds())


@asynccontextmanager
async def lifespan(_app):
    # One line at startup saying what this pod is actually holding. Without it,
    # "why does an app serving six pages want 190 MiB" needs a profiler; with
    # it, the answer is in the first line of the log.
    memory.log_snapshot("at startup")
    tasks = []
    if settings.price_feed.enabled:
        tasks.append(asyncio.create_task(_feed_loop()))
    if settings.price_feed.enabled and settings.price_feed.quotes_enabled:
        tasks.append(asyncio.create_task(_quote_loop()))
    if settings.maintenance.enabled:
        tasks.append(asyncio.create_task(_maintenance_loop()))
    yield
    for task in tasks:
        task.cancel()


app = create_app(
    settings,
    templates_dir=str(APP_DIR / "templates"),
    static_dir=str(APP_DIR / "static"),
    readiness_check=_db_ready,
    lifespan=lifespan,
)
app.state.session_factory = SessionLocal
templates = app.state.templates


# --------------------------------------------------------------------------- #
# Template filters/globals
# --------------------------------------------------------------------------- #

def _money_plain(v) -> str:
    return money.plain(v)


def _money(v, currency: str | None = None):
    """The digits, no symbol — for table cells, whose column header names the
    currency. See `app/money.py` for why the symbol is not repeated per cell.

    Wrapped so dollar values can be blurred by the privacy toggle ("glass
    brick"). Use money_plain inside HTML attributes (placeholders etc.).
    """
    from markupsafe import Markup, escape

    return Markup(f'<span class="m">{escape(money.plain(v))}</span>')


def _cash(v, currency: str | None = None):
    """The amount WITH its symbol, for headline numbers that have no column
    header to carry it. The symbol goes inside the span so the privacy overlay
    blurs it along with the digits rather than leaving it sharp beside one."""
    from markupsafe import Markup, escape

    return Markup(f'<span class="m">{escape(money.text(v, currency))}</span>')


# The admin cog renders in the top bar on every page, so it is a global rather
# than something every route has to remember to pass.
templates.env.globals["admin_nav"] = navigation.ADMIN_ITEM
# What a person reads on a page, from the one module that knows it. A global
# because the login card needs it while signed OUT, where there is no route
# context to hang it on — and because the alternative is the name typed into a
# template, which is what `branding.NAME` exists to stop.
templates.env.globals["app_name"] = branding.NAME
# The currency totals are computed in. A global so no template writes "AUD",
# which is what makes T28b's per-portfolio reporting currency one change.
templates.env.globals["reporting_ccy"] = money.REPORTING
# Exposed to every template, so a link to the docs is `{{ docs_url }}` rather
# than an address repeated in a dozen files.
templates.env.globals["docs_url"] = DOCS_URL
templates.env.globals["role_labels"] = ROLE_LABELS
templates.env.globals["role_blurbs"] = ROLE_BLURBS
# Spreadsheet column letters: A..Z, AA, AB. Nobody counts to the 29th column.
templates.env.globals["column_letter"] = brokerdesign.column_letter
# A trade's moment on the exchange's clock, offset attached, for a <time
# datetime="...">. localtime.js rewrites those to the viewer's clock when the
# account asked for it; without JS the exchange's own time still renders.
templates.env.globals["market_stamp"] = lambda d, t, ex: (
    markettime.aware(d, t, ex).isoformat() if markettime.aware(d, t, ex) else ""
)

templates.env.filters["money"] = _money
templates.env.filters["cash"] = _cash
templates.env.filters["money_plain"] = _money_plain
templates.env.filters["qty"] = lambda v: f"{v.normalize():f}" if v is not None else "—"
templates.env.filters["pct"] = lambda v: f"{v * 100:+.2f}%" if v is not None else "—"
# Cache-buster for static assets: browsers kept serving stale charts.js across
# releases (StaticFiles sends no Cache-Control). Any asset change → new URL.
templates.env.globals["asset_v"] = str(
    int(max(p.stat().st_mtime for p in (APP_DIR / "static").rglob("*") if p.is_file()))
)


# --------------------------------------------------------------------------- #
# Session plumbing
# --------------------------------------------------------------------------- #

# Both live in auth.py so the imports router shares them verbatim.
scoped = auth.scoped_session


@contextmanager
def anon():
    """A session for the pre-login flows (users, sessions, login attempts)."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _users_exist(db: DbSession) -> bool:
    return (db.scalar(select(func.count()).select_from(User)) or 0) > 0


def _set_session_cookie(resp: Response, raw_token: str) -> None:
    # A session now exists, so somebody is about to look at what their holdings
    # are worth. Kick a live refresh off here rather than in each of the five
    # sign-in paths (password, second factor, recovery code, passkey, setup) —
    # this is the one line all of them share, and it is exactly the moment the
    # answer stops being hypothetical.
    refresh_quotes_soon()
    resp.set_cookie(
        settings.auth.cookie_name,
        raw_token,
        max_age=settings.auth.session_ttl_days * 86400,
        httponly=True,
        samesite="lax",
        secure=settings.auth.cookie_secure,
        path="/",
    )


# MUST comfortably outlive the session idle window, or the login page you were
# just sent to fails its own CSRF check — decisions.md #94.
PRE_AUTH_CSRF_SECONDS = 12 * 3600


def _set_pre_auth_csrf(resp: Response, token: str) -> None:
    resp.set_cookie(
        auth.PRE_AUTH_CSRF_COOKIE,
        token,
        max_age=PRE_AUTH_CSRF_SECONDS,
        httponly=False,  # double-submit token; paired with a hidden form field
        samesite="lax",
        secure=settings.auth.cookie_secure,
        path="/",
    )


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _render(request: Request, ctx, name: str, extra: dict) -> HTMLResponse:
    """Render with what the chrome always needs: auth, the nav list, and which
    entry to highlight (routes pass `active_nav`)."""
    return templates.TemplateResponse(
        request,
        name,
        {
            "auth": ctx,
            "nav": navigation.nav_for(ctx, settings),
            # The idle overlay reads this off <body> so the warning lead time is
            # configurable without templating JavaScript.
            "idle_warning_seconds": settings.auth.idle_warning_seconds,
            # Set here rather than per route: it belongs to every page, and an
            # inline style outranks both `:root` and `body[data-theme=...]`.
            "theme_style": theming.css_variables(
                getattr(ctx.user, "theme_colors", None) if ctx else None),
            **extra,
        },
    )


@app.middleware("http")
async def optional_features(request: Request, call_next):
    """Refuse the routes owned by a switched-off optional feature.

    Middleware rather than a check in each route, because a feature owns a
    *family* of paths — `/schedule`, `/schedule/save`, `/schedule/skip`, `/schedule/complete` —
    and a per-route guard is a list that falls behind the routes. `features.py`
    names one prefix and this covers everything under it, POSTs included: with
    the nav entry hidden, a stale tab is exactly how a write to a switched-off
    feature arrives.

    **410 Gone, not 404.** The page exists and is turned off; saying "not
    found" would send somebody hunting for a typo. The message names the
    feature and says where to turn it back on.
    """
    feature = features.owning(request.url.path)
    if feature is not None and not features.enabled(settings, feature.key):
        return PlainTextResponse(
            f"{feature.name} is switched off for this install. "
            "An administrator can turn it back on under Admin → Settings.",
            status_code=410,
        )
    return await call_next(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Headers every response carries.

    Nothing loads from a CDN, so every CSP source is `'self'`.

    **`'unsafe-inline'` is present and is the weak part** — the templates still
    carry inline blocks and handlers, so the real XSS defence is the escaping,
    not this: decisions.md #97.
    """
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        # See the note above: inline handlers remain in the templates.
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "form-action 'self'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'",
    )
    return response


@app.middleware("http")
async def require_login(request: Request, call_next):
    """Single gate for the whole app — a new route is protected by default.

    Unauthenticated HTML requests are sent to /login; anything else gets a 401
    rather than a redirect, so a stale fetch doesn't silently render a page.
    """
    path = request.url.path
    # Serve-without-a-database: static files, the health probes, the wizard
    # itself and the metrics endpoint, which guards its own access.
    if path.startswith(NO_DATABASE_PREFIXES) or path in NO_DATABASE_PATHS:
        return await call_next(request)
    # No database means no sessions, no users, and nothing any other route can
    # do. Everything goes to the wizard rather than to a 500 from the first
    # query — this is the state a fresh container starts in.
    #
    # This is checked BEFORE the public paths, not after. /login is public and
    # opens a session to look for the account, so an unconfigured app answered
    # it with `NotConfigured` — a 500 on the one page somebody lands on after
    # any redirect, which reads as the app being broken beyond recovery rather
    # than as "finish setting me up".
    if not database.is_ready():
        if "text/html" not in request.headers.get("accept", ""):
            return PlainTextResponse("not set up yet — visit /setup", status_code=503)
        return _redirect("/setup")
    if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
        return await call_next(request)
    with anon() as db:
        ctx = auth.load_auth(request, db)
        logged_in = ctx is not None
        first_run = not logged_in and not _users_exist(db)
        must_change = logged_in and ctx.user.must_change_password
    if not logged_in:
        if "text/html" not in request.headers.get("accept", ""):
            return PlainTextResponse("not authenticated", status_code=401)
        return _redirect("/setup" if first_run else "/login")
    # A temporary password must be replaced before anything else is reachable.
    if must_change and path not in ("/profile", "/profile/password", "/logout"):
        return _redirect("/profile")
    # The wizard creates the database and the account BEFORE it has finished —
    # step 3 has to write the account somewhere — so from there on a session
    # exists and every page answers. Leaving mid-wizard then means a
    # half-configured app with no timezone, no portfolio and no config file
    # written, reached by deleting `/setup` from the address bar. Setup is
    # finished when the draft is discarded, so this closes the gap without the
    # wizard needing to know about it. `/logout` stays open: abandoning it is a
    # legitimate thing to want.
    if setupwizard.in_progress() and path != "/logout":
        if "text/html" not in request.headers.get("accept", ""):
            return PlainTextResponse("setup is not finished", status_code=503)
        return _redirect(_wizard_url())
    return await call_next(request)


app.include_router(imports_router)
app.include_router(api_router)


# --------------------------------------------------------------------------- #
# First run: the setup wizard — the HTTP half of app/setupwizard.py.
#
# EVERY route under /setup must open with `_wizard_step()`: /setup/ is public,
# so one without that call is a route anybody can post to (decisions.md #92).
# --------------------------------------------------------------------------- #

def _accounts_exist() -> bool:
    """Whether anybody has signed up, answered safely with no database."""
    if not database.is_ready():
        return False
    try:
        with anon() as db:
            return _users_exist(db)
    except Exception:
        # A configured database that will not answer. Not "no accounts" — the
        # wizard must not offer to create one over the top of a database it
        # simply cannot reach.
        log.exception("could not check for existing accounts")
        return True


class _WizardRefused(Exception):
    """A step that no longer applies, carrying where to go instead."""

    def __init__(self, response):
        self.response = response


# Which step URL the wizard is actually on. NOT `/setup`: welcome closes the
# moment an account exists, so redirecting there sends somebody to /login, which
# sends them to /, which arrives back here — a loop.
_WIZARD_URLS = {
    "welcome": "/setup", "database": "/setup/database", "account": "/setup/profile",
    "recovery": "/setup/recovery", "2fa": "/setup/2fa", "portfolio": "/setup/portfolio",
    "features": "/setup/features", "environment": "/setup/environment",
    "finish": "/setup/finish",
}


def _wizard_url() -> str:
    step = setupwizard.next_step(
        database_configured=database.source(settings).configured,
        account_exists=_accounts_exist(),
    )
    return _WIZARD_URLS.get(step, "/setup/finish")


def _wizard_step(request: Request, step: str):
    """Gate one wizard step, returning the auth context for the later ones.

    The rule is per-step rather than one blanket check, because the steps are
    open at different times and for different reasons:

    * `welcome` and `database` are open only while there is no account. They
      run without a session — there is nowhere to keep one.
    * `account` is the same, plus it needs a database to write to.
    * everything after needs a live session AND a wizard in progress. That
      second half is what closes these pages afterwards: `finish` discards the
      draft, and a restart has none, so the settings steps stop answering.
    """
    if step in ("welcome", "database", "account"):
        if _accounts_exist():
            raise _WizardRefused(_redirect("/login"))
        if step == "account" and not database.is_ready():
            raise _WizardRefused(_redirect("/setup"))
        return None

    if not setupwizard.in_progress():
        raise _WizardRefused(_redirect("/" if _accounts_exist() else "/setup"))
    db = SessionLocal()
    try:
        ctx = auth.load_auth(request, db)
    finally:
        db.close()
    if ctx is None:
        raise _WizardRefused(_redirect("/login"))
    return ctx


def _previous_step_url(rail: list[dict]) -> str | None:
    """The step before the active one, as a URL, or None at the start.

    A wizard whose only way back is the browser button is one where noticing a
    mistake on the summary means starting again. Steps the deployment skips are
    not offered: they have nothing to go back TO.
    """
    seen: list[str] = []
    for entry in rail:
        if entry.get("state") == "active":
            return _WIZARD_URLS.get(seen[-1]) if seen else None
        if entry.get("state") != "skipped":
            seen.append(entry.get("key") or entry.get("name") or "")
    return None


def _wizard_page(request: Request, step: str, extra: dict, *,
                 ctx=None, skip: set[str] | None = None,
                 rail: str | None = None) -> HTMLResponse:
    """Render a step with the rail, the CSRF token and the chrome it needs.

    `rail` is for the pages that are part of a step without being one — the
    recovery codes belong to two-factor, and giving them a rail entry of their
    own would make the flow look longer than it is.
    """
    token = ctx.session.csrf_token if ctx else auth.ensure_pre_auth_csrf(request)
    resp = templates.TemplateResponse(
        request,
        f"setup_{step}.html",
        {
            "auth": None,          # the wizard never draws the app's navigation
            "csrf": token,
            "step": step,
            "steps": setupwizard.progress(rail or step, skip=skip),
            # The previous step somebody can actually return to. Derived from
            # the rail rather than a fixed order, so a step this deployment
            # skips is skipped going backwards too.
            "wizard_back": _previous_step_url(setupwizard.progress(rail or step, skip=skip)),
            "error": None,
            **extra,
        },
    )
    if ctx is None:
        _set_pre_auth_csrf(resp, token)
    return resp


def _skipped_steps() -> set[str]:
    """Steps this deployment will not be shown, so the rail can grey them out
    rather than silently renumbering itself."""
    skip = set()
    if database.source(settings).configured:
        skip.add("database")
    if not configfile.creatable().writable:
        # Neither of these can be saved, so neither is offered — a form that
        # discards what you type is worse than one that is not there. Optional
        # features live in config.yaml exactly like the environment settings,
        # so they skip for the same reason and keep every feature on.
        skip.add("environment")
        skip.add("features")
    return skip


@app.exception_handler(_WizardRefused)
async def _wizard_refused(request: Request, exc: _WizardRefused):
    return exc.response


# --- step 1: welcome ------------------------------------------------------- #

@app.get("/setup", response_class=HTMLResponse)
def setup_welcome(request: Request):
    _wizard_step(request, "welcome")
    source = database.source(settings)
    writable = configfile.creatable()
    # A configured database is TESTED here, not merely reported as present.
    # "Configured" only says a connection string exists; the useful answer is
    # whether it works, and finding out on this page beats finding out three
    # steps later with an account half-created.
    probe = database.probe(settings.database) if source.configured else None
    return _wizard_page(request, "welcome", {
        "db_source": source,
        "db_description": database.describe(settings.database) if source.configured else None,
        "db_probe": probe,
        "config_path": str(configfile.config_path()),
        "config_writable": writable,
    }, skip=_skipped_steps())


@app.post("/setup")
async def setup_welcome_next(request: Request):
    _wizard_step(request, "welcome")
    await auth.verify_pre_auth_csrf(request)
    setupwizard.draft().completed("welcome")
    if not database.source(settings).configured:
        return _redirect("/setup/database")
    if not database.is_ready():
        # Configured but not connected: the connection failed at boot. Saying
        # so beats sending them into an account form that cannot save anything.
        return _redirect("/setup/database")
    return _redirect("/setup/profile")


# --- step 2: database ------------------------------------------------------ #

@app.get("/setup/database", response_class=HTMLResponse)
def setup_database(request: Request, tested: str = "", ok: str = ""):
    _wizard_step(request, "database")
    source = database.source(settings)
    if source.configured and database.is_ready():
        return _redirect("/setup/profile")
    return _wizard_page(request, "database", {
        "db_source": source,
        "db_description": database.describe(settings.database) if source.configured else None,
        "default_path": settings.database.path or "/data/portfolio.db",
        "tested": tested,
        "tested_ok": ok == "1",
        "form": {},
    }, skip=_skipped_steps())


def _database_from_form(form) -> DatabaseSettings:
    """Build settings from the submitted form, raising ValueError to show."""
    kind = (form.get("kind") or "sqlite").strip()
    if kind == "sqlite":
        return setupwizard.sqlite_settings(str(form.get("path") or ""))
    return setupwizard.postgres_settings(
        host=str(form.get("host") or ""),
        port=str(form.get("port") or "5432"),
        name=str(form.get("name") or ""),
        user=str(form.get("user") or ""),
        password=str(form.get("password") or ""),
    )


def _database_form_page(request: Request, form, message: str, ok: bool) -> HTMLResponse:
    """Redraw the database step keeping what was typed — losing a host and port
    someone just worked out, because the password was wrong, is its own small
    cruelty, and retyping is where the next typo comes from.

    The password is dropped here rather than carried. That is the second of two
    layers: the template's password input has no `value` either, so it cannot
    echo one even if this dict grew one. Both, because either alone is one edit
    away from putting a database password into the page source.
    """
    page = _wizard_page(request, "database", {
        "db_source": database.source(settings),
        "db_description": None,
        "default_path": settings.database.path or "/data/portfolio.db",
        "tested": message,
        "tested_ok": ok,
        "form": {k: v for k, v in form.items() if k not in ("_csrf", "password")},
    }, skip=_skipped_steps())
    return page


@app.post("/setup/database/test")
async def setup_database_test(request: Request):
    """Connect and report, writing nothing. The whole point of the button."""
    _wizard_step(request, "database")
    await auth.verify_pre_auth_csrf(request)
    form = await request.form()
    try:
        chosen = _database_from_form(form)
    except ValueError as exc:
        return _database_form_page(request, form, str(exc), False)
    result = database.probe(chosen)
    return _database_form_page(request, form, result.detail, result.ok)


@app.post("/setup/database")
async def setup_database_save(request: Request):
    """Connect for real: migrate, build the session factory, and remember the
    choice so the last step can write it to the config file.

    Probed first even though `connect()` would fail anyway — a failure here
    would surface as an Alembic traceback, and the probe's explanation is the
    one worth reading.
    """
    _wizard_step(request, "database")
    await auth.verify_pre_auth_csrf(request)
    form = await request.form()
    try:
        chosen = _database_from_form(form)
    except ValueError as exc:
        return _database_form_page(request, form, str(exc), False)
    result = database.probe(chosen)
    if not result.ok:
        return _database_form_page(request, form, result.detail, False)
    try:
        database.connect(chosen)
    except Exception as exc:
        log.exception("wizard could not connect the chosen database")
        return _database_form_page(
            request, form,
            f"Connected, but preparing the database failed: {exc}", False)
    current = setupwizard.draft()
    current.database = chosen
    current.completed("database")
    return _redirect("/setup/profile")


# --- step 3: the account --------------------------------------------------- #

@app.get("/setup/profile", response_class=HTMLResponse)
def setup_account(request: Request, error: str = ""):
    _wizard_step(request, "account")
    return _wizard_page(request, "account", {
        "db_description": database.describe(database.current() or settings.database),
        "error": error or None,
    }, skip=_skipped_steps())


@app.post("/setup/profile")
async def setup_account_create(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
):
    _wizard_step(request, "account")
    await auth.verify_pre_auth_csrf(request)
    problem = auth.password_problem(password) or auth.email_problem(email)
    if problem:
        return _wizard_page(request, "account", {
            "db_description": database.describe(database.current() or settings.database),
            "error": problem,
        }, skip=_skipped_steps())
    with anon() as db:
        if _users_exist(db):
            raise HTTPException(403, "Setup already completed")
        user = User(
            email=email.strip().lower(),
            name=name.strip() or email.strip(),
            password_hash=auth.hash_password(password),
            is_admin=True,  # the bootstrap account manages the others
        )
        db.add(user)
        db.flush()
        # Named now and renamed on the portfolio step. Created here rather than
        # there because a session needs a portfolio to be scoped to — and
        # because an abandoned wizard should still leave a working install.
        portfolio = Portfolio(name=f"{user.name}'s portfolio"[:80])
        db.add(portfolio)
        db.flush()
        db.add(PortfolioMember(portfolio_id=portfolio.id, user_id=user.id, role="owner"))
        db.flush()
        _claim_legacy_rows(db, user.id, portfolio.id)
        twofactor.ensure_recovery_codes(db, user)
        raw = auth.create_session(db, user, settings, active_portfolio_id=portfolio.id)
    current = setupwizard.draft()
    current.completed("account")
    current.portfolio_name = portfolio.name
    resp = _redirect("/setup/recovery")
    _set_session_cookie(resp, raw)
    return resp


# --- step 4: recovery codes ------------------------------------------------ #

@app.get("/setup/recovery", response_class=HTMLResponse)
def setup_recovery(request: Request):
    """The codes, shown once, before the second factor is even offered.

    This order is the point. They are what makes skipping 2FA survivable, and
    they are the whole of account recovery for an app with no reset email — so
    they are not a footnote to enrolling an authenticator, they come first.
    """
    _wizard_step(request, "recovery")
    with scoped(request) as (ctx, db):
        codes = twofactor.generate_recovery_codes(db, ctx.user)
        ctx.user.recovery_codes_seen_at = dt.datetime.now(dt.timezone.utc)
        db.flush()
    setupwizard.draft().completed("recovery")
    return _wizard_page(request, "recovery", {
        "codes": codes,
        "totp_on": False,
    }, ctx=ctx, skip=_skipped_steps())


# --- step 5: two-factor ---------------------------------------------------- #

# Two-factor is NOT a wizard step. It lives on the account page — see
# `setupwizard.STEPS` for why, and `/profile/2fa` for the routes that do it.


# --- step 5: the first portfolio ------------------------------------------- #

@app.get("/setup/portfolio", response_class=HTMLResponse)
def setup_portfolio(request: Request, error: str = ""):
    ctx = _wizard_step(request, "portfolio")
    current = setupwizard.draft()
    return _wizard_page(request, "portfolio", {
        "error": error,
        "name": current.portfolio_name or "My portfolio",
        "price_feed": current.price_feed,
        "timezone": current.timezone or settings.timezone,
        "zones": setupwizard.timezones(),
    }, ctx=ctx, skip=_skipped_steps())


@app.post("/setup/portfolio")
async def setup_portfolio_save(
    request: Request,
    name: str = Form(...),
    timezone: str = Form(...),
    price_feed: str = Form(""),
):
    _wizard_step(request, "portfolio")
    problem = setupwizard.timezone_problem(timezone)
    if not name.strip():
        problem = "Give the portfolio a name."
    if problem:
        return _redirect("/setup/portfolio?error=" + quote_plus(problem))
    with scoped(request) as (ctx, db):
        await auth.verify_csrf(request, db)
        portfolio = db.get(Portfolio, ctx.active_portfolio_id)
        portfolio.name = name.strip()[:80]
        db.flush()
    current = setupwizard.draft()
    current.portfolio_name = name.strip()[:80]
    current.timezone = timezone.strip()
    current.price_feed = price_feed.lower() in ("1", "true", "on", "yes")
    current.completed("portfolio")
    # The timezone decides what "today" means everywhere, so it applies now
    # rather than at the restart — a wizard finished on the wrong date would be
    # a strange first impression.
    settings.timezone = current.timezone
    clock.configure(current.timezone)
    if "features" in _skipped_steps():
        return _redirect("/setup/finish" if "environment" in _skipped_steps()
                         else "/setup/environment")
    return _redirect("/setup/features")


# --- step 6: optional features --------------------------------------------- #

@app.get("/setup/features", response_class=HTMLResponse)
def setup_features(request: Request):
    """Which optional parts of the app this install wants.

    Everything defaults ON, and the page says these are changeable later —
    a first-run wizard is the worst moment to ask somebody to decide what they
    will want, so the honest framing is "you can change your mind".
    """
    ctx = _wizard_step(request, "features")
    current = setupwizard.draft()
    return _wizard_page(request, "features", {
        "features": features.FEATURES,
        "chosen": current.features,
    }, ctx=ctx, skip=_skipped_steps())


@app.post("/setup/features")
async def setup_features_save(request: Request):
    """A checkbox that is off is simply absent from the submission, so the
    chosen set is built from what IS there — every declared feature not named
    was unticked."""
    _wizard_step(request, "features")
    with scoped(request) as (_, db):
        await auth.verify_csrf(request, db)
    form = await request.form()
    ticked = set(form.getlist("feature"))
    current = setupwizard.draft()
    current.features = {f.key: (f.key in ticked) for f in features.FEATURES}
    current.completed("features")
    if "environment" in _skipped_steps():
        return _redirect("/setup/finish")
    return _redirect("/setup/environment")


# --- step 7: environment --------------------------------------------------- #

@app.get("/setup/environment", response_class=HTMLResponse)
def setup_environment(request: Request, error: str = ""):
    ctx = _wizard_step(request, "environment")
    current = setupwizard.draft()
    return _wizard_page(request, "environment", {
        "error": error,
        "proxies": "\n".join(current.trusted_proxies),
        "external_url": current.external_url or "",
        "supervision": lifecycle.supervision(),
    }, ctx=ctx, skip=_skipped_steps())


@app.post("/setup/environment")
async def setup_environment_save(
    request: Request,
    trusted_proxies: str = Form(""),
    external_url: str = Form(""),
):
    _wizard_step(request, "environment")
    with scoped(request) as (_, db):
        await auth.verify_csrf(request, db)
    current = setupwizard.draft()
    try:
        current.trusted_proxies = setupwizard.parse_proxies(trusted_proxies)
        setupwizard.parse_external_url(external_url)   # validated, kept raw
    except ValueError as exc:
        return _redirect("/setup/environment?error=" + quote_plus(str(exc)))
    current.external_url = external_url.strip() or None
    current.completed("environment")
    return _redirect("/setup/finish")


# --- step 7: finish -------------------------------------------------------- #

@app.get("/setup/finish", response_class=HTMLResponse)
def setup_finish(request: Request, error: str = ""):
    ctx = _wizard_step(request, "finish")
    current = setupwizard.draft()
    values = setupwizard.config_values(current)
    writable = configfile.creatable()
    return _wizard_page(request, "finish", {
        "error": error,
        "values": values,
        "yaml": configfile.render(values),
        "config_path": str(configfile.config_path()),
        "config_writable": writable,
        "restart_for": setupwizard.needs_restart(current),
        "supervision": lifecycle.supervision(),
        "external_url": current.external_url,
    }, ctx=ctx, skip=_skipped_steps())


@app.post("/setup/finish")
async def setup_finish_save(request: Request, restart: str = Form("")):
    """Write the config, then either land on the dashboard or on the page that
    carries you across a restart."""
    _wizard_step(request, "finish")
    with scoped(request) as (_, db):
        await auth.verify_csrf(request, db)
    current = setupwizard.draft()
    values = setupwizard.config_values(current)
    if configfile.creatable().writable:
        try:
            configfile.write_tree(values)
        except OSError as exc:
            log.exception("wizard could not write the config file")
            return _redirect("/setup/finish?error=" + quote_plus(
                f"Could not write {configfile.config_path()}: {exc}"))

    wants_restart = restart.lower() in ("1", "true", "on", "yes")
    target = _post_setup_url(current)
    setupwizard.discard()
    if wants_restart and lifecycle.supervision().restarts:
        resp = _redirect("/setup/restarting?to=" + quote_plus(target))
        return resp
    return _redirect(target)


def _post_setup_url(current) -> str:
    """Where to send someone once setup is done.

    Their own external URL if they gave one — the app may well be about to
    become unreachable at the address they are currently using, which is
    exactly the case the waiting page exists for.
    """
    url = setupwizard.parse_external_url(current.external_url or "")
    return url.url + "/" if url is not None else "/"


@app.get("/setup/restarting", response_class=HTMLResponse)
def setup_restarting(request: Request, to: str = "/"):
    """The page that survives the restart.

    Rendered, THEN the stop is requested — the other order sends a SIGTERM
    before the browser has the page telling it what is happening, and the
    person is left looking at a connection error with no idea whether the
    wizard worked.

    Deliberately not gated on the wizard being in progress: the draft has
    already been discarded by the time this loads, and this page has no power
    of its own — it polls a URL and redirects.
    """
    resp = templates.TemplateResponse(
        request,
        "setup_restarting.html",
        {"auth": None, "target": to, "supervision": lifecycle.supervision()},
    )
    lifecycle.request_stop("setup wizard finished")
    return resp


def _claim_legacy_rows(db: DbSession, user_id: int, portfolio_id: int) -> None:
    """Hand every pre-auth row to the first account.

    Rows created before accounts existed have no owner. They join the first portfolio. Instrument notes were
    personal too (sale prices, strategy) — they move into that portfolio's
    holding_pref and are cleared from the shared catalogue in the same
    transaction.
    """
    # These rows have no owner yet, so a user filter could never find them.
    tenancy.allow_unscoped(db)
    claimed = 0
    for model in (Trade, Dividend, PlannedPurchase, InvestmentPlan):
        rows = db.scalars(select(model).where(model.portfolio_id.is_(None))).all()
        for row in rows:
            row.portfolio_id = portfolio_id
            if getattr(row, "user_id", "absent") is None:
                row.user_id = user_id
        claimed += len(rows)

    for inst in db.scalars(select(Instrument)).all():
        if inst.note or inst.drp:
            db.add(
                HoldingPref(
                    portfolio_id=portfolio_id,
                    instrument_id=inst.id,
                    drp=inst.drp,
                    note=inst.note,
                )
            )
            inst.note = None
    db.flush()
    log.info("claimed %d pre-auth rows into portfolio %d", claimed, portfolio_id)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, timeout: str = ""):
    with anon() as db:
        if auth.load_auth(request, db) is not None:
            return _redirect("/")
        if not _users_exist(db):
            return _redirect("/setup")
        token = auth.ensure_pre_auth_csrf(request)
        resp = templates.TemplateResponse(
            request, "login.html",
            {"auth": None, "csrf": token, "error": None,
             # Said BEFORE they try, because the failure it describes looks
             # like a wrong password and would otherwise be retried forever.
             "insecure": auth.insecure_login_problem(request, settings),
             # Sent here by the idle overlay. Saying why beats a login page
             # that appears for no reason someone can see.
             "timed_out": timeout == "1"},
        )
        _set_pre_auth_csrf(resp, token)
        return resp


@app.post("/login")
async def login_submit(
    request: Request, email: str = Form(...), password: str = Form(...)
):
    ip = auth.client_ip(request)
    email_l = email.strip().lower()
    with anon() as db:

        def _fail(message: str):
            token = auth.ensure_pre_auth_csrf(request)
            resp = templates.TemplateResponse(
                request, "login.html", {"auth": None, "csrf": token, "error": message}
            )
            _set_pre_auth_csrf(resp, token)
            return resp

        # A stale token hands back a fresh login form, not a 403: presentation
        # only, not a weaker check — decisions.md #43.
        try:
            await auth.verify_csrf(request, db)
        except HTTPException as exc:
            if exc.status_code != 403:
                raise
            log.info("stale login CSRF from %s — re-issuing the form", ip)
            return _fail("This page had been open a while. Try again.")

        if auth.is_locked(db, email_l, ip, settings):
            return _fail("Too many attempts. Try again shortly.")
        user = db.scalar(select(User).where(User.email == email_l))
        stored = user.password_hash if user else None
        # Same message either way — no account enumeration.
        ok = auth.verify_password(stored, password) and user is not None and user.is_active
        auth.record_attempt(db, email_l, ip, success=ok)
        if not ok:
            return _fail("Invalid email or password.")
        if auth.needs_rehash(user.password_hash):
            user.password_hash = auth.hash_password(password)
        # Land in a portfolio they own before one merely shared with them —
        # otherwise the lowest id wins and someone invited into an older
        # portfolio opens somebody else's holdings instead of their own.
        landing = db.scalar(
            select(PortfolioMember.portfolio_id)
            .where(PortfolioMember.user_id == user.id)
            .order_by(
                (PortfolioMember.role != "owner"),  # owners first
                PortfolioMember.portfolio_id,
            )
        )
        # With a second factor on, the password only buys a half-authenticated
        # session: `auth.load_session` refuses it everywhere, so the cookie is
        # useless until the code step flips the flag.
        pending = twofactor.is_enabled(user)
        raw = auth.create_session(
            db, user, settings, active_portfolio_id=landing, awaiting_totp=pending,
            ip=ip, user_agent=request.headers.get("user-agent"),
        )
        if pending:
            resp = _redirect("/login/code")
            _set_session_cookie(resp, raw)
            return resp
        resp = _redirect("/profile" if user.must_change_password else "/")
        _set_session_cookie(resp, raw)
        return resp


def _code_form(request: Request, error: str | None = None, used_recovery: bool = False):
    token = auth.ensure_pre_auth_csrf(request)
    resp = templates.TemplateResponse(
        request,
        "login_code.html",
        {"auth": None, "csrf": token, "error": error, "used_recovery": used_recovery},
    )
    _set_pre_auth_csrf(resp, token)
    return resp


# --------------------------------------------------------------------------- #
# Forgotten password: a recovery code stands in for it — but never for the
# second factor, which is what stops a stolen code list being the whole
# account. decisions.md #36.
# --------------------------------------------------------------------------- #

def _recover_form(request: Request, error: str | None = None) -> HTMLResponse:
    token = auth.ensure_pre_auth_csrf(request)
    resp = templates.TemplateResponse(
        request, "recover.html", {"auth": None, "csrf": token, "error": error},
    )
    _set_pre_auth_csrf(resp, token)
    return resp


@app.get("/login/recover", response_class=HTMLResponse)
def recover_form(request: Request):
    return _recover_form(request)


@app.post("/login/recover")
async def recover_submit(request: Request, email: str = Form(""), code: str = Form("")):
    """Spend a recovery code to get past a forgotten password.

    Every failure returns the SAME page with the SAME message. An
    unauthenticated endpoint that takes an email address is an enumeration
    oracle the moment "no such account" and "wrong code" look different.
    """
    ip = auth.client_ip(request)
    email_l = email.strip().lower()
    with anon() as db:
        await auth.verify_pre_auth_csrf(request)

        # Throttled. NIST stops requiring this once a look-up secret clears 64
        # bits — ours do — but it is the difference between one wrong guess and
        # an unbounded stream of them, and it already exists.
        if auth.is_locked(db, email_l, ip, settings):
            return _recover_form(request, "Too many attempts. Try again shortly.")

        user = db.scalar(select(User).where(User.email == email_l))
        ok = False
        if user is not None and user.is_active:
            ok = twofactor.consume_recovery_code(db, user, code)
        auth.record_attempt(db, email_l, ip, success=ok)
        if not ok:
            return _recover_form(
                request, "That email and recovery code do not match.")

        # The code proved possession of the list; it did NOT prove the second
        # factor. If one is enrolled, this session stays half-authenticated
        # exactly as a password login would.
        landing = db.scalar(
            select(PortfolioMember.portfolio_id)
            .where(PortfolioMember.user_id == user.id)
            .order_by((PortfolioMember.role != "owner"), PortfolioMember.portfolio_id)
        )
        pending = twofactor.is_enabled(user)
        # Whatever password they could not remember is now unusable to anyone
        # who might have learned it, and they are made to choose a new one
        # before the app is reachable.
        user.must_change_password = True
        raw = auth.create_session(
            db, user, settings, active_portfolio_id=landing, awaiting_totp=pending,
            ip=ip, user_agent=request.headers.get("user-agent"),
            recovery_spent=True,
        )
        left = twofactor.remaining_recovery_codes(db, user)
        # The code itself is never logged — it would be a live credential in a
        # log file until it was spent.
        log.warning("user %d signed in with a recovery code (%d left)", user.id, left)
        resp = _redirect("/login/code" if pending else "/profile")
        _set_session_cookie(resp, raw)
        return resp


@app.get("/login/code", response_class=HTMLResponse)
def login_code_form(request: Request):
    with anon() as db:
        pending = auth.load_pending_session(
            db, request.cookies.get(settings.auth.cookie_name), settings)
        if pending is None:
            # No half-authenticated session: either they never gave a password,
            # or they already finished. Either way this page is not for them.
            return _redirect("/login")
        return _code_form(request)


@app.post("/login/code")
async def login_code_submit(request: Request, code: str = Form(...)):
    ip = auth.client_ip(request)
    with anon() as db:
        await auth.verify_csrf(request, db)
        pending = auth.load_pending_session(
            db, request.cookies.get(settings.auth.cookie_name), settings)
        if pending is None:
            return _redirect("/login")
        user = db.get(User, pending.user_id)
        if user is None:  # account deleted between the two steps
            auth.destroy_session(db, request.cookies.get(settings.auth.cookie_name))
            return _redirect("/login")

        # Lockout covers this stage too. Without it the code is a six-digit
        # secret that can be brute-forced at will by anyone holding a password
        # — which is precisely the situation the second factor exists for.
        if auth.is_locked(db, user.email, ip, settings):
            return _code_form(request, "Too many attempts. Try again shortly.")

        used_recovery = False
        if twofactor.verify_code(user.totp_secret, code):
            ok = True
        elif pending.recovery_spent:
            # ONE recovery code per sign-in (decisions.md #45), refused
            # indistinguishably from a wrong code (#51).
            ok = False
        else:
            ok = used_recovery = twofactor.consume_recovery_code(db, user, code)
        auth.record_attempt(db, user.email, ip, success=ok)
        if not ok:
            return _code_form(request, "That code isn't right.")

        pending.awaiting_totp = False
        db.flush()
        if used_recovery:
            left = twofactor.remaining_recovery_codes(db, user)
            log.info("user %s signed in with a recovery code (%d left)", user.id, left)
        return _redirect("/profile" if user.must_change_password else "/")


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #

@app.get("/metrics", include_in_schema=False)
def prometheus_metrics():
    """Health for a scraper. See `app/metrics.py` for what may go in here.

    **404 when disabled, not 403.** A 403 would confirm the endpoint exists and
    is merely shut, which is a small piece of free reconnaissance for no
    benefit — a caller who is allowed to know already reads the config.

    This is in `PUBLIC_PATHS`, so the check below is the *only* thing standing
    between an unconfigured install and an anonymous database read. It stays
    the first statement in the function.
    """
    if not settings.metrics.enabled:
        raise HTTPException(status_code=404)
    if not database.is_ready():
        # No database yet (the wizard has not been finished). Still a valid
        # scrape — `stocktake_database_ready 0` is the useful answer, and a
        # 500 here would show up as a scrape failure rather than as the state
        # it actually is.
        return PlainTextResponse(metrics.render(None), media_type=metrics.CONTENT_TYPE)
    with anon() as db:
        return PlainTextResponse(metrics.render(db), media_type=metrics.CONTENT_TYPE)


# --------------------------------------------------------------------------- #
# Session lifetime: the countdown, and the one endpoint that resets it
# --------------------------------------------------------------------------- #

@app.get("/session/status")
def session_status(request: Request):
    """Seconds left before this session dies, for the idle overlay.

    In `auth.SLIDING_EXEMPT`, so asking must NOT extend the thing being asked
    about — otherwise the overlay's own polling would keep the session alive
    and the timeout would never fire.

    The server is the authority here. The browser runs its own timer for a
    smooth countdown, but re-syncs from this so a clock skew, a sleeping laptop
    or a second tab can't make it disagree with reality.
    """
    with anon() as db:
        row = auth.load_session(db, request.cookies.get(settings.auth.cookie_name),
                                settings)
        if row is None:
            return {"authenticated": False, "seconds_left": 0}
        idle_deadline, hard_deadline = auth.session_deadlines(row, settings)
        deadline = min(idle_deadline, hard_deadline, ensure_utc(row.expires_at))
        left = (deadline - dt.datetime.now(dt.timezone.utc)).total_seconds()
        return {
            "authenticated": True,
            "seconds_left": max(0, int(left)),
            "warn_at": settings.auth.idle_warning_seconds,
        }


@app.post("/session/keepalive")
async def session_keepalive(request: Request):
    """"Stay signed in" — the ONE endpoint that deliberately slides the window.

    A button press is a person, which is exactly what the status poll is not.
    It still can't push past the absolute cap; `load_auth` clamps to it.
    """
    with anon() as db:
        await auth.verify_csrf(request, db)
        ctx = auth.load_auth(request, db)  # this path slides, by design
        if ctx is None:
            raise HTTPException(401, "session expired")
        idle_deadline, hard_deadline = auth.session_deadlines(ctx.session, settings)
        left = (min(idle_deadline, hard_deadline)
                - dt.datetime.now(dt.timezone.utc)).total_seconds()
        return {"authenticated": True, "seconds_left": max(0, int(left))}


@app.post("/logout")
async def logout(request: Request, next: str = ""):
    with anon() as db:
        await auth.verify_csrf(request, db)
        auth.destroy_session(db, request.cookies.get(settings.auth.cookie_name))
    # `next` exists for one caller: the idle overlay, which sends people to
    # /login?timeout=1 so the page explains itself. Only same-site paths are
    # honoured — see `_safe_path`, which is also what the trade editor's
    # `?return=` uses.
    target = _safe_path(next, "/login")
    resp = _redirect(target)
    resp.delete_cookie(settings.auth.cookie_name, path="/")
    return resp


@app.get("/profile", response_class=HTMLResponse)
def account_page(request: Request, saved: str = "", error: str = ""):
    """`saved` names WHAT was saved: the page has two forms and one message
    for both told you your password had changed when you picked a colour."""
    with scoped(request) as (ctx, db):
        return _render(
            request,
            ctx,
            "account.html",
            {"saved": saved if saved in ("password", "appearance", "2fa-off",
                                         "session-revoked", "sessions-revoked") else "",
             # Names the profile control as the active one, the same way a page
             # names its nav entry.
             "error": error, "active_nav": "account",
             "hideable_nav": navigation.hideable_for(ctx, settings),
             # Passkeys need a secure context, so the page says whether this
             # connection is one rather than claiming they are unavailable.
             "secure": auth.is_secure_request(request, settings),
             # The theme designer: what may be set, what this person has set,
             # what the built-in value is (shown as the placeholder, so an
             # empty box still tells you what you would get), and any choice
             # that will be hard to see.
             "swatches": theming.SWATCHES,
             "colors": ctx.user.theme_colors or {},
             "defaults": theming.DEFAULTS,
             "color_warnings": theming.warnings(ctx.user.theme_colors),
             **_twofactor_context(db, ctx.user),
             **_sessions_context(db, ctx)},
        )


def _describe_agent(ua: str | None) -> str:
    """A user agent as something a person recognises.

    Deliberately crude. The goal is "is this the laptop or the phone?", and a
    real UA-parsing library would be a dependency, a data file to keep current,
    and a much longer answer than the question deserves.
    """
    if not ua:
        return "Unknown device"
    browser = next(
        (name for token, name in (
            ("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"),
            ("Chrome/", "Chrome"), ("Safari/", "Safari"),
        ) if token in ua),
        "Browser",
    )
    platform = next(
        (name for token, name in (
            ("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
            ("Macintosh", "macOS"), ("Windows", "Windows"), ("Linux", "Linux"),
        ) if token in ua),
        None,
    )
    return f"{browser} on {platform}" if platform else browser


def _sessions_context(db: DbSession, ctx) -> dict:
    rows = db.scalars(
        select(UserSession)
        .where(UserSession.user_id == ctx.user.id)
        .order_by(UserSession.last_seen_at.desc())
    ).all()
    return {
        "sessions": [
            {
                "id": row.id,
                "device": _describe_agent(row.user_agent),
                "ip": row.ip or "—",
                "last_seen": ensure_utc(row.last_seen_at),
                "started": ensure_utc(row.created_at),
                # Compared on the hash, never on anything from the request —
                # the cookie itself never needs to be looked at here.
                "current": row.token_hash == ctx.session.token_hash,
            }
            for row in rows
        ]
    }


@app.post("/profile/sessions/{session_id}/revoke")
async def revoke_session(request: Request, session_id: int):
    with scoped(request) as (ctx, db):
        await auth.verify_csrf(request, db)
        row = db.get(UserSession, session_id)
        # Own sessions only. Without the ownership check this would be a way to
        # sign anybody out by guessing a small integer.
        if row is None or row.user_id != ctx.user.id:
            raise HTTPException(404, "no such session")
        if row.token_hash == ctx.session.token_hash:
            return _redirect("/profile?error=current-session")
        db.delete(row)
        db.flush()
        return _redirect("/profile?saved=session-revoked")


@app.post("/profile/sessions/revoke-others")
async def revoke_other_sessions(request: Request):
    """Sign out everywhere else — the thing you want after losing a laptop."""
    with scoped(request) as (ctx, db):
        await auth.verify_csrf(request, db)
        removed = 0
        for row in db.scalars(
            select(UserSession).where(UserSession.user_id == ctx.user.id)
        ).all():
            if row.token_hash != ctx.session.token_hash:
                db.delete(row)
                removed += 1
        db.flush()
        log.info("user %s revoked %d other sessions", ctx.user.id, removed)
        return _redirect("/profile?saved=sessions-revoked")


def _twofactor_context(db: DbSession, user: User) -> dict:
    return {
        "totp_enabled": twofactor.is_enabled(user),
        "recovery_left": (
            twofactor.remaining_recovery_codes(db, user)
            if twofactor.is_enabled(user) else 0
        ),
        "enrol": None,      # set by the enrolment page
        "new_codes": None,  # shown exactly once, right after enabling
    }


# --------------------------------------------------------------------------- #
# Two-factor authentication. Enrolment is two requests and the unconfirmed
# secret never reaches the user row — decisions.md #39.
# --------------------------------------------------------------------------- #

@app.get("/profile/2fa", response_class=HTMLResponse)
def twofactor_setup(request: Request, error: str = ""):
    with scoped(request) as (ctx, db):
        if twofactor.is_enabled(ctx.user):
            return _redirect("/profile")
        secret = twofactor.new_secret()
        uri = twofactor.provisioning_uri(secret, ctx.user.email, branding.NAME)
        return _render(
            request,
            ctx,
            "account.html",
            {"saved": "", "error": error, "active_nav": "",
             **_twofactor_context(db, ctx.user),
             "enrol": {"secret": secret, "qr": twofactor.qr_svg(uri)}},
        )


@app.post("/profile/2fa/enable")
async def twofactor_enable(
    request: Request, secret: str = Form(...), code: str = Form(...)
):
    with scoped(request) as (ctx, db):
        await auth.verify_csrf(request, db)
        if twofactor.is_enabled(ctx.user):
            return _redirect("/profile")
        # The code proves the authenticator really holds this secret. Skipping
        # it is how people lock themselves out.
        if not twofactor.verify_code(secret, code):
            return _redirect("/profile/2fa?error=" + quote_plus(
                "That code isn't right — check the app and try again."))
        codes = twofactor.enable(db, ctx.user, secret)
        log.info("2FA enabled for user %s", ctx.user.id)
        return _render(
            request,
            ctx,
            "account.html",
            {"saved": "", "error": None, "active_nav": "",
             **_twofactor_context(db, ctx.user),
             "new_codes": codes},
        )


@app.post("/profile/recovery")
async def account_recovery_codes(request: Request):
    """Issue a fresh set of recovery codes and show them once.

    Always a REGENERATION, never a re-display. They are stored hashed, so the
    originals genuinely cannot be shown again — and a page that offered to
    "view" them would be promising something it cannot do. The consequence is
    real and the page says it: any list already written down stops working.

    A POST rather than a GET because it changes state, and because a link that
    silently invalidated somebody's printed codes on hover-preview would be a
    poor thing to have built.
    """
    with scoped(request) as (ctx, db):
        await auth.verify_csrf(request, db)
        codes = twofactor.generate_recovery_codes(db, ctx.user)
        ctx.user.recovery_codes_seen_at = dt.datetime.now(dt.timezone.utc)
        db.flush()
        log.info("recovery codes reissued for user %d", ctx.user.id)
        return _render(
            request, ctx, "recovery_codes.html",
            {"active_nav": "", "codes": codes,
             "totp_on": twofactor.is_enabled(ctx.user)},
        )


@app.post("/profile/2fa/disable")
async def twofactor_disable(
    request: Request, password: str = Form(...), code: str = Form("")
):
    """Turning it off needs the password AND a current code.

    Both, deliberately: the password alone would let someone who walked up to
    an unlocked screen remove the factor, and a code alone would let anyone
    holding the phone do it.
    """
    with scoped(request) as (ctx, db):
        await auth.verify_csrf(request, db)
        if not twofactor.is_enabled(ctx.user):
            return _redirect("/profile")
        if not auth.verify_password(ctx.user.password_hash, password):
            return _redirect("/profile?error=password")
        if not (twofactor.verify_code(ctx.user.totp_secret, code)
                or twofactor.consume_recovery_code(db, ctx.user, code)):
            return _redirect("/profile?error=code")
        twofactor.disable(db, ctx.user)
        log.info("2FA disabled for user %s", ctx.user.id)
        return _redirect("/profile?saved=2fa-off")


@app.post("/users/{user_id}/2fa/clear")
async def users_clear_2fa(request: Request, user_id: int):
    """Admin escape hatch for someone who lost their phone AND their codes.

    Sits beside the existing password reset because it is the same kind of act:
    an administrator vouching for someone out of band. Logged loudly — this
    removes a security control from another person's account.
    """
    with scoped(request) as (ctx, db):
        await auth.verify_csrf(request, db)
        _require_admin(ctx)
        user = db.get(User, user_id)
        if user is None:
            raise HTTPException(404, "no such user")
        twofactor.disable(db, user)
        # Any half-authenticated sessions become unusable; full ones would
        # survive, and shouldn't — the account's security just changed.
        auth.destroy_sessions_for(db, user.id)
        log.warning("admin %s cleared 2FA for user %s", ctx.user.id, user.id)
        return _redirect("/users?reset=2fa")


@app.post("/profile/password")
async def account_password(
    request: Request,
    current_password: str = Form(...),
    password: str = Form(...),
    confirm: str = Form(...),
):
    with scoped(request) as (ctx, db):
        await auth.verify_csrf(request, db)
        error = None
        if not auth.verify_password(ctx.user.password_hash, current_password):
            error = "Current password is incorrect."
        elif password != confirm:
            error = "The new passwords don't match."
        else:
            error = auth.password_problem(password)
        if error:
            return _render(
                request, ctx, "account.html",
                {"saved": "", "error": error, "active_nav": ""},
            )
        ctx.user.password_hash = auth.hash_password(password)
        ctx.user.must_change_password = False
        db.flush()
        # Every other session for this account is invalidated; keep this one.
        this_token = ctx.session.token_hash
        auth.destroy_sessions_for(db, ctx.user.id)
        raw = auth.create_session(
            db, ctx.user, settings, active_portfolio_id=ctx.active_portfolio_id,
            ip=auth.client_ip(request), user_agent=request.headers.get("user-agent"),
        )
        log.info("password changed for user %d (was token %s…)", ctx.user.id, this_token[:8])
        resp = _redirect("/profile?saved=password")
        _set_session_cookie(resp, raw)
        return resp


# --------------------------------------------------------------------------- #
# User administration (admin only)
# --------------------------------------------------------------------------- #

def _kick_feed() -> None:
    """Nudge the price feed after a new instrument appears, so its history
    shows up without waiting for the daily run."""
    if not feed_status["running"]:
        try:
            asyncio.get_running_loop().run_in_executor(None, _run_feed)
        except RuntimeError:  # no loop (CLI/tests) — the schedule will catch it
            pass


# Jurisdictions worth offering. Deliberately short: the financial-year and
# capital-gains logic in `fyreport` is Australia's, so a 250-entry country list
# would imply support that does not exist. Adding a code here is the cheap half
# of supporting a jurisdiction — see T28b for the other half.
# How many lines the console shows on load. Enough to cover the last restart
# and a failed import; small enough that the panel is a window, not the page.
CONSOLE_LINES = 100

COUNTRIES = [
    ("AU", "Australia"),
    ("NZ", "New Zealand"),
    ("GB", "United Kingdom"),
    ("US", "United States"),
    ("CA", "Canada"),
    ("IE", "Ireland"),
]


def _require_admin(ctx):
    """App-wide account administration."""
    if ctx is None or not ctx.is_admin:
        raise HTTPException(403, "Admins only")


def _require_write(ctx):
    """Anything that changes this portfolio. Viewers are read-only."""
    if ctx is None or not ctx.can_write:
        raise HTTPException(403, "You have read-only access to this portfolio")


def _require_owner(ctx):
    """Members, keys and portfolio settings."""
    if ctx is None or not ctx.is_owner:
        raise HTTPException(403, "Only the portfolio owner can do that")


def _users_context(db, *, created: str = "", reset: str = "",
                   temp_password: str = "", error: str | None = None) -> dict:
    return {
        "active_nav": "admin",
        "users": db.scalars(select(User).order_by(User.id)).all(),
        "created": created,
        "reset": reset,
        "temp_password": temp_password,
        "error": error,
    }


@app.get("/admin/accounts", response_class=HTMLResponse)
@app.get("/users", response_class=HTMLResponse)
def users_page(request: Request):
    with scoped(request) as (ctx, db):
        _require_admin(ctx)
        return _render(request, ctx, "users.html", _users_context(db))


@app.post("/users/add")
async def users_add(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    is_admin: str = Form(""),
):
    with scoped(request) as (ctx, db):
        _require_admin(ctx)
        await auth.verify_csrf(request, db)
        email_l = email.strip().lower()
        error = auth.email_problem(email_l) or auth.password_problem(password)
        if not error and db.scalar(select(User).where(User.email == email_l)):
            error = "That email already has an account."
        if error:
            return _render(request, ctx, "users.html", _users_context(db, error=error))
        new_user = User(
            email=email_l,
            name=name.strip() or email_l,
            password_hash=auth.hash_password(password),
            is_admin=bool(is_admin),
            # They set their own on first login; this one is only a handover.
            must_change_password=True,
        )
        db.add(new_user)
        db.flush()
        # Everyone starts with a portfolio of their own; sharing is opt-in from
        # the Members page of whichever portfolio wants them.
        own = Portfolio(name=f"{new_user.name}'s portfolio"[:80])
        db.add(own)
        db.flush()
        db.add(PortfolioMember(portfolio_id=own.id, user_id=new_user.id, role="owner"))
        # Issued now, but deliberately NOT shown: these are the new user's
        # recovery codes, and an administrator is not who they belong to.
        # `recovery_codes_seen_at` stays NULL, so the banner asks them to
        # generate a set of their own the first time they sign in.
        twofactor.ensure_recovery_codes(db, new_user)
        db.flush()
        # Rendered, not redirected: a temporary password in a query string
        # would be written to the access log, the browser history and every
        # proxy on the way. Refreshing re-submits and fails harmlessly with
        # "that email already has an account".
        return _render(request, ctx, "users.html", _users_context(
            db, created=email_l, temp_password=password))


@app.post("/users/{user_id}/active")
async def users_toggle_active(request: Request, user_id: int):
    with scoped(request) as (ctx, db):
        _require_admin(ctx)
        await auth.verify_csrf(request, db)
        user = db.get(User, user_id)
        if user is None:
            raise HTTPException(404, "no such user")
        if user.id == ctx.user.id:
            raise HTTPException(400, "you can't deactivate yourself")
        user.is_active = not user.is_active
        if not user.is_active:
            auth.destroy_sessions_for(db, user.id)
        return _redirect("/users")


@app.post("/users/{user_id}/reset")
async def users_reset(request: Request, user_id: int, password: str = Form(...)):
    with scoped(request) as (ctx, db):
        _require_admin(ctx)
        await auth.verify_csrf(request, db)
        error = auth.password_problem(password)
        if error:
            raise HTTPException(400, error)
        user = db.get(User, user_id)
        if user is None:
            raise HTTPException(404, "no such user")
        user.password_hash = auth.hash_password(password)
        user.must_change_password = True
        auth.destroy_sessions_for(db, user.id)
        db.flush()
        return _render(request, ctx, "users.html", _users_context(
            db, reset=user.email, temp_password=password))




# --------------------------------------------------------------------------- #
# Application settings (admin only)
# --------------------------------------------------------------------------- #

def _settings_context(saved: str = "", error: str = "", pending: list[str] | None = None) -> dict:
    """Everything the settings page renders from.

    Values come from the LIVE settings object rather than the file, so the page
    shows what the application is actually using — an environment variable
    beats the file, and a page showing the file would quietly disagree with
    reality.
    """
    # The console's opening window. Rendered server-side so it is there with
    # scripting off; `console.js` tails from the last seq after that.
    values = {}
    for option in configfile.OPTIONS:
        value = configfile.effective(settings, option)
        values[option.name] = value.isoformat() if isinstance(value, dt.date) else value
    db = settings.database
    return {
        "active_nav": "admin",
        "options": configfile.OPTIONS,
        "values": values,
        "writable": configfile.writability(),
        "config_path": str(configfile.config_path()),
        "from_environment": {
            o.name: f"APP_{'__'.join(p.upper() for p in o.path)}"
            for o in configfile.OPTIONS
            if configfile.overridden_by_environment(o)
        },
        "fixed": {
            "app_name": settings.app_name,
            "database": (f"sqlite at {db.path}" if db.type == "sqlite"
                         else f"postgres {db.name} on {db.host}"),
            "feed_timezone": settings.price_feed.timezone,
            "brokers": ", ".join(sorted(settings.imports.brokers_available())),
        },
        "saved": saved,
        "pending": pending or [],
        "error": error,
        "supervision": lifecycle.supervision(),
        # The console's opening window. Rendered server-side so the panel is
        # useful with scripting off; console.js tails from the last seq.
        "log_lines": logbuffer.recent(CONSOLE_LINES),
    }


@app.get("/admin/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: str = ""):
    with scoped(request) as (ctx, db):
        _require_admin(ctx)
        return _render(request, ctx, "settings.html", _settings_context(saved=saved))


@app.post("/admin/settings")
async def settings_save(request: Request):
    """Write the submitted values back to config.yaml.

    Refused outright when the file is read-only: the page hides the controls in
    that case, so reaching here means the request was made some other way.
    """
    with scoped(request) as (ctx, db):
        _require_admin(ctx)
        await auth.verify_csrf(request, db)
        state = configfile.writability()
        if not state.writable:
            raise HTTPException(409, state.reason)

        form = await request.form()
        values, problems = {}, []
        for option in configfile.OPTIONS:
            raw = form.get(option.name)
            # An unchecked checkbox submits nothing at all; every other field
            # that is genuinely absent is left alone rather than blanked.
            if raw is None and option.kind != "bool":
                continue
            try:
                values[option.name] = configfile.coerce(option, raw if isinstance(raw, str) else "")
            except ValueError as exc:
                problems.append(str(exc))
        if problems:
            return _render(request, ctx, "settings.html",
                           _settings_context(error=" ".join(problems)))

        configfile.save(values)
        pending = configfile.apply_live(settings, values)
        log.info("settings updated by user %d: %s", ctx.user.id, ", ".join(sorted(values)))
        if form.get("restart"):
            lifecycle.request_stop(f"settings saved by user {ctx.user.id}")
            return _render(request, ctx, "restarting.html",
                           {"active_nav": "admin", "saved": True,
                            "supervision": lifecycle.supervision()})
        return _render(request, ctx, "settings.html",
                       _settings_context(saved="1", pending=pending))


@app.post("/admin/settings/restart")
async def settings_restart(request: Request):
    """Stop the application, and let whatever started it start it again.

    Offered whether or not the settings are writable — a restart is useful on
    its own (to pick up a ConfigMap changed from outside, or to clear a wedged
    price feed), and a read-only config file is exactly the deployment where
    changes arrive from elsewhere and need one.
    """
    with scoped(request) as (ctx, db):
        _require_admin(ctx)
        await auth.verify_csrf(request, db)
        lifecycle.request_stop(f"requested by user {ctx.user.id}")
        return _render(request, ctx, "restarting.html",
                       {"active_nav": "admin", "saved": False,
                        "supervision": lifecycle.supervision()})


# --------------------------------------------------------------------------- #
# Portfolios: switching, members, API keys
# --------------------------------------------------------------------------- #

@app.post("/portfolio/switch")
async def portfolio_switch(request: Request, portfolio_id: int = Form(...)):
    """Change which portfolio this session acts in.

    The id is validated against the user's own memberships — a forged form
    value can't move the session into someone else's portfolio.
    """
    with scoped(request) as (ctx, db):
        await auth.verify_csrf(request, db)
        if portfolio_id not in {m.portfolio_id for m in ctx.memberships}:
            raise HTTPException(403, "you're not a member of that portfolio")
        ctx.session.active_portfolio_id = portfolio_id
        db.flush()
        return _redirect(request.headers.get("referer") or "/")


@app.get("/admin/logs.json")
def admin_logs(request: Request, after: int = 0):
    """Log lines newer than `after`, for the Settings page console to tail.

    `after=0` means "the initial window"; the page then sends the highest seq
    it has and gets only what arrived since. That is what lets it keep the
    lines it was first shown instead of replacing them on every poll.
    """
    with scoped(request) as (ctx, _db):
        _require_admin(ctx)
        lines = logbuffer.since(after) if after else logbuffer.recent(CONSOLE_LINES)
        return JSONResponse({"lines": [
            {"seq": line.seq, "when": line.when, "level": line.level,
             "logger": line.logger, "message": line.message}
            for line in lines
        ]})


@app.post("/portfolio/settings")
async def portfolio_settings(
    request: Request,
    name: str = Form(...),
    reporting_currency: str = Form("AUD"),
    jurisdiction: str = Form("AU"),
):
    """Rename this portfolio, and set the currency and jurisdiction it reports in.

    **Both are stored and only partly wired.** Totals are still computed in
    `money.REPORTING` and the financial-year logic is still AU's — T28b is the
    task that unpicks the ~110 places that assume it. The page says so rather
    than implying a change here re-denominates anything, because a setting that
    silently does nothing is worse than no setting.

    The field is `jurisdiction` in the code and **"Country" on the page**. That
    is deliberate: what the column decides is which tax rules and which words
    apply, which follows tax residency — but "country" is the word a person
    recognises for it. See `Portfolio.jurisdiction`.
    """
    with scoped(request) as (ctx, db):
        await auth.verify_csrf(request, db)
        _require_admin(ctx)
        cleaned = name.strip()
        if not cleaned:
            raise HTTPException(400, "a portfolio needs a name")
        currency = reporting_currency.strip().upper()
        if currency not in money.SYMBOLS:
            raise HTTPException(400, "unknown currency")
        place = jurisdiction.strip().upper()
        if len(place) != 2 or not place.isalpha():
            raise HTTPException(400, "country must be a two-letter code")
        portfolio = db.get(Portfolio, ctx.active_portfolio_id)
        portfolio.name = cleaned[:80]
        portfolio.reporting_currency = currency
        portfolio.jurisdiction = place
        db.commit()
        return _redirect("/members")


@app.post("/portfolio/new")
async def portfolio_new(request: Request, name: str = Form(...)):
    with scoped(request) as (ctx, db):
        await auth.verify_csrf(request, db)
        label = name.strip()[:80]
        if not label:
            raise HTTPException(400, "name is required")
        portfolio = Portfolio(name=label)
        db.add(portfolio)
        db.flush()
        db.add(
            PortfolioMember(
                portfolio_id=portfolio.id, user_id=ctx.user.id, role="owner"
            )
        )
        ctx.session.active_portfolio_id = portfolio.id
        db.flush()
        return _redirect("/members")


@app.get("/admin", response_class=HTMLResponse)
@app.get("/members", response_class=HTMLResponse)
def members_page(request: Request, error: str = ""):
    with scoped(request) as (ctx, db):
        rows = db.execute(
            select(PortfolioMember, User)
            .join(User, User.id == PortfolioMember.user_id)
            .where(PortfolioMember.portfolio_id == ctx.active_portfolio_id)
            .order_by(PortfolioMember.role, User.name)
        ).all()
        # Only people with an account can be added; accounts are created by an
        # app admin on /users.
        member_ids = {u.id for (_m, u) in rows}
        candidates = [
            u
            for u in db.scalars(select(User).where(User.is_active.is_(True)).order_by(User.name))
            if u.id not in member_ids
        ]
        keys = db.scalars(
            select(ApiKey)
            .where(ApiKey.portfolio_id == ctx.active_portfolio_id)
            .order_by(ApiKey.id.desc())
        ).all()
        return _render(
            request,
            ctx,
            "members.html",
            {
                # The portfolio's own settings, editable by an admin.
                "portfolio": db.get(Portfolio, ctx.active_portfolio_id),
                "currencies": sorted(money.SYMBOLS),
                "countries": COUNTRIES,
                "active_nav": "admin",
                "rows": rows,
                "candidates": candidates,
                "keys": keys,
                "roles": MEMBER_ROLES,
                   "role_blurbs": ROLE_BLURBS,
                "error": error,
                "new_key": request.query_params.get("key", ""),
            },
        )


@app.post("/members/add")
async def members_add(request: Request, user_id: int = Form(...), role: str = Form("member")):
    with scoped(request) as (ctx, db):
        _require_owner(ctx)
        await auth.verify_csrf(request, db)
        if role not in MEMBER_ROLES:
            raise HTTPException(400, "unknown role")
        if db.get(User, user_id) is None:
            raise HTTPException(404, "no such account")
        existing = db.scalar(
            select(PortfolioMember).where(
                PortfolioMember.portfolio_id == ctx.active_portfolio_id,
                PortfolioMember.user_id == user_id,
            )
        )
        if existing is not None:
            return _redirect("/members?error=Already+a+member")
        db.add(
            PortfolioMember(
                portfolio_id=ctx.active_portfolio_id, user_id=user_id, role=role
            )
        )
        db.flush()
        return _redirect("/members")


@app.post("/members/{member_id}/role")
async def members_role(request: Request, member_id: int, role: str = Form(...)):
    with scoped(request) as (ctx, db):
        _require_owner(ctx)
        await auth.verify_csrf(request, db)
        if role not in MEMBER_ROLES:
            raise HTTPException(400, "unknown role")
        member = db.get(PortfolioMember, member_id)
        if member is None or member.portfolio_id != ctx.active_portfolio_id:
            raise HTTPException(404, "no such member")
        if member.user_id == ctx.user.id and role != "owner":
            # Losing your own ownership could orphan the portfolio.
            return _redirect("/members?error=Hand+ownership+over+first")
        member.role = role
        db.flush()
        return _redirect("/members")


@app.post("/members/{member_id}/remove")
async def members_remove(request: Request, member_id: int):
    with scoped(request) as (ctx, db):
        _require_owner(ctx)
        await auth.verify_csrf(request, db)
        member = db.get(PortfolioMember, member_id)
        if member is None or member.portfolio_id != ctx.active_portfolio_id:
            raise HTTPException(404, "no such member")
        owners = db.scalar(
            select(func.count())
            .select_from(PortfolioMember)
            .where(
                PortfolioMember.portfolio_id == ctx.active_portfolio_id,
                PortfolioMember.role == "owner",
            )
        )
        if member.role == "owner" and owners <= 1:
            return _redirect("/members?error=A+portfolio+needs+an+owner")
        db.delete(member)
        db.flush()
        return _redirect("/members")


@app.post("/keys/new")
async def keys_new(request: Request, name: str = Form(...), scopes: str = Form("read")):
    """Issue an API key. The raw value is shown once, on the next page load."""
    with scoped(request) as (ctx, db):
        _require_owner(ctx)
        await auth.verify_csrf(request, db)
        if scopes not in ("read", "read,write"):
            raise HTTPException(400, "scopes must be 'read' or 'read,write'")
        raw, key_hash, prefix = auth.new_api_key()
        db.add(
            ApiKey(
                portfolio_id=ctx.active_portfolio_id,
                name=name.strip()[:80] or "unnamed",
                key_hash=key_hash,
                prefix=prefix,
                scopes=scopes,
                created_by=ctx.user.id,
            )
        )
        db.flush()
        log.info("api key %s issued for portfolio %d", prefix, ctx.active_portfolio_id)
        return _redirect(f"/members?key={raw}")


@app.post("/keys/{key_id}/revoke")
async def keys_revoke(request: Request, key_id: int):
    with scoped(request) as (ctx, db):
        _require_owner(ctx)
        await auth.verify_csrf(request, db)
        key = db.get(ApiKey, key_id)
        if key is None or key.portfolio_id != ctx.active_portfolio_id:
            raise HTTPException(404, "no such key")
        key.revoked_at = dt.datetime.now(dt.timezone.utc)
        db.flush()
        return _redirect("/members")


# Only a query string, and only characters a query string is made of. The
# chooser posts the page's current query back so that saving columns does not
# throw away the sort you were reading under — and this is what stops that
# convenience from becoming an open redirect. No scheme, no host, no path: the
# value can only ever be appended to "/".
_QUERY_ONLY = re.compile(r"^\?[A-Za-z0-9_=&%.,+:~-]*$")


def _safe_query(back: str | None) -> str:
    return back if back and _QUERY_ONLY.match(back) else ""


# What a back button says, for the paths one can point at. The label has to be
# decided WITH the path and not in the template: `?return=` is an arbitrary
# path, and a template turning one into a name is guessing. Anything not named
# here falls back to something the caller knows — the ticker, usually.
BACK_LABELS = navigation.BACK_LABELS


def _back_label(path: str, fallback: str) -> str:
    """The name of the page `path` leads to."""
    # A financial year is the one destination whose name is in the path rather
    # than in the table — there is one per year and they are generated.
    if path.startswith("/?fy="):
        year = path.removeprefix("/?fy=")
        if year.isdigit():
            return fyreport.fy_label(int(year))
    return navigation.back_label(path, fallback)


# Both live in `navigation` so the imports router can reach them too; a guard
# that is copied is a guard that will eventually be copied slightly wrong.
_safe_path = navigation.safe_path


@app.post("/profile/columns")
async def save_columns(request: Request):
    """Which holdings columns this person wants.

    A plain form post rather than anything cleverer: the page has to re-render
    to show the new table anyway, so a fetch would buy nothing but a code path
    that works differently with JavaScript off.

    New columns are appended rather than slotted into declaration order: the
    order is the user's, and re-sorting it here would undo every move
    they had made the moment they ticked one more box.
    """
    with scoped(request) as (ctx, db):
        await auth.verify_csrf(request, db)
        form = await request.form()
        wanted = columns.clean(form.getlist("column"))
        existing = [k for k in (ctx.user.dashboard_columns or []) if k in wanted]
        ctx.user.dashboard_columns = existing + [k for k in wanted if k not in existing]
        db.flush()
        return _redirect("/" + _safe_query(form.get("back")))


@app.post("/profile/columns/order")
async def order_columns(request: Request):
    """The order the columns are rendered in.

    One route for two interactions, deliberately. The form carries the whole
    current order as hidden inputs; the ↑/↓ buttons add `move=<key>:<direction>`
    and the server does the swap, while the drag enhancement rewrites the hidden
    inputs and submits with no `move` at all. Same code path either way, so the
    keyboard route and the mouse route cannot drift — and the keyboard one works
    with no JavaScript, which is the requirement drag cannot meet on its own.
    """
    with scoped(request) as (ctx, db):
        await auth.verify_csrf(request, db)
        form = await request.form()
        order = columns.clean(form.getlist("order"))
        move = form.get("move") or ""
        if ":" in move:
            key, _, direction = move.partition(":")
            order = columns.move(order, key, direction)
        ctx.user.dashboard_columns = order
        db.flush()
        return _redirect("/" + _safe_query(form.get("back")))


@app.post("/profile/columns/reset")
async def reset_columns(request: Request):
    """Back to the defaults, and back to *tracking* them — storing NULL rather
    than a copy of the current list means a later change to what the defaults
    are reaches this person too."""
    with scoped(request) as (ctx, db):
        await auth.verify_csrf(request, db)
        ctx.user.dashboard_columns = None
        db.flush()
        return _redirect("/")


@app.post("/profile/colors")
async def account_colors(request: Request):
    """Save this person's colour overrides.

    A blank field means "back to the default", which is stored as an ABSENT
    key rather than an empty string — so the stylesheet's own value shows
    through and a later change to it still reaches them. An all-blank form
    therefore stores NULL, which is the same state a fresh account is in.

    Validation lives in `theming.clean`: anything that is not a hex colour is
    dropped rather than saved, because a value the `<input type="color">`
    beside it cannot round-trip is worse than being told no.
    """
    with scoped(request) as (ctx, db):
        await auth.verify_csrf(request, db)
        form = await request.form()
        chosen = theming.clean({s.token: form.get(s.token, "") for s in theming.SWATCHES})
        ctx.user.theme_colors = chosen or None
        db.commit()
        return _redirect("/profile")


@app.post("/profile/colors/reset")
async def account_colors_reset(request: Request):
    """Back to the built-in palette.

    Stores NULL, not a copy of the defaults — the same distinction the columns
    reset makes. A stored copy would freeze today's colours into the account
    and quietly opt them out of every future change to them.
    """
    with scoped(request) as (ctx, db):
        await auth.verify_csrf(request, db)
        ctx.user.theme_colors = None
        db.commit()
        return _redirect("/profile")


@app.post("/profile/appearance")
async def account_appearance(
    request: Request,
    theme: str = Form("auto"),
    nav_style: str = Form("both"),
    times_in: str = Form("market"),
):
    """Theme, menu style and which nav entries to show.

    **Accent is not set here** — the Theme colours section does it, to any
    colour, and two controls for one value is how they end up disagreeing.
    `user.accent` is left alone rather than reset, so an account keeps its
    choice until it picks a colour, and the column still drives the per-mode
    accent defaults.
    """
    with scoped(request) as (ctx, db):
        await auth.verify_csrf(request, db)
        if (theme not in THEMES or nav_style not in NAV_STYLES
                or times_in not in TIME_ZONES_SHOWN):
            raise HTTPException(400, "unknown appearance option")
        ctx.user.theme = theme
        ctx.user.nav_style = nav_style
        ctx.user.times_in = times_in
        # Checkboxes say what to SHOW; what gets stored is what to hide. An
        # unticked box submits nothing, so "shown" cannot be read from absence —
        # and storing the hidden set means a nav entry added to the app later
        # appears for everybody rather than staying invisible until they find
        # this page.
        form = await request.form()
        shown = set(form.getlist("nav_show"))
        hidden = [key for key in navigation.HIDEABLE if key not in shown]
        ctx.user.nav_hidden = hidden or None
        db.flush()
        return _redirect("/profile?saved=appearance")


# --------------------------------------------------------------------------- #
# Dashboard / charts / calendar
# --------------------------------------------------------------------------- #

def _one_currency(holdings) -> str | None:
    """The single currency these holdings trade in, or None if there are
    several. None is not "unknown" — it is "this column cannot name one", and
    the header says `(native)` instead."""
    found = {h.instrument.currency for h in holdings}
    return found.pop() if len(found) == 1 else None


def _instrument_options(db, instruments):
    """What each option in the instrument picker needs to carry.

    The form fills in a price and an FX rate when you pick something, and doing
    that with a fetch per selection would be a round trip for data the page
    already had to load. So each option carries its own latest close and rate,
    and `tradeform.js` reads them off the selected option.
    """
    prices = queries.latest_prices(db, [i.id for i in instruments])
    rates = queries.latest_fx_rates(db, money.REPORTING)
    out = []
    for inst in instruments:
        price = prices.get(inst.id)
        out.append({
            "id": inst.id,
            "ticker": inst.ticker,
            "name": inst.name,
            "asset_class": inst.asset_class,
            "currency": inst.currency,
            "price": f"{price.normalize():f}" if price is not None else "",
            # Only for a foreign instrument: the reporting currency needs no
            # conversion, and offering "1" would invite it being edited.
            "fx": ("" if inst.currency == money.REPORTING
                   else rates.get(inst.currency, "")),
        })
    return out


def _dca_prefill(db):
    """Today's scheduled buy, as form values — or None.

    Skipped entirely when the DCA schedule feature is switched off: an install
    that turned it off should not get its trade form quietly filled in from a
    schedule it does not have.
    """
    if not features.enabled(settings, "dca_schedule"):
        return None
    return plans.prefill(db)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, fy: int | None = None):
    """Today's position, or a past financial year.

    The FY tabs replace the separate Reports page: a financial year is another
    view of the same portfolio, not a different feature.
    """
    with scoped(request) as (ctx, db):
        fys = fyreport.available_fys(db)
        query = dict(request.query_params)
        common = {
            "active_nav": "portfolio",
            "fys": fys,
            "fy_labels": {y: fyreport.fy_label(y) for y in fys},
            "fy": fy,
            "feed": feed_status,
            "latest_price_date": db.execute(select(func.max(Price.date))).scalar(),
            # Whether that latest figure is a live price for a session still
            # running, rather than a close. The label has to say which: "prices
            # to today" reads as settled, and quietly meaning something else is
            # how a number stops being trustworthy.
            # What the chooser posts back so that saving columns keeps whatever
            # sort you were reading under. `_safe_query` refuses anything that
            # is not purely a query string.
            "back": ("?" + str(request.url.query)) if request.url.query else "",
        }
        if fy is not None:
            if fy not in fys:
                raise HTTPException(404, "no such financial year")
            report = fyreport.fy_report(db, fy)
            # Three tables, three independent sorts. A shared `?sort=` would
            # order all of them at once. `fyreport` owns both the column names
            # and where the rows live, because it owns the row shapes.
            sorts = {
                table: sorting.read(query, table, keys)
                for table, keys in fyreport.SORTABLE.items()
            }
            fyreport.sort_tables(report, sorts)
            return _render(request, ctx, "dashboard_fy.html",
                           {**common, "r": report, "sorts": sorts,
                            # Only the snapshot's Price is in an instrument's
                            # own currency; everything else on this page is a
                            # tax figure and therefore in the reporting one.
                            # A dict, not an object — Jinja's `r.snapshot`
                            # hides that, Python does not.
                            "native_ccy": _one_currency(report["snapshot"])})
        holdings = queries.all_holdings(db)
        open_positions, _closed = queries.split_positions(holdings)
        cols = columns.chosen(ctx.user.dashboard_columns)
        sort = sorting.read(query, "holdings", columns.sortable_keys())
        if sort.key:
            # The allow-list is every column that EXISTS, not every column
            # showing: otherwise a bookmarked link would work or not depending
            # on whose preferences were loaded.
            open_positions = sorting.apply(
                open_positions, columns.sort_key(columns.BY_KEY[sort.key]),
                descending=sort.descending,
            )
        return _render(
            request,
            ctx,
            "dashboard.html",
            {
                **common,
                "totals": queries.totals(open_positions),
                # Built from the positions being shown, not from the instrument
                # table — see `_live_exchanges` for the zero-unit BTC row that
                # kept this lit all night.
                "live_exchanges": _live_exchanges(open_positions),
                "open_positions": open_positions,
                # Nothing has ever been recorded here, which is a different
                # state from "everything has been sold" — one wants an
                # invitation, the other wants five zeros and a closed-position
                # history. `holdings` is every position ever held, so an empty
                # list is the genuinely-new case.
                "first_run": not holdings,
                # The Record trade dialog renders the same include `/trade/new`
                # does, so it needs the same two things. Cheap: one indexed
                # query, and the dialog is on every dashboard load anyway.
                "instruments": db.scalars(
                    select(Instrument)
                    .where(Instrument.active.is_(True))
                    .order_by(Instrument.asset_class, Instrument.ticker)
                ).all(),
                "today": clock.today().isoformat(),
                # The performance chart, below the year tabs. Built from the
                # same daily series every other chart uses, so it cannot
                # disagree with the charts page about how the portfolio did.
                "summary": charts_build.summary_chart(
                    db, clock.today(),
                    fyreport.fy_bounds(fyreport.current_fy(clock.today()))[0]),
                "prefill": _dca_prefill(db),
                "options": _instrument_options(db, db.scalars(
                    select(Instrument).where(Instrument.active.is_(True))
                    .order_by(Instrument.asset_class, Instrument.ticker)).all()),
                "cols": cols,
                # The one currency these holdings trade in, or None when they
                # trade in several — which is what lets a native-currency
                # column name its currency in the header instead of repeating
                # a symbol down every cell. See `Column.heading`.
                "native_ccy": _one_currency(open_positions),
                # True when every figure in the table is in one currency, so
                # the table names it once instead of seven headers each
                # repeating it.
                "one_ccy": columns.one_currency_everywhere(
                    _one_currency(open_positions)),
                "col_groups": columns.groups(),
                "chosen_keys": {c.key for c in cols},
                "chosen_order": [c.key for c in cols],
                "sort": sort,
            },
        )


@app.post("/refresh")
async def refresh(request: Request):
    """Manual price-feed run, for when the scheduled one missed (pod restart,
    Yahoo hiccup). Fire-and-forget: the dashboard polls itself while running."""
    with scoped(request) as (ctx, db):
        await auth.verify_csrf(request, db)
        _require_write(ctx)
    if not feed_status["running"]:
        asyncio.get_running_loop().run_in_executor(None, _run_feed)
    return _redirect(request.headers.get("referer") or "/")


@app.get("/charts", response_class=HTMLResponse)
def charts(request: Request):
    with scoped(request) as (ctx, db):
        _seed_charts(db, ctx)
        saved = db.scalars(
            select(SavedChart).order_by(SavedChart.position, SavedChart.id)
        ).all()
        drawn, broken = [], []
        for chart in saved:
            try:
                drawn.append(
                    {
                        "id": chart.id,
                        "name": chart.name,
                        "width": chart.width,
                        "data": charts_build.run(db, chart.spec),
                    }
                )
            except ValueError as exc:
                # A spec the vocabulary no longer supports: say so on the card
                # rather than dropping the chart without explanation.
                broken.append({"id": chart.id, "name": chart.name, "why": str(exc)})
        return _render(
            request,
            ctx,
            "charts.html",
            {
                "active_nav": "charts",
                "charts": drawn,
                "broken": broken,
                "chart_json": {c["id"]: c["data"] for c in drawn},
                # Holdings left out of every AUD figure on this page because no
                # exchange rate is stored for their currency. Said once at the
                # top rather than per card: it applies to all of them equally,
                # and the same disclosure is on the dashboard.
                "excluded": queries.cached_portfolio_series(db).get("excluded", []),
            },
        )


def _seed_charts(db: DbSession, ctx) -> None:
    """Lay out the default charts for someone who has never seen the page.

    Guarded by `charts_seeded` rather than "has no charts", so deleting the lot
    is a decision that sticks instead of being undone on the next page load.
    """
    if ctx.user.charts_seeded:
        return
    for position, key in enumerate(chart_templates.DEFAULT_KEYS):
        template = chart_templates.BY_KEY[key]
        db.add(
            tenancy.owned(
                db,
                SavedChart(
                    name=template["name"],
                    spec=dict(template["spec"]),
                    template_key=key,
                    position=position,
                    width=template["width"],
                ),
            )
        )
    ctx.user.charts_seeded = True
    db.flush()
    log.info("seeded %d default charts for user %d", len(chart_templates.DEFAULT_KEYS), ctx.user.id)


@app.post("/charts/order")
async def charts_order(request: Request):
    """Persist the grid order after a drag. Ids not owned by this user are
    ignored rather than trusted."""
    body = await request.json()
    with scoped(request) as (ctx, db):
        await auth.verify_csrf(request, db)
        mine = {c.id: c for c in db.scalars(select(SavedChart)).all()}
        for position, raw in enumerate(body.get("order") or []):
            try:
                chart = mine.get(int(raw))
            except (TypeError, ValueError):
                continue   # not an id we issued; ignore rather than 500
            if chart is not None:
                chart.position = position
        db.flush()
        return {"ok": True}


# --------------------------------------------------------------------------- #
# Chart builder
# --------------------------------------------------------------------------- #

@app.get("/charts/build", response_class=HTMLResponse)
def chart_builder(request: Request, edit: int | None = None):
    """Drag fields onto shelves, see the chart as you go."""
    with scoped(request) as (ctx, db):
        existing = None
        if edit:
            existing = db.get(SavedChart, edit)
            # Charts belong to the USER and follow them between portfolios, so
            # ownership is the user id — checking the portfolio instead would
            # 404 someone's own chart while they had another portfolio active.
            if existing is None or existing.user_id != ctx.user.id:
                raise HTTPException(404, "no such chart")
        return _render(
            request,
            ctx,
            "chart_builder.html",
            {
                "active_nav": "charts",
                "catalogue": fields.catalogue(),
                "existing": existing,
                "existing_spec": existing.spec if existing else None,
                "filter_values": _filter_values(db),
                "templates": chart_templates.TEMPLATES,
                "templates_json": {
                    t["key"]: {"name": t["name"], "spec": t["spec"], "width": t["width"]}
                    for t in chart_templates.TEMPLATES
                },
            },
        )


def _filter_values(db: DbSession) -> dict:
    """What the filter shelf can offer — drawn from this portfolio, so nobody
    is invited to filter on a ticker they don't hold."""
    held = {h.instrument for h in queries.cached_holdings(db) if h.instrument.trades}
    return {
        "ticker": sorted({i.ticker for i in held}),
        "asset_class": sorted({i.asset_class for i in held}),
        "currency": sorted({i.currency for i in held}),
        "exchange": sorted({i.exchange for i in held}),
    }


@app.post("/charts/preview")
async def chart_preview(request: Request):
    """Data for a spec that hasn't been saved — the live preview.

    Same engine as a saved chart, so what you see while dragging is what you
    get afterwards.
    """
    spec = await request.json()
    with scoped(request) as (ctx, db):
        try:
            return charts_build.run(db, spec)
        except ValueError as exc:
            raise HTTPException(400, str(exc))


@app.post("/charts/save")
async def chart_save(request: Request):
    with scoped(request) as (ctx, db):
        _require_write(ctx)
        body = await request.json()
        await auth.verify_csrf(request, db)
        spec = body.get("spec") or {}
        name = (body.get("name") or "").strip()[:80]
        if not name:
            raise HTTPException(400, "give the chart a name")
        problem = fields.validate(spec)
        if problem:
            raise HTTPException(400, problem)
        width = "full" if body.get("width") == "full" else "half"
        chart_id = body.get("id")
        if chart_id:
            chart = db.get(SavedChart, int(chart_id))
            if chart is None or chart.user_id != ctx.user.id:
                raise HTTPException(404, "no such chart")
            chart.name, chart.spec, chart.width = name, spec, width
        else:
            last = db.scalar(select(func.max(SavedChart.position))) or 0
            chart = tenancy.owned(
                db,
                SavedChart(
                    name=name,
                    spec=spec,
                    width=width,
                    position=last + 1,
                    template_key=body.get("template_key"),
                ),
            )
            db.add(chart)
        db.flush()
        return {"id": chart.id, "name": chart.name}


@app.post("/charts/{chart_id}/delete")
async def chart_delete(request: Request, chart_id: int):
    with scoped(request) as (ctx, db):
        _require_write(ctx)
        await auth.verify_csrf(request, db)
        chart = db.get(SavedChart, chart_id)
        if chart is None or chart.user_id != ctx.user.id:
            raise HTTPException(404, "no such chart")
        db.delete(chart)
        db.flush()
        return _redirect("/charts")


@app.get("/calendar")
def calendar_legacy_redirect(year: int | None = None, month: int | None = None):
    """The calendar lives on /schedule now (merged 2026-08-01)."""
    suffix = f"?year={year}&month={month}" if year and month else ""
    return _redirect(f"/schedule{suffix}")


@app.get("/reports")
@app.get("/reports/fy")
@app.get("/fy")
def fy_legacy_redirect(year: int | None = None):
    """FY reporting moved onto the portfolio page as tabs (2026-08-01)."""
    return _redirect(f"/?fy={year}" if year else "/")


# --------------------------------------------------------------------------- #
# Investment plan (the rotation formerly known as DCA)
# --------------------------------------------------------------------------- #

@app.get("/dca")
def dca_legacy_redirect():
    return _redirect("/schedule")


@app.get("/schedule", response_class=HTMLResponse)
def plan_page(
    request: Request, year: int | None = None, month: int | None = None, edit: int = 0
):
    """Calendar and the rotation on one page — what's coming, then what drives it."""
    today = clock.today()
    year = year or today.year
    month = month or today.month
    if not 1 <= month <= 12:
        raise HTTPException(400, "month must be 1-12")
    with scoped(request) as (ctx, db):
        # 24 rather than 6: these are the Preview plan's rows, and a preview
        # you cannot scroll is a list. Roughly two years of a monthly plan.
        sched = plans.schedule(db, upcoming=24)
        instruments = db.scalars(
            select(Instrument).where(Instrument.active.is_(True)).order_by(Instrument.ticker)
        ).all()
        return _render(
            request,
            ctx,
            "plan.html",
            {
                "active_nav": "plan",
                "sched": sched,
                "plan": sched["plan"],
                "instruments": instruments,
                # Only needed when there is no plan to show; cheap either way.
                "suggested_rotation": plans.suggested_rotation(db),
                "edit": bool(edit),
                **calendarview.month_grid(db, year, month),
            },
        )


@app.get("/feed/status")
def feed_status_json(request: Request):
    """Whether the price feed is running, for the page to watch.

    Lets the page ask, and reload once, when there is something new — rather
    than reloading on a blind timer whether or not anything has finished.
    """
    with scoped(request) as (_ctx, _db):
        return JSONResponse({
            "running": bool(feed_status["running"]),
            "ok": feed_status["ok"],
            "finished": feed_status["finished"].isoformat()
            if feed_status["finished"] else None,
            # The live-price poll writes new numbers into the database every few
            # minutes, and a page rendered before that has no way to know. This
            # is what an open dashboard compares against what it was rendered
            # with — without it the only way to see a fresh price was to reload
            # by hand, which makes the refresh look broken when it is working.
            "quoted": feed_status["quoted"].isoformat()
            if feed_status["quoted"] else None,
        })


@app.post("/schedule/preview")
async def plan_preview(
    request: Request,
    interval_days: int = Form(...),
    start_date: str = Form(""),
    tickers: str = Form(""),
):
    """The dates a DRAFT rotation would produce. Saves nothing.

    Live, so rearranging the rotation shows what it produces without having to
    save first. Live by asking the SERVER rather than projecting dates in
    JavaScript: `plans.project` is the same arithmetic the real schedule uses,
    so the preview cannot drift from it.

    A POST because it carries a draft and must not be cached or bookmarked —
    not because it writes. Nothing here touches the database except to read
    where the rotation currently stands.
    """
    with scoped(request) as (ctx, db):
        await auth.verify_csrf(request, db)
        _require_write(ctx)
        if interval_days < 1 or interval_days > 365:
            return JSONResponse({"rows": [], "why": "interval must be 1–365 days"})
        start = None
        if start_date.strip():
            try:
                start = dt.date.fromisoformat(start_date.strip())
            except ValueError:
                return JSONResponse({"rows": [], "why": "start date must be YYYY-MM-DD"})

        wanted = [t.strip().upper() for t in tickers.replace("\n", ",").split(",") if t.strip()]
        known = {
            i.ticker
            for i in db.scalars(select(Instrument).where(Instrument.ticker.in_(set(wanted))))
        }
        # An unknown ticker is refused on save, so the preview says so here
        # rather than drawing dates for a plan that cannot be saved.
        unknown = sorted({t for t in wanted if t not in known})
        if unknown:
            return JSONResponse(
                {"rows": [], "why": f"unknown ticker(s): {', '.join(unknown)}"})

        rows = plans.preview(db, wanted, interval_days, start)
        return JSONResponse(
            {"rows": [{"date": due.isoformat(), "ticker": ticker} for due, ticker in rows],
             "why": ""})


@app.post("/schedule/save")
async def plan_save(
    request: Request,
    name: str = Form(...),
    interval_days: int = Form(...),
    amount: str = Form(""),
    brokerage: str = Form("9.50"),
    start_date: str = Form(""),
    tickers: str = Form(""),
):
    """Create or update the plan, including its whole rotation.

    The rotation arrives as an ordered comma/newline separated ticker list —
    the same shape as the config.yaml it replaces, and easy to paste.
    """
    with scoped(request) as (ctx, db):
        await auth.verify_csrf(request, db)
        _require_write(ctx)
        if interval_days < 1 or interval_days > 365:
            raise HTTPException(400, "interval must be between 1 and 365 days")
        try:
            amount_val = Decimal(amount) if amount.strip() else None
            brokerage_val = Decimal(brokerage or "0")
        except InvalidOperation:
            raise HTTPException(400, "amount/brokerage must be numbers")
        start = None
        if start_date.strip():
            try:
                start = dt.date.fromisoformat(start_date.strip())
            except ValueError:
                raise HTTPException(400, "start date must be YYYY-MM-DD")

        wanted = [t.strip().upper() for t in tickers.replace("\n", ",").split(",") if t.strip()]
        by_ticker = {
            i.ticker: i
            for i in db.scalars(select(Instrument).where(Instrument.ticker.in_(set(wanted))))
        }
        unknown = [t for t in wanted if t not in by_ticker]
        if unknown:
            raise HTTPException(
                400, f"unknown ticker(s): {', '.join(sorted(set(unknown)))} — add them first"
            )

        plan = plans.active_plan(db)
        if plan is None:
            plan = tenancy.owned(db, InvestmentPlan(name=name.strip() or "My plan"))
            db.add(plan)
            db.flush()
        plan.name = name.strip() or "My plan"
        plan.interval_days = interval_days
        plan.amount = amount_val
        plan.brokerage = brokerage_val
        plan.start_date = start
        plans.set_entries(db, plan, [by_ticker[t].id for t in wanted])
        return _redirect("/schedule")


def _verify_next(db: DbSession, ticker: str, due_date: str) -> plans.UpcomingBuy:
    """Guard: the submitted entry must still be the computed next (double
    submit, or another buy recorded since the form was opened)."""
    nxt = plans.next_buy(db)
    if nxt is None:
        raise HTTPException(404, "no rotation configured")
    if nxt.ticker != ticker or nxt.due_date.isoformat() != due_date:
        raise HTTPException(409, f"next buy is now {nxt.ticker} ({nxt.due_date}) — reload")
    return nxt


@app.get("/schedule/complete", response_class=HTMLResponse)
def plan_complete_form(request: Request):
    with scoped(request) as (ctx, db):
        nxt = plans.next_buy(db)
        if nxt is None:
            return _redirect("/schedule")
        inst = db.get(Instrument, nxt.instrument_id) if nxt.instrument_id else None
        latest = queries._latest_price(db, inst.id) if inst else None
        plan = plans.active_plan(db)
        return _render(
            request,
            ctx,
            "plan_complete.html",
            {
                "active_nav": "plan",
                "nxt": nxt,
                "price": latest[0] if latest else None,
                "today": clock.today().isoformat(),
                "brokerage": plan.brokerage if plan else Decimal("9.50"),
            },
        )


@app.post("/schedule/complete")
async def plan_complete(
    request: Request,
    ticker: str = Form(...),
    due_date: str = Form(...),
    trade_date: str = Form(...),
    quantity: str = Form(...),
    unit_price: str = Form(...),
    brokerage: str = Form("9.50"),
):
    with scoped(request) as (ctx, db):
        await auth.verify_csrf(request, db)
        _require_write(ctx)
        try:
            date = dt.date.fromisoformat(trade_date)
            qty = Decimal(quantity)
            price = Decimal(unit_price)
            brk = Decimal(brokerage or "0")
        except (ValueError, InvalidOperation):
            raise HTTPException(400, "bad date/quantity/price/brokerage")
        if qty <= 0 or price <= 0:
            raise HTTPException(400, "quantity and price must be positive")
        nxt = _verify_next(db, ticker, due_date)
        inst = db.get(Instrument, nxt.instrument_id)
        if inst is None:
            raise HTTPException(400, f"instrument {nxt.ticker} not found")
        trade = tenancy.owned(
            db,
            Trade(
                instrument_id=inst.id,
                date=date,
                type="buy",
                quantity=qty,
                unit_price=price,
                brokerage=brk,
                fx_rate=Decimal(1) if inst.currency == "AUD" else None,
                note=f"planned buy (scheduled {nxt.due_date})",
            ),
        )
        db.add(trade)
        db.flush()  # trade needs its id before we can link it
        db.add(
            tenancy.owned(
                db,
                PlannedPurchase(
                    due_date=nxt.due_date,
                    instrument_id=inst.id,
                    plan_entry_id=nxt.entry_id,
                    status="done",
                    trade_id=trade.id,
                ),
            )
        )
        return _redirect(f"/holding/{ticker}")


@app.post("/schedule/skip")
async def plan_skip(request: Request, ticker: str = Form(...), due_date: str = Form(...)):
    with scoped(request) as (ctx, db):
        await auth.verify_csrf(request, db)
        _require_write(ctx)
        nxt = _verify_next(db, ticker, due_date)
        db.add(
            tenancy.owned(
                db,
                PlannedPurchase(
                    due_date=nxt.due_date,
                    instrument_id=nxt.instrument_id,
                    plan_entry_id=nxt.entry_id,
                    status="skipped",
                ),
            )
        )
        return _redirect("/schedule")




# --------------------------------------------------------------------------- #
# Exports
# --------------------------------------------------------------------------- #

@app.get("/export")
def export_legacy_redirect():
    """Exports live on the imports page now (merged 2026-08-01)."""
    return _redirect("/imports-exports#exports")


def _filename(stem: str, fy: int | None, ticker: str | None, ext: str) -> str:
    parts = ["portfolio", stem]
    if ticker:
        parts.append(ticker.lower())
    if fy:
        parts.append(f"fy{fy}")
    parts.append(clock.today().isoformat())
    return "-".join(parts) + "." + ext


@app.get("/export/download")
def export_download(
    request: Request,
    report: str = "transactions",
    fmt: str = "csv",
    fy: str = "",
    ticker: str = "",
):
    """One report as CSV, or every selected report as sheets in one workbook.

    `fy` arrives as a string because the form's "All years" option submits an
    empty value; typing it as int|None made FastAPI reject every download with
    a parse error before the handler ran.
    """
    if fmt not in ("csv", "xlsx"):
        raise HTTPException(400, "format must be csv or xlsx")
    year: int | None = None
    if fy.strip():
        if not fy.strip().isdigit():
            raise HTTPException(400, "financial year must be a year, e.g. 2026")
        year = int(fy)
    wanted = [report] if report != "all" else list(exports.TITLES)
    unknown = [r for r in wanted if r not in exports.TITLES]
    if unknown:
        raise HTTPException(400, f"unknown report(s): {', '.join(unknown)}")
    if report == "all" and fmt == "csv":
        raise HTTPException(400, "choose Excel for the combined workbook")

    with scoped(request) as (ctx, db):
        sheets = exports.build(db, wanted, fy=year, ticker=ticker.upper() or None)
        stem = "everything" if report == "all" else report
        if fmt == "csv":
            headers, rows = next(iter(sheets.values()))
            body = exports.to_csv(headers, rows)
            media = "text/csv; charset=utf-8"
        else:
            body = exports.to_xlsx(sheets, note=exports.ABOUT)
            media = (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
        name = _filename(stem, year, ticker or None, fmt)
        log.info("export %s (%s) by user %d", stem, fmt, ctx.user.id)
        return Response(
            content=body,
            media_type=media,
            headers={"content-disposition": f'attachment; filename="{name}"'},
        )


# --------------------------------------------------------------------------- #
# Ad-hoc trades (anything outside the plan — the individual shares, one-offs)
# --------------------------------------------------------------------------- #

def _parse_time(set_time: str, trade_time: str) -> dt.time | None:
    """The optional intra-day time, or MARKET_OPEN when the box isn't ticked.

    Returns None only when the box IS ticked and what was typed isn't a time,
    which the callers turn into a validation message.
    """
    if not set_time:
        return MARKET_OPEN
    try:
        return dt.time.fromisoformat(trade_time)
    except ValueError:
        return None


def _held_trades(db: DbSession, instrument_id: int) -> list[Trade]:
    return list(db.scalars(select(Trade).where(Trade.instrument_id == instrument_id)).all())


def _breach(
    db: DbSession, inst: Instrument, candidate: Trade | None, *, exclude_ids=frozenset()
) -> str | None:
    """Validate the WHOLE timeline for this instrument, and say what breaks.

    Every write path goes through here — create, edit, delete and the API —
    because each of them can strand a trade that isn't the one being touched.
    Cutting an old buy's quantity, moving its date later, or deleting it
    outright all break a sell that was fine a moment ago.
    """
    found = queries.balance_breach(
        _held_trades(db, inst.id), candidate, exclude_ids=exclude_ids
    )
    return None if found is None else queries.breach_message(inst.ticker, found)


@app.get("/trade/new", response_class=HTMLResponse)
def trade_form(request: Request, ticker: str = "", error: str = ""):
    with scoped(request) as (ctx, db):
        instruments = db.scalars(
            select(Instrument)
            .where(Instrument.active.is_(True))
            .order_by(Instrument.asset_class, Instrument.ticker)
        ).all()
        return _render(
            request,
            ctx,
            "trade_new.html",
            {
                "active_nav": "trade",
                "instruments": instruments,
                "ticker": ticker.upper(),
                "today": clock.today().isoformat(),
                "prefill": _dca_prefill(db),
                "options": _instrument_options(db, instruments),
                "error": error,
                "form": {},
                "edit": None,
                # Reached from the dashboard's Record trade button and from a
                # holding's ledger. The `?ticker=` in the URL is what tells the
                # two apart — state, not a guess at the referrer, which is why
                # the button can name where it lands.
                "return_to": f"/holding/{ticker.upper()}" if ticker else "/",
                "back_label": ticker.upper() if ticker else "Portfolio",
            },
        )


@app.post("/trade/new")
async def trade_create(
    request: Request,
    instrument_id: str = Form(...),
    type: str = Form(...),
    trade_date: str = Form(...),
    set_time: str = Form(""),
    trade_time: str = Form(""),
    quantity: str = Form(...),
    unit_price: str = Form(...),
    brokerage: str = Form("0"),
    fx_rate: str = Form(""),
    note: str = Form(""),
    # Only used when instrument_id == "new" (added inline from this page).
    new_ticker: str = Form(""),
    new_name: str = Form(""),
    new_exchange: str = Form("ASX"),
    new_asset_class: str = Form("etf"),
    new_currency: str = Form("AUD"),
    new_yahoo: str = Form(""),
    new_drp: str = Form(""),
):
    with scoped(request) as (ctx, db):
        await auth.verify_csrf(request, db)
        _require_write(ctx)
        instruments = db.scalars(
            select(Instrument)
            .where(Instrument.active.is_(True))
            .order_by(Instrument.asset_class, Instrument.ticker)
        ).all()

        def _reject(message: str):
            return _render(
                request,
                ctx,
                "trade_new.html",
                {
                    "instruments": instruments,
                    "ticker": "",
                    "today": clock.today().isoformat(),
                    "error": message,
                    "edit": None,
                    # A rejected submission has no ticker to go back to — the
                    # instrument may be the thing that was wrong.
                    "return_to": "/",
                    "back_label": "Portfolio",
                    "form": {
                        "instrument_id": instrument_id,
                        "type": type,
                        "trade_date": trade_date,
                        "set_time": set_time,
                        "trade_time": trade_time,
                        "quantity": quantity,
                        "unit_price": unit_price,
                        "brokerage": brokerage,
                        "fx_rate": fx_rate,
                        "note": note,
                        "new_ticker": new_ticker,
                        "new_name": new_name,
                        "new_exchange": new_exchange,
                        "new_asset_class": new_asset_class,
                        "new_currency": new_currency,
                        "new_yahoo": new_yahoo,
                        "new_drp": new_drp,
                    },
                },
            )

        if type not in ("buy", "sell"):
            return _reject("Choose buy or sell.")
        try:
            date = dt.date.fromisoformat(trade_date)
            qty = Decimal(quantity)
            price = Decimal(unit_price)
            brk = Decimal(brokerage or "0")
            fx = Decimal(fx_rate) if fx_rate.strip() else None
        except (ValueError, InvalidOperation):
            return _reject("Date, units, price, brokerage and FX must be numbers (date as YYYY-MM-DD).")
        if qty <= 0 or price <= 0:
            return _reject("Units and price must be greater than zero.")
        if brk < 0 or (fx is not None and fx <= 0):
            return _reject("Brokerage can't be negative and FX must be positive.")
        if date > clock.today():
            return _reject("That date is in the future.")
        when = _parse_time(set_time, trade_time)
        if when is None:
            return _reject("That time isn't a time — use HH:MM, like 14:30.")

        # "new" means the fields below the picker were filled in: create the
        # instrument and record the trade in one go, rather than sending someone
        # to another page and losing what they typed.
        if instrument_id == "new":
            ticker = new_ticker.strip().upper()
            problem = ticker_problem(ticker) or exchange_problem(new_exchange or "ASX")
            if problem:
                return _reject(problem)
            if new_asset_class not in ("etf", "share", "crypto"):
                return _reject("Pick an asset class for the new instrument.")
            exchange = (new_exchange or "ASX").strip().upper()
            inst = db.scalar(
                select(Instrument).where(
                    Instrument.exchange == exchange, Instrument.ticker == ticker
                )
            )
            if inst is None:
                found = pricefeed.lookup(ticker, exchange)
                inst = Instrument(
                    ticker=ticker,
                    exchange=exchange,
                    name=(new_name.strip() or found.get("name") or None),
                    currency=(
                        new_currency.strip().upper() or found.get("currency") or "AUD"
                    ),
                    asset_class=new_asset_class,
                    drp=bool(new_drp),
                    active=True,
                    yahoo_symbol=new_yahoo.strip()
                    or found.get("symbol")
                    or pricefeed.yahoo_symbol_for(ticker, exchange),
                )
                db.add(inst)
                db.flush()
            if queries.prefs_by_instrument(db).get(inst.id) is None:
                db.add(
                    tenancy.owned(
                        db, HoldingPref(instrument_id=inst.id, drp=bool(new_drp))
                    )
                )
            db.flush()
            _kick_feed()  # its price history arrives with the next run
        else:
            try:
                inst = db.get(Instrument, int(instrument_id))
            except ValueError:
                inst = None
        if inst is None:
            return _reject("Pick an instrument.")

        trade = tenancy.owned(
            db,
            Trade(
                instrument_id=inst.id,
                date=date,
                time=when,
                type=type,
                quantity=qty,
                unit_price=price,
                brokerage=brk,
                # AUD is 1:1; for anything else an unset rate is backfilled from
                # the stored FX close for that date by the price feed.
                fx_rate=Decimal(1) if inst.currency == "AUD" else fx,
                note=note.strip() or None,
            ),
        )
        problem = _breach(db, inst, trade)
        if problem:
            return _reject(problem)
        db.add(trade)
        db.flush()
        return _redirect(f"/holding/{inst.ticker}")


# --------------------------------------------------------------------------- #
# Editing and removing what's already recorded — decisions.md #91.
# --------------------------------------------------------------------------- #

def _owning_dividend(db: DbSession, trade_id: int) -> Dividend | None:
    return db.scalar(select(Dividend).where(Dividend.reinvest_trade_id == trade_id))


def _trade_for_edit(db: DbSession, trade_id: int) -> Trade:
    """The trade, or a 404/409 explaining why it can't be edited directly."""
    trade = db.get(Trade, trade_id)
    # `db.get` bypasses the tenancy filter (it can return from the identity
    # map), so the ownership check is explicit here rather than assumed.
    if trade is None or trade.portfolio_id != tenancy.current_portfolio_id(db):
        raise HTTPException(404, "no such trade")
    if _owning_dividend(db, trade.id) is not None:
        raise HTTPException(
            409,
            "That's a reinvested distribution — edit it through its dividend so "
            "the units and the cash stay in step.",
        )
    return trade


def _log_change(kind: str, row_id: int, before: dict, after: dict) -> None:
    """What changed, at INFO. `user_id` stays the recorder, not the last
    editor, so this log is the only record of who touched what."""
    changed = {k: (before[k], after[k]) for k in before if before[k] != after[k]}
    if changed:
        log.info("%s %s edited: %s", kind, row_id, changed)


def _trade_snapshot(t: Trade) -> dict:
    return {
        "date": t.date,
        "time": t.time,
        "type": t.type,
        "quantity": t.quantity,
        "unit_price": t.unit_price,
        "brokerage": t.brokerage,
        "fx_rate": t.fx_rate,
        "note": t.note,
    }


@app.get("/trade/{trade_id}/edit", response_class=HTMLResponse)
def trade_edit_form(request: Request, trade_id: int, error: str = ""):
    with scoped(request) as (ctx, db):
        _require_write(ctx)
        trade = _trade_for_edit(db, trade_id)
        inst = db.get(Instrument, trade.instrument_id)
        # Where to go back to once this trade is dealt with. Arrives as
        # `?return=`, from whatever linked here — the holdings page's "what is
        # blocking this" panel is the first caller. Validated rather than
        # trusted: it ends up in a Location header.
        back_to = _safe_path(request.query_params.get("return"),
                             f"/holding/{inst.ticker}")
        return _render(
            request,
            ctx,
            "trade_new.html",
            {
                "active_nav": "holdings",
                "instruments": [inst],
                "ticker": inst.ticker,
                "today": clock.today().isoformat(),
                "error": error,
                "edit": trade,
                "inst": inst,
                "return_to": back_to,
                # The back button goes wherever the delete button already
                # goes. Two controls on one page that both mean "I am finished
                # here" must not land in different places.
                "back_label": _back_label(back_to, inst.ticker),
                "form": {
                    "instrument_id": str(inst.id),
                    "type": trade.type,
                    "trade_date": trade.date.isoformat(),
                    # Ticked only when the time carries information — a trade
                    # left at the open shouldn't come back looking deliberate.
                    "set_time": "1" if trade.time not in (None, MARKET_OPEN) else "",
                    "trade_time": (trade.time or MARKET_OPEN).strftime("%H:%M"),
                    "quantity": f"{trade.quantity.normalize():f}",
                    "unit_price": f"{trade.unit_price.normalize():f}",
                    "brokerage": f"{trade.brokerage:f}",
                    "fx_rate": "" if trade.fx_rate is None else f"{trade.fx_rate.normalize():f}",
                    "note": trade.note or "",
                },
            },
        )


@app.post("/trade/{trade_id}/edit")
async def trade_edit(
    request: Request,
    trade_id: int,
    type: str = Form(...),
    trade_date: str = Form(...),
    set_time: str = Form(""),
    trade_time: str = Form(""),
    quantity: str = Form(...),
    unit_price: str = Form(...),
    brokerage: str = Form("0"),
    fx_rate: str = Form(""),
    note: str = Form(""),
):
    with scoped(request) as (ctx, db):
        await auth.verify_csrf(request, db)
        _require_write(ctx)
        trade = _trade_for_edit(db, trade_id)
        inst = db.get(Instrument, trade.instrument_id)

        def _reject(message: str):
            return _redirect(
                f"/trade/{trade_id}/edit?error={quote_plus(message)}"
            )

        if type not in ("buy", "sell"):
            return _reject("Choose buy or sell.")
        try:
            date = dt.date.fromisoformat(trade_date)
            qty = Decimal(quantity)
            price = Decimal(unit_price)
            brk = Decimal(brokerage or "0")
            fx = Decimal(fx_rate) if fx_rate.strip() else None
        except (ValueError, InvalidOperation):
            return _reject("Date, units, price, brokerage and FX must be numbers (date as YYYY-MM-DD).")
        if qty <= 0 or price <= 0:
            return _reject("Units and price must be greater than zero.")
        if brk < 0 or (fx is not None and fx <= 0):
            return _reject("Brokerage can't be negative and FX must be positive.")
        if date > clock.today():
            return _reject("That date is in the future.")
        when = _parse_time(set_time, trade_time)
        if when is None:
            return _reject("That time isn't a time — use HH:MM, like 14:30.")

        # Check the edit against the timeline WITHOUT the original row: the
        # candidate replaces it. Validating the new values alone would miss a
        # quantity cut or a date move that strands a later sell.
        candidate = Trade(
            instrument_id=inst.id,
            date=date,
            time=when,
            type=type,
            quantity=qty,
            unit_price=price,
            brokerage=brk,
            fx_rate=Decimal(1) if inst.currency == "AUD" else fx,
        )
        # The candidate stands in for a SAVED row, so it keeps that row's place
        # in the day rather than sorting to the end as a new entry would.
        candidate.id = trade.id
        problem = _breach(db, inst, candidate, exclude_ids={trade.id})
        if problem:
            return _reject(problem)

        before = _trade_snapshot(trade)
        trade.date = date
        trade.time = when
        trade.type = type
        trade.quantity = qty
        trade.unit_price = price
        trade.brokerage = brk
        trade.fx_rate = Decimal(1) if inst.currency == "AUD" else fx
        trade.note = note.strip() or None
        _log_change("trade", trade.id, before, _trade_snapshot(trade))
        db.flush()
        return _redirect(f"/holding/{inst.ticker}")


def _release_planned_purchases(db: DbSession, trade_id: int) -> None:
    """Keep the plan history, drop the link to a trade that no longer exists.

    The purchase happened — deleting the trade doesn't unschedule it — so the
    row stays and says what became of it.
    """
    for purchase in db.scalars(
        select(PlannedPurchase).where(PlannedPurchase.trade_id == trade_id)
    ).all():
        purchase.trade_id = None
        note = " ".join(filter(None, [purchase.note, "(trade removed)"]))
        purchase.note = note[:200]  # the column's width, not a guess


@app.post("/trade/{trade_id}/delete")
async def trade_delete(request: Request, trade_id: int, return_to: str = Form("")):
    """Delete, then go back where you came from.

    Deleting a trade returns to the window you came from, wherever that was.
    Carried explicitly through the form rather than read from `Referer` — that
    header is absent under several privacy settings, and the one time it is
    missing is the one time somebody is lost.
    """
    with scoped(request) as (ctx, db):
        await auth.verify_csrf(request, db)
        _require_write(ctx)
        trade = _trade_for_edit(db, trade_id)
        inst = db.get(Instrument, trade.instrument_id)
        # A path on this site or the instrument's own page — never the
        # dashboard, which is further from where they were than the fallback.
        back = _safe_path(return_to, f"/holding/{inst.ticker}")
        # Removing a buy can strand a sell that depended on its units.
        problem = _breach(db, inst, None, exclude_ids={trade.id})
        if problem:
            return _redirect(f"/holding/{inst.ticker}?error={quote_plus(problem)}")
        _release_planned_purchases(db, trade.id)
        log.info("trade %s deleted: %s", trade.id, _trade_snapshot(trade))
        db.delete(trade)
        db.flush()
        return _redirect(back)


def _dividend_for_edit(db: DbSession, dividend_id: int) -> Dividend:
    dividend = db.get(Dividend, dividend_id)
    if dividend is None or dividend.portfolio_id != tenancy.current_portfolio_id(db):
        raise HTTPException(404, "no such dividend")
    return dividend


@app.get("/dividend/{dividend_id}/edit", response_class=HTMLResponse)
def dividend_edit_form(request: Request, dividend_id: int, error: str = ""):
    with scoped(request) as (ctx, db):
        _require_write(ctx)
        dividend = _dividend_for_edit(db, dividend_id)
        inst = db.get(Instrument, dividend.instrument_id)
        drp = dividend.reinvest_trade
        return _render(
            request,
            ctx,
            "dividend_edit.html",
            {
                "active_nav": "holdings",
                "inst": inst,
                "dividend": dividend,
                "drp": drp,
                "today": clock.today().isoformat(),
                "error": error,
            },
        )


@app.post("/dividend/{dividend_id}/edit")
async def dividend_edit(
    request: Request,
    dividend_id: int,
    div_date: str = Form(...),
    cash_amount: str = Form(...),
    franking_credits: str = Form(""),
    quantity: str = Form(""),
    unit_price: str = Form(""),
    note: str = Form(""),
):
    with scoped(request) as (ctx, db):
        await auth.verify_csrf(request, db)
        _require_write(ctx)
        dividend = _dividend_for_edit(db, dividend_id)
        inst = db.get(Instrument, dividend.instrument_id)
        drp = dividend.reinvest_trade

        def _reject(message: str):
            return _redirect(f"/dividend/{dividend_id}/edit?error={quote_plus(message)}")

        try:
            date = dt.date.fromisoformat(div_date)
            cash = Decimal(cash_amount)
            franking = Decimal(franking_credits) if franking_credits.strip() else None
        except (ValueError, InvalidOperation):
            return _reject("Date, cash and franking must be numbers (date as YYYY-MM-DD).")
        if cash <= 0:
            return _reject("A distribution has to be more than zero.")
        if franking is not None and franking < 0:
            return _reject("Franking credits can't be negative.")
        if date > clock.today():
            return _reject("That date is in the future.")

        # A reinvested distribution carries units too — the pair moves together
        # or not at all, so both halves are validated before either is written.
        if drp is not None:
            try:
                qty = Decimal(quantity)
                price = Decimal(unit_price)
            except (ValueError, InvalidOperation):
                return _reject("Units and price must be numbers.")
            if qty <= 0 or price <= 0:
                return _reject("Units and price must be greater than zero.")
            candidate = Trade(
                instrument_id=inst.id,
                date=date,
                time=drp.time,
                type="drp",
                quantity=qty,
                unit_price=price,
                brokerage=drp.brokerage,
                fx_rate=drp.fx_rate,
            )
            candidate.id = drp.id
            problem = _breach(db, inst, candidate, exclude_ids={drp.id})
            if problem:
                return _reject(problem)

        before = {
            "date": dividend.date,
            "cash_amount": dividend.cash_amount,
            "franking_credits": dividend.franking_credits,
            "note": dividend.note,
        }
        dividend.date = date
        dividend.cash_amount = cash
        dividend.franking_credits = franking
        dividend.note = note.strip() or None
        if drp is not None:
            drp.date = date
            drp.quantity = qty
            drp.unit_price = price
        _log_change(
            "dividend",
            dividend.id,
            before,
            {
                "date": dividend.date,
                "cash_amount": dividend.cash_amount,
                "franking_credits": dividend.franking_credits,
                "note": dividend.note,
            },
        )
        db.flush()
        return _redirect(f"/holding/{inst.ticker}")


@app.post("/dividend/{dividend_id}/delete")
async def dividend_delete(request: Request, dividend_id: int):
    with scoped(request) as (ctx, db):
        await auth.verify_csrf(request, db)
        _require_write(ctx)
        dividend = _dividend_for_edit(db, dividend_id)
        inst = db.get(Instrument, dividend.instrument_id)
        drp = dividend.reinvest_trade
        if drp is not None:
            # The units go with the cash: they were the same event. Check the
            # timeline survives losing them before removing either.
            problem = _breach(db, inst, None, exclude_ids={drp.id})
            if problem:
                return _redirect(f"/holding/{inst.ticker}?error={quote_plus(problem)}")
        log.info(
            "dividend %s deleted: %s on %s%s",
            dividend.id,
            dividend.cash_amount,
            dividend.date,
            f" (with DRP trade {drp.id})" if drp is not None else "",
        )
        # The FK is RESTRICT, so the dividend's reference has to go first.
        dividend.reinvest_trade = None
        db.delete(dividend)
        if drp is not None:
            _release_planned_purchases(db, drp.id)
            db.delete(drp)
        db.flush()
        return _redirect(f"/holding/{inst.ticker}")


# --------------------------------------------------------------------------- #
# Instruments
# --------------------------------------------------------------------------- #

def _blocking_trades(db, instruments) -> dict[int, dict]:
    """What stands in the way of removing each instrument.

    Two counts, and the difference between them is the point:

    * **`mine`** — this portfolio's trades, listed with links, because those
      are the ones the person reading can actually go and fix.
    * **`total`** — every portfolio's, counted through `unscoped_session`.

    The total is what decides whether removal is allowed: an instrument is
    shared, so deactivating one that another household member is using would
    stop their prices updating. The *list* stays scoped, because showing
    somebody another portfolio's trades would be a leak — so when the two
    numbers differ, the panel says how many others exist and nothing more.
    """
    ids = [i.id for i in instruments]
    if not ids:
        return {}

    mine: dict[int, list[Trade]] = {}
    for trade in db.scalars(
        select(Trade).where(Trade.instrument_id.in_(ids)).order_by(Trade.date.desc())
    ):
        mine.setdefault(trade.instrument_id, []).append(trade)

    with tenancy.unscoped_session(SessionLocal) as everyone:
        totals = dict(
            everyone.execute(
                select(Trade.instrument_id, func.count())
                .where(Trade.instrument_id.in_(ids))
                .group_by(Trade.instrument_id)
            ).all()
        )
        dividends = dict(
            everyone.execute(
                select(Dividend.instrument_id, func.count())
                .where(Dividend.instrument_id.in_(ids))
                .group_by(Dividend.instrument_id)
            ).all()
        )

    return {
        iid: {
            "mine": mine.get(iid, []),
            "total": totals.get(iid, 0) + dividends.get(iid, 0),
        }
        for iid in ids
    }


@app.get("/holdings", response_class=HTMLResponse)
def instruments_page(request: Request, error: str = "", added: str = "",
                     new: str = "1", removed: str = ""):
    with scoped(request) as (ctx, db):
        instruments = db.scalars(
            select(Instrument).order_by(Instrument.asset_class, Instrument.ticker)
        ).all()
        prefs = queries.prefs_by_instrument(db)
        held = {
            iid
            for iid in db.scalars(select(Trade.instrument_id).distinct()).all()
        }
        return _render(
            request,
            ctx,
            "instruments.html",
            {
                "active_nav": "holdings",
                "instruments": instruments,
                "prefs": prefs,
                "held": held,
                "blocking": _blocking_trades(db, instruments),
                # The latest stored close per instrument. Shown even for one
                # no longer held: a position you exited is still one you may be
                # watching, and the dashboard deliberately hides it.
                "prices": queries.latest_prices(db, [i.id for i in instruments]),
                "error": error,
                "added": added,
                "added_is_new": new != "0",
                "removed": removed,
            },
        )


@app.post("/holdings/{instrument_id}/remove")
async def instrument_remove(request: Request, instrument_id: int):
    """Stop tracking an instrument nobody has traded.

    **Deactivated, not deleted**, and the distinction is not squeamishness:
    the instrument list is shared, and its price history is shared with it.
    Deleting the row would take that history from anybody who later re-adds the
    same ticker. Setting `active = False` achieves what this is for — the feed
    stops fetching it and it leaves the lists — and is reversible by re-adding.
    """
    with scoped(request) as (ctx, db):
        await auth.verify_csrf(request, db)
        _require_write(ctx)
        inst = db.get(Instrument, instrument_id)
        if inst is None:
            raise HTTPException(404, "no such instrument")

        blocking = _blocking_trades(db, [inst]).get(instrument_id, {})
        if blocking.get("total"):
            # Refused rather than cascaded. Removing an instrument out from
            # under its own trades would leave a ledger referring to nothing.
            raise HTTPException(
                409,
                f"{inst.ticker} still has {blocking['total']} trade(s) or "
                f"dividend(s) recorded against it. Delete those first.",
            )
        inst.active = False
        log.info("instrument %s deactivated by user %d", inst.ticker, ctx.user.id)
        return _redirect("/holdings?removed=" + quote_plus(inst.ticker))


@app.get("/holdings/lookup")
def instruments_lookup(request: Request, ticker: str = "", exchange: str = "ASX"):
    """What Yahoo knows about a ticker — name, currency, price symbol.

    Backs the add-instrument forms so nobody has to know that ASX codes carry
    a .AX suffix or that NOVA prices in USD.
    """
    with scoped(request) as (ctx, _db):
        if not ticker.strip():
            raise HTTPException(400, "ticker is required")
        return pricefeed.lookup(ticker, exchange)


@app.post("/holdings/add")
async def instruments_add(
    request: Request,
    ticker: str = Form(...),
    name: str = Form(""),
    exchange: str = Form("ASX"),
    asset_class: str = Form("etf"),
    currency: str = Form("AUD"),
    yahoo_symbol: str = Form(""),
    drp: str = Form(""),
):
    with scoped(request) as (ctx, db):
        await auth.verify_csrf(request, db)
        _require_write(ctx)
        ticker = ticker.strip().upper()
        exchange = exchange.strip().upper()
        problem = ticker_problem(ticker) or exchange_problem(exchange)
        if problem:
            return _redirect(f"/holdings?error={quote_plus(problem)}")
        if asset_class not in ("etf", "share", "crypto"):
            return _redirect("/holdings?error=Unknown+asset+class")
        existing = db.scalar(
            select(Instrument).where(
                Instrument.exchange == exchange, Instrument.ticker == ticker
            )
        )
        was_new = existing is None
        if existing is None:
            # Anything left blank is filled from Yahoo; what was typed wins.
            found = pricefeed.lookup(ticker, exchange) if not (name.strip() and currency.strip()) else {}
            symbol = yahoo_symbol.strip() or found.get("symbol") or pricefeed.yahoo_symbol_for(ticker, exchange)
            existing = Instrument(
                ticker=ticker,
                exchange=exchange,
                name=(name.strip() or found.get("name") or None),
                currency=(currency.strip().upper() or found.get("currency") or "AUD"),
                asset_class=asset_class,
                drp=bool(drp),
                active=True,
                yahoo_symbol=symbol,
            )
            db.add(existing)
            db.flush()
        # Adding it to MY list is the per-user half: a pref row is what makes
        # the instrument mine to track (the catalogue entry itself is shared).
        pref = queries.prefs_by_instrument(db).get(existing.id)
        if pref is None:
            db.add(
                tenancy.owned(
                    db, HoldingPref(instrument_id=existing.id, drp=bool(drp))
                )
            )
        else:
            pref.drp = bool(drp)
        db.flush()
    _kick_feed()  # prices for a brand-new instrument arrive with the next run
    # Say which of the two things happened — the catalogue is shared, so
    # "added" is often "was already there, and is now on your list".
    return _redirect(f"/holdings?added={ticker}&new={'1' if was_new else '0'}")


@app.post("/holdings/{instrument_id}/pref")
async def instrument_pref(
    request: Request,
    instrument_id: int,
    drp: str = Form(""),
    note: str = Form(""),
    yahoo_symbol: str = Form(None),
):
    with scoped(request) as (ctx, db):
        await auth.verify_csrf(request, db)
        _require_write(ctx)
        inst = db.get(Instrument, instrument_id)
        if inst is None:
            raise HTTPException(404, "no such instrument")
        pref = queries.prefs_by_instrument(db).get(instrument_id)
        if pref is None:
            pref = tenancy.owned(db, HoldingPref(instrument_id=instrument_id))
            db.add(pref)
        pref.drp = bool(drp)
        pref.note = note.strip() or None
        # The Yahoo symbol is catalogue data (shared) — only set when supplied.
        if yahoo_symbol is not None and yahoo_symbol.strip():
            inst.yahoo_symbol = yahoo_symbol.strip()
        return _redirect(request.headers.get("referer") or "/holdings")


@app.get("/holding/{ticker}", response_class=HTMLResponse)
def instrument(request: Request, ticker: str, error: str = ""):
    with scoped(request) as (ctx, db):
        result = queries.ledger(db, ticker.upper())
        if result is None:
            raise HTTPException(404, f"unknown instrument {ticker!r}")
        inst, holding, events = result
        series = queries.instrument_series(db, inst)
        back_to = _safe_path(request.query_params.get("return"), "/")
        return _render(
            request,
            ctx,
            "instrument.html",
            {
                "active_nav": "holdings",
                "inst": inst,
                "holding": holding,
                "events": events,
                "series_json": series,
                "has_series": bool(series["dates"]),
                # Where the back button goes comes from `?return=`, not from
                # `Referer` or `history.back()` — decisions.md #40.
                "return_to": back_to,
                "back_label": _back_label(back_to, "Portfolio"),
                # A refused delete comes back here — the ledger is where you
                # need to be to act on what the message says.
                "error": error,
            },
        )

"""Every input the app takes, sent what its own forms never would.

Issues #53 and #60 were one bug in two shapes: a value the page's form could
not produce (NaN in a number box, 5000 characters in a 400-wide note) went
straight past the server into the database. SQLite stored it; Postgres refused
it, so the same request was a 500 and the person lost what they typed. The
fixes were per field, and the next field would have had the same gap.

So this test does not know the fields. It reads every route from the app, and
sends each field, one at a time, values a browser would never send, while every
other field holds what a careful person would type. After each request it asks:

1. **Did the app fall over?** An exception or a 5xx fails, whatever the value.
2. **Did anything land in the database that does not fit its column?** Text
   longer than its `String(n)`, a figure past its `Numeric(p, s)`, an integer
   Postgres's INTEGER cannot hold, NaN or infinity, a NUL character. SQLite
   takes all of these without a word, which is why this reads the rows back
   rather than trusting the status code: the same check fails on both backends.
3. **Did an HTML response echo markup unescaped?**

The careful values are themselves checked. Each route's are sent as a control
before the hostile ones and again after, and must be accepted both times. A
control refused first means the hostile values only ever met the first
validation; one refused last means the route shut its own door partway (the
first account closes the wizard), so the rest met that instead. Routes like
that get a fresh world per request: `REBUILD`.

A route is covered the moment it exists. One the walker cannot drive is in
`NOT_REACHED` with the reason. What it finds that nobody has fixed yet is in
`KNOWN`. Both fail when an entry goes stale, so they only ever shrink.

An identity provider's claims and Yahoo's lookup are input too, and get the
same treatment: `test_what_a_provider_says_fits_too`, and `YAHOO_SAYS`.
"""

from __future__ import annotations

import datetime as dt
import inspect
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import JSON, BigInteger, Integer, Numeric, String, select
from sqlalchemy.types import TypeDecorator
from test_api import bearer, issue_key
from test_pricefeed import stub_yf

import factories as fac
from app import auth as auth_mod
from app import configfile, invites, plans, statements, tenancy, theming
from app import main as main_mod
from app.models import (
    ApiKey,
    Base,
    ExternalIdentity,
    Instrument,
    Portfolio,
    PortfolioInvite,
    SavedChart,
    User,
    UserSession,
    WebauthnCredential,
)
from test_routes import PASSWORD, do_setup, make_login

HTML = {"accept": "text/html"}
MARKUP = '<img src=x onerror="alert(1)">'
FORMATS = main_mod.APP_DIR / "formats"

# What a browser's form cannot send, or a server should not believe if one did.
# Every field gets every one: a date in a note is harmless, and deciding in
# advance which fields "are numbers" is the assumption that let #53 through.
HOSTILE = {
    # #53: a number box cannot produce these, and Decimal() accepts all of them.
    "NaN": "NaN",
    "Infinity": "Infinity",
    "-Infinity": "-Infinity",
    "1E+999999": "1E+999999",
    # Past what a BIGINT holds, and past Python's own limit on turning a string
    # of digits into an int.
    "2**63": str(2**63),
    "5000 digits": "9" * 5000,
    # #60: a maxlength holds back a browser, not a POST.
    "5000 characters": "x" * 5000,
    # One character as typed, two once lowercased: fits a check made before
    # .lower() and overflows the column after it.
    "İ×300": "İ" * 300 + "@example.test",
    "blank": " ",
    # Postgres text cannot hold a NUL at all; SQLite stores it.
    "NUL": "a\x00b",
    "markup": MARKUP,
    "far future date": "9999-12-31",
    "impossible date": "2026-02-30",
}

# A JSON body can also send the wrong type, which a form cannot.
HOSTILE_JSON = {
    **HOSTILE,
    "number": 12345,
    "huge number": 1e308,
    "bare NaN": float("nan"),
    "true": True,
    "null": None,
    "list": [],
    "object": {},
}


# --------------------------------------------------------------------------- #
# The routes, read from the app
# --------------------------------------------------------------------------- #

def _all_routes() -> list[APIRoute]:
    """Every route, including those behind `include_router`, which leaves a
    placeholder in `app.routes` rather than its routes (see test_api_parity)."""
    found: list[APIRoute] = []

    def walk(router):
        for route in getattr(router, "routes", []):
            if isinstance(route, APIRoute):
                found.append(route)
            elif hasattr(route, "original_router"):
                walk(route.original_router)

    walk(main_mod.app)
    return found


@dataclass(frozen=True)
class Field:
    name: str
    where: str            # path, query, form, json, file


def _reads_body_by_hand(route: APIRoute) -> bool:
    source = inspect.getsource(route.endpoint)
    return "request.json()" in source or "request.form()" in source


# Routes that read their body themselves instead of declaring it, so FastAPI
# cannot say what they take. Each lists what its handler reads.
BY_HAND: dict[str, list[Field]] = {
    "/charts/save": [Field(n, "json") for n in ("name", "spec", "width", "id", "template_key")],
    # The whole body is the spec here, not a key in it.
    "/charts/preview": [Field(n, "json") for n in ("grain", "x", "measures", "type", "split")],
    "/charts/order": [Field("order", "json")],
    "/profile/passkeys": [Field("name", "form")],
    "/profile/columns": [Field("column", "form"), Field("back", "form")],
    "/profile/columns/order": [Field(n, "form") for n in ("order", "move", "back")],
    "/profile/appearance": [Field("nav_show", "form")],
    "/profile/colors": [Field(s.token, "form") for s in theming.SWATCHES],
    "/admin/settings": [Field(o.name, "form") for o in configfile.OPTIONS],
    "/setup/features": [Field("feature", "form")],
    "/imports-exports/csv/{uid}/resolve": [Field("ticker__ALPHA", "form")],
    "/imports-exports/broker/design": [Field(n, "form") for n in (
        "col_date", "col_action", "col_ticker", "col_units", "col_price")],
}


def _fields(route: APIRoute) -> list[Field]:
    out: list[Field] = []
    dependant = route.dependant
    out += [Field(p.name, "path") for p in dependant.path_params]
    out += [Field(p.name, "query") for p in dependant.query_params]
    for p in dependant.body_params:
        kind = type(p.field_info).__name__
        annotation = p.field_info.annotation
        if kind == "File":
            out.append(Field(p.name, "file"))
        elif kind == "Form":
            out.append(Field(p.name, "form"))
        elif hasattr(annotation, "model_fields"):            # a pydantic body
            out += [Field(n, "json") for n in annotation.model_fields]
        else:
            out.append(Field(p.name, "json"))
    return out + BY_HAND.get(route.path, [])


def _method(route: APIRoute) -> str:
    return sorted(route.methods - {"HEAD"})[0]


def _key(route: APIRoute) -> str:
    return f"{_method(route)} {route.path}"


def _takes_anything(route: APIRoute) -> bool:
    return _method(route) != "GET" or any(f.where != "file" for f in _fields(route))


ROUTES = [r for r in _all_routes() if _takes_anything(r)]


# --------------------------------------------------------------------------- #
# Who is asking
# --------------------------------------------------------------------------- #

# Asked before there is a session. Everything else is asked by a signed-in
# admin who owns the portfolio.
SIGNED_OUT = {"/login", "/login/recover", "/login/code", "/login/passkey",
              "/login/passkey/options", "/login/oidc", "/login/oidc/callback",
              "/oidc/backchannel-logout", "/invite/{token}", "/invite/{token}/signup"}
# The wizard's first steps run while there is no account at all; the rest run
# inside it, after the account step.
FIRST_RUN = {"/setup", "/setup/database", "/setup/database/test", "/setup/profile",
             "/setup/restarting"}
MID_WIZARD = {"/setup/2fa", "/setup/2fa/skip", "/setup/portfolio", "/setup/features",
              "/setup/environment", "/setup/finish"}
SCHEDULE = {"/schedule/complete", "/schedule/skip", "/schedule/delete"}

# What Yahoo answers while this runs: a provider is input too. Stubbed where
# yfinance would be, so `pricefeed.lookup` itself runs. Only the name is
# Yahoo's own (the symbol is ours, from the ticker), and a blank name on a
# form falls back to it, into an 80-character column.
YAHOO_SAYS = {"longName": "N" * 5000, "currency": "AUD", "regularMarketPrice": 1.5}

TODAY = dt.date.today()
WHEN = (TODAY - dt.timedelta(days=5)).isoformat()
# Long, but a valid address that fits the email column: a name that falls back
# to it does not fit the name column.
LONG_EMAIL = "a" * 290 + "@example.test"

# The test broker in tests/config.test.yaml, and one of the shipped statements.
def _broker_csv(units: int = 10) -> str:
    """One trade. The units vary, so an upload after a commit is not a
    duplicate with nothing left to stage."""
    return ("Trade Date,Buy/Sell,Code,Units,Price,Brokerage\n"
            f"{(TODAY - dt.timedelta(days=5)):%d/%m/%Y},Buy,ALPHA,{units},1.50,9.50\n")


BROKER_CSV = _broker_csv()
STATEMENT = (FORMATS / "samples" / "generic-au.txt").read_text()
STATEMENT_TEMPLATE = (FORMATS / "statements" / "generic-au.yaml").read_text()
BROKER_TEMPLATE = (FORMATS / "brokers" / "commsec.yaml").read_text()

FILES = {
    "/imports-exports/csv": ("trades.csv", BROKER_CSV.encode(), "text/csv"),
    "/imports-exports/statement": ("statement.txt", STATEMENT.encode(), "text/plain"),
    "/imports-exports/broker/design": ("trades.csv", BROKER_CSV.encode(), "text/csv"),
}


@dataclass
class World:
    client: object
    session_factory: object
    kind: str
    user_id: int | None = None
    portfolio_id: int | None = None
    other_portfolio_id: int | None = None
    instrument_id: int | None = None
    api_key: str | None = None
    n: int = 0
    extra: dict = field(default_factory=dict)

    def bound(self):
        session = self.session_factory()
        tenancy.bind(session, self.portfolio_id, self.user_id)
        return session

    def tick(self) -> int:
        self.n += 1
        return self.n

    def csrf(self) -> str | None:
        """This client's own token: its session's when signed in, else the
        pre-auth cookie. Read per request, because some routes rotate it."""
        settings = main_mod.settings
        raw = self.client.cookies.get(settings.auth.cookie_name)
        if raw:
            with self.session_factory() as s:
                row = auth_mod.load_session(s, raw, settings)
                if row is not None:
                    return row.csrf_token
        return self.client.cookies.get(auth_mod.PRE_AUTH_CSRF_COOKIE)


def _signed_in(client, session_factory) -> World:
    make_login(client, session_factory)
    world = World(client, session_factory, "signed in")
    with session_factory() as s:
        user = s.scalars(select(User)).one()
        portfolio = s.scalars(select(Portfolio)).one()
        world.user_id, world.portfolio_id = user.id, portfolio.id
        world.other_portfolio_id = fac.make_portfolio(s, "Second portfolio", owner=user).id
        s.commit()
    with world.bound() as s:
        acme = fac.make_instrument(s, "ACME", asset_class="share")
        fac.make_instrument(s, "ALPHA", asset_class="share")
        fac.add_prices(s, acme, [(TODAY - dt.timedelta(days=d), "1.50") for d in range(40)])
        fac.add_trade(s, acme, TODAY - dt.timedelta(days=30), "buy", 100, "1.00")
        world.instrument_id = acme.id
        s.commit()
    world.api_key = issue_key(session_factory, world.portfolio_id, scopes="read,write",
                              created_by=world.user_id)
    return world


def _signed_out(client, session_factory) -> World:
    world = _signed_in(client, session_factory)
    client.cookies.clear()
    client.get("/login", headers=HTML)                 # the pre-auth cookie
    world.kind = "signed out"
    return world


def _first_run(client, session_factory) -> World:
    client.get("/setup", headers=HTML)
    token = client.cookies.get(auth_mod.PRE_AUTH_CSRF_COOKIE)
    client.post("/setup", data={"_csrf": token}, headers=HTML, follow_redirects=False)
    return World(client, session_factory, "first run")


def _mid_wizard(client, session_factory) -> World:
    do_setup(client)
    world = World(client, session_factory, "mid wizard")
    with session_factory() as s:
        world.user_id = s.scalars(select(User)).one().id
    return world


def _world(route: APIRoute, client, session_factory) -> World:
    if route.path in FIRST_RUN:
        return _first_run(client, session_factory)
    if route.path in MID_WIZARD:
        return _mid_wizard(client, session_factory)
    if route.path in SIGNED_OUT:
        return _signed_out(client, session_factory)
    world = _signed_in(client, session_factory)
    if route.path in SCHEDULE:
        client.post("/schedule/save", headers=HTML, follow_redirects=False, data={
            "name": "Regular buys", "interval_days": "28", "amount": "500",
            "brokerage": "9.50", "start_date": WHEN, "tickers": "ACME",
            "_csrf": world.csrf()})
    return world


# --------------------------------------------------------------------------- #
# What a careful person would type
# --------------------------------------------------------------------------- #

# A path parameter names something that has to exist. Each request gets a new
# one, so a route that uses it up (a delete, an invite spent) still has one for
# the next hostile value instead of answering 404 to all of them.
def _fresh(world: World, name: str) -> str:
    n = world.tick()
    if name == "key_id":
        issue_key(world.session_factory, world.portfolio_id, created_by=world.user_id)
        with world.session_factory() as s:
            return str(s.scalars(select(ApiKey.id).order_by(ApiKey.id.desc())).first())
    if name == "uid":
        resp = world.client.post(
            "/imports-exports/csv", headers=HTML, follow_redirects=False,
            data={"broker": "testbroker", "_csrf": world.csrf()},
            files={"file": ("trades.csv", _broker_csv(n).encode(), "text/csv")})
        return re.search(r"/imports-exports/csv/([\w-]+)/", resp.text).group(1)
    if name == "slug":
        folder = main_mod.settings.imports.user_dir("statement")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"installed-{n}.yaml").write_text(STATEMENT_TEMPLATE)
        return f"installed-{n}"
    with world.bound() as s:
        acme = s.get(Instrument, world.instrument_id) if world.instrument_id else None
        if name == "trade_id":
            made = fac.add_trade(s, acme, TODAY - dt.timedelta(days=20), "buy", 5, "1.00")
        elif name == "dividend_id":
            made = fac.add_dividend(s, acme, TODAY - dt.timedelta(days=10), "3.00")
        elif name == "instrument_id":
            made = fac.make_instrument(s, f"FRESH{n}", asset_class="share")
        elif name == "chart_id":
            made = tenancy.owned(s, SavedChart(name=f"Chart {n}", spec={}, position=n))
            s.add(made)
        elif name in ("invite_id", "token"):
            raw = invites.create(s, portfolio_id=world.portfolio_id, role="member",
                                 created_by=world.user_id)
            s.commit()
            made = s.scalars(select(PortfolioInvite).order_by(PortfolioInvite.id.desc())).first()
            return raw if name == "token" else str(made.id)
        elif name == "user_id":
            made = fac.make_user(s, f"someone{n}@example.test")
        elif name == "member_id":
            person = fac.make_user(s, f"member{n}@example.test")
            made = fac.add_member(s, s.get(Portfolio, world.portfolio_id), person)
        elif name == "session_id":
            auth_mod.create_session(s, s.get(User, world.user_id), main_mod.settings)
            s.commit()
            return str(s.scalars(select(UserSession.id).order_by(UserSession.id.desc())).first())
        elif name == "credential_id":
            made = WebauthnCredential(user_id=world.user_id, credential_id=f"cred{n}",
                                      public_key="x", sign_count=0, rp_id="testserver")
            s.add(made)
        elif name == "identity_id":
            made = ExternalIdentity(user_id=world.user_id, issuer="https://idp.test",
                                    subject=f"subject{n}")
            s.add(made)
        else:
            return {"ticker": "ACME", "year": str(TODAY.year), "kind": "statement",
                    "index": "0"}.get(name, "1")
        s.flush()
        s.commit()
        return str(made.id)


def _typed(world: World, route: APIRoute, n: int, made: dict) -> dict:
    """The control values. A unique suffix where the app insists on one (an
    email, a new ticker), so a hostile value that is rightly accepted does not
    turn every later request into a duplicate."""
    values = {
        "name": "Careful name", "email": f"{n}{LONG_EMAIL}",
        "password": "a-new-long-password", "confirm": "a-new-long-password",
        "current_password": PASSWORD,
        "ticker": "ACME", "tickers": "ACME", "instrument_id": str(world.instrument_id),
        "instrument": str(world.instrument_id),
        "type": "buy", "trade_date": WHEN, "div_date": WHEN, "payment_date": WHEN,
        "date": WHEN, "due_date": WHEN, "start_date": WHEN,
        "quantity": "10", "units": "10", "unit_price": "1.50", "cash_amount": "12.34",
        "net_amount": "12.34", "amount": "500", "brokerage": "0", "interval_days": "28",
        "note": "A careful note", "yahoo_symbol": "ACME.AX", "currency": "AUD",
        "exchange": "ASX", "asset_class": "share",
        "new_ticker": f"NEW{n}", "new_name": "New company", "new_yahoo": f"NEW{n}.AX",
        "new_asset_class": "share",
        "role": "member", "scopes": "read", "portfolios": [str(world.portfolio_id)],
        "portfolio_id": str(world.other_portfolio_id), "target": str(world.other_portfolio_id),
        "timezone": "Australia/Sydney", "reporting_currency": "AUD", "jurisdiction": "AU",
        "theme": "auto", "nav_style": "both", "times_in": "market",
        "spec": {"grain": "positions", "x": "ticker", "measures": ["pos_value"], "type": "bar"},
        "width": "half", "order": [],
        # The importers: the test broker's CSV, and a shipped statement.
        "broker": "testbroker", "text": STATEMENT, "label": "net_amount", "index": "0",
        "mapping": json.dumps({"net_amount": {"after": ["Net Payment"], "type": "money"}}),
        "body": STATEMENT_TEMPLATE, "filename": f"careful-{n}",
        "ticker__ALPHA": "ALPHA",
    }
    values.update(made)
    path = route.path
    if path == "/login":
        values.update(email="user@example.test", password=PASSWORD)
    elif path == "/members/add":
        with world.session_factory() as s:
            values["user_id"] = str(fac.make_user(s, f"outsider{n}@example.test").id)
            s.commit()
    elif path == "/holdings/add":
        values["ticker"] = f"ADD{n}"
    elif path == "/api/v1/dividends":
        values["cash_amount"] = f"{10 + n}.00"      # the same dividend twice is a 409
    elif path == "/trade/{trade_id}/move":
        values["row"] = [f"trade:{made['trade_id']}"]
    elif path == "/imports-exports/statement/commit":
        values["net_amount"] = f"{10 + n}.00"       # the same dividend twice is refused
    elif path == "/charts/preview":
        values.update(values["spec"])
    elif path == "/imports-exports/statement/design/check":
        values["type"] = "text"
    elif path == "/imports-exports/broker/design/export":
        values["body"] = BROKER_TEMPLATE
    elif path == "/imports-exports/broker/design":
        values.update(text=BROKER_CSV, col_date="Trade Date", col_action="Buy/Sell",
                      col_ticker="Code", col_units="Units", col_price="Price")
    elif path in ("/schedule/complete", "/schedule/skip"):
        with world.bound() as s:
            upcoming = plans.next_buy(s)
        if upcoming is not None:
            values.update(ticker=upcoming.ticker, due_date=upcoming.due_date.isoformat())
    return values


# A route asked in more than one way, because it branches on a field. The
# hostile values are sent once per way, so each branch meets them.
WAYS = {
    "/trade/new": [{}, {"instrument_id": "new"}],
}

# The control is refused on these for a reason a form cannot type its way past,
# so the hostile values there only meet the first checks. Each says why.
NOT_REACHED: dict[str, str] = {
    "POST /login/code": "needs a sign-in waiting on an authenticator code",
    "POST /login/recover": "a recovery code works once; every hostile email still "
                           "reaches the sign-in record, which is the write that matters",
    "POST /setup/2fa": "needs a code from the authenticator being enrolled",
    "POST /profile/2fa/enable": "needs a code from the authenticator being enrolled",
    "POST /profile/passkeys/options": "passkeys are off without auth.webauthn",
    "POST /profile/passkeys": "a passkey ceremony needs an authenticator's signature",
    "POST /login/passkey/options": "passkeys are off without auth.webauthn",
    "POST /login/passkey": "a passkey ceremony needs an authenticator's signature",
    "GET /login/oidc": "single sign-on is off; the claims a provider sends are "
                       "fuzzed in test_what_a_provider_says_fits_too",
    "GET /login/oidc/callback": "the same: needs a provider's signed answer",
    "POST /oidc/backchannel-logout": "needs a provider's signed logout token",
    "POST /profile/oidc/link": "single sign-on is off",
    "POST /setup/database": "chooses the app's own database; test_setup_wizard "
                            "drives it with the database stubbed",
    "POST /setup/database/test": "connects to the database it is given",
    "POST /imports-exports/statement/visual": "OCR: needs tesseract and a PDF",
    "POST /imports-exports/statement/visual/edit": "needs an OCR'd statement staged",
    "GET /imports-exports/statement/visual/{token}/page/{index}.png":
        "needs an OCR'd statement staged",
}

# Where a control legitimately lands somewhere `_accepted` would otherwise read
# as a refusal.
LANDS = {"/logout": "/login", "/members/{member_id}/remove": "/"}

# Routes whose own success shuts the door on the next request: the first
# account closes the wizard, a changed password is no longer the current one.
# Each request on these gets a world of its own. `test_..._breaks_the_app...`
# finds new ones by sending the control again at the end.
REBUILD = {"/setup/profile", "/setup/finish", "/profile/password", "/schedule/delete",
           "/invite/{token}/signup", "/login"}


def _wipe(session_factory) -> None:
    """Every row gone, as if the database had just been created."""
    from app import setupwizard

    setupwizard.discard()
    with session_factory() as s:
        for table in reversed(Base.metadata.sorted_tables):
            if table.name != "alembic_version":
                s.execute(table.delete())
        s.commit()


# --------------------------------------------------------------------------- #
# What it finds today
# --------------------------------------------------------------------------- #

# Every finding as (route, field, what), each one waiting on a fix. A finding
# not listed fails, and so does a listed one that has stopped happening: fix
# something and its lines here have to go, so this list only ever shrinks.
KNOWN: set[tuple[str, str, str]] = set()      # nothing outstanding
# Findings only Postgres shows, such as a lookup SQLite answers and Postgres
# refuses. Checked for staleness on Postgres only.
POSTGRES_ONLY: set[tuple[str, str, str]] = set()

POSTGRES = main_mod.settings.database.type == "postgres"


def _cause(message: str) -> str:
    if "NUL" in message:
        return "NUL"
    if "characters in a column" in message or "value too long" in message:
        return "too long"
    if re.search(r"past a (BIG)?INT|past Numeric|integer out of range|"
                 r"too large to convert|C long", message):
        return "too big"
    return "crash"


def _ratchet(case: str, problems: list[tuple[str, str]]) -> None:
    known = {(f, c) for r, f, c in KNOWN
             if r == case and (POSTGRES or (r, f, c) not in POSTGRES_ONLY)}
    new = [f"{label}: {message}" for label, message in problems
           if (label.split("=")[0], _cause(message)) not in known]
    found = {(label.split("=")[0], _cause(message)) for label, message in problems}
    assert not new, "\n".join(new)
    fixed = sorted(known - found)
    assert not fixed, f"no longer happens, so delete it from KNOWN: {fixed}"


# --------------------------------------------------------------------------- #
# The checks
# --------------------------------------------------------------------------- #

def _bounds(column):
    """The type that sets the limit: a TypeDecorator's underlying one."""
    kind = column.type
    return kind.impl_instance if isinstance(kind, TypeDecorator) else kind


def _misfit(kind, value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        if "\x00" in value:
            return "a NUL character"
        length = getattr(kind, "length", None)
        if isinstance(kind, String) and length and len(value) > length:
            return f"{len(value)} characters in a column of {length}"
        return None
    if isinstance(kind, Numeric) and isinstance(value, (Decimal, float)):
        figure = Decimal(str(value)) if isinstance(value, float) else value
        if not figure.is_finite():
            return f"{figure} in a NUMERIC"
        if kind.precision is not None and kind.scale is not None:
            if abs(figure) >= Decimal(10) ** (kind.precision - kind.scale):
                return f"{figure} past Numeric({kind.precision}, {kind.scale})"
        return None
    if isinstance(kind, Integer) and isinstance(value, int) and not isinstance(value, bool):
        top = 2**63 - 1 if isinstance(kind, BigInteger) else 2**31 - 1
        if abs(value) > top:
            return f"{value} past a {'BIGINT' if top > 2**31 else 'INTEGER'}"
        return None
    if isinstance(kind, JSON):
        try:
            json.dumps(value, allow_nan=False)
        except ValueError:
            return "NaN or infinity in JSON"
    return None


def _misfits(session_factory) -> set[str]:
    """Every stored value that does not fit its column, keyed by row."""
    out: set[str] = set()
    with session_factory() as s:
        conn = s.connection()          # Core, under the tenancy filter: every row
        for table in Base.metadata.sorted_tables:
            keys = list(table.primary_key.columns)
            columns = [c for c in table.columns if c not in keys]
            try:
                rows = conn.execute(select(*keys, *columns)).all()
            except Exception as exc:          # a value that cannot even be read back
                out.add(f"{table.name}: unreadable ({type(exc).__name__}: {exc})")
                continue
            for row in rows:
                ident = ",".join(str(v) for v in row[:len(keys)])
                for column, value in zip(columns, row[len(keys):], strict=True):
                    why = _misfit(_bounds(column), value)
                    if why:
                        out.add(f"{table.name}.{column.name}[{ident}]: {why}")
    return out


def _accepted(route: APIRoute, resp) -> bool:
    if resp.status_code >= 400:
        return False
    location = resp.headers.get("location", "")
    if location and location == LANDS.get(route.path):
        return True
    if "error=" in location or location.startswith("/login"):
        return False
    return 'class="panel error"' not in resp.text and 'class="error"' not in resp.text


def _send(world: World, route: APIRoute, fields: list[Field], values: dict):
    path = route.path
    for f in fields:
        if f.where == "path":
            path = path.replace("{" + f.name + "}", quote(str(values[f.name]), safe=""))
    query = {f.name: values[f.name] for f in fields if f.where == "query" and f.name in values}
    form = {f.name: values[f.name] for f in fields if f.where == "form" and f.name in values}
    body = {f.name: values[f.name] for f in fields if f.where == "json" and f.name in values}
    headers = dict(HTML)
    token = world.csrf()
    if token:
        headers["x-csrf-token"] = token
    if route.path.startswith("/api/"):
        headers.update(bearer(world.api_key))
    method = _method(route)
    kwargs = {"params": query, "headers": headers, "follow_redirects": False}
    if method == "GET":
        pass
    elif any(f.where == "json" for f in fields):
        # Encoded here rather than by httpx, which refuses a bare NaN: a client
        # that is not httpx sends one happily, and json.loads accepts it.
        kwargs["content"] = json.dumps(body)
        headers["content-type"] = "application/json"
    else:
        if token:
            form["_csrf"] = token
        kwargs["data"] = form
        if any(f.where == "file" for f in fields) and route.path in FILES:
            kwargs["files"] = {"file": FILES[route.path]}
    return world.client.request(method, path, **kwargs)


def _failure(exc: BaseException) -> str:
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    first = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    return f"{type(exc).__name__}: {first[:160]}"


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    """Everything outside the database a route can touch, pointed somewhere
    harmless, and Yahoo answering with `YAHOO_SAYS`.

    The restart is the one that matters most: the real `request_stop` sends
    SIGTERM to this process, which ends the whole test session.
    """
    from app import imports_web, lifecycle

    stub_yf(monkeypatch, info=dict(YAHOO_SAYS))
    # The statement upload reads its file as text, as the statement tests do:
    # the careful file is a shipped sample, and turning it into a PDF would
    # only test pdfplumber.
    monkeypatch.setattr(statements, "_pdf_text", lambda data: data.decode("utf-8", "replace"))
    monkeypatch.setattr(lifecycle, "request_stop", lambda reason: None)
    real = Path(main_mod.APP_DIR.parent, "tests", "config.test.yaml")
    original = real.read_text()
    config = tmp_path / "config.yaml"
    config.write_text(original)
    monkeypatch.setattr(configfile, "CONFIG_FILE", str(config))
    monkeypatch.setattr(main_mod.settings.imports, "templates_dir", str(tmp_path / "formats"))
    monkeypatch.setattr(imports_web, "STAGING", tmp_path / "staged")
    yield
    # Before this fixture existed, the settings page wrote straight into the
    # suite's own config. Anything new that writes it would do the same.
    assert real.read_text() == original, "a route wrote to tests/config.test.yaml"


class Probe:
    """Sends one request and records whatever it broke."""

    def __init__(self, session_factory):
        self.session_factory = session_factory
        self.problems: list[tuple[str, str]] = []
        self.seen = _misfits(session_factory)

    def __call__(self, label: str, send):
        live = main_mod.settings
        before = live.model_copy(deep=True)
        resp = None
        try:
            resp = send()
        except Exception as exc:          # what a 500 is, under TestClient
            self.problems.append((label, _failure(exc)))
        finally:
            # A setting a hostile request managed to save would otherwise be
            # blamed on whichever request it broke next.
            for name in type(live).model_fields:
                setattr(live, name, getattr(before, name))
        if resp is not None:
            if resp.status_code >= 500:
                self.problems.append((label, f"HTTP {resp.status_code}"))
            if "text/html" in resp.headers.get("content-type", "") and MARKUP in resp.text:
                self.problems.append((label, "the markup came back unescaped"))
        now = _misfits(self.session_factory)
        self.problems.extend((label, f"stored {m}") for m in sorted(now - self.seen))
        self.seen = now
        return resp


# --------------------------------------------------------------------------- #
# The tests
# --------------------------------------------------------------------------- #

@pytest.mark.hostile
@pytest.mark.parametrize("route", [pytest.param(r, id=_key(r)) for r in ROUTES])
def test_nothing_a_form_cannot_send_breaks_the_app_or_the_database(
        route, client, session_factory, sandbox):
    world = _world(route, client, session_factory)
    fields = _fields(route)
    probe = Probe(session_factory)

    def values_for(way: dict) -> dict:
        nonlocal world
        if route.path in REBUILD:
            _wipe(session_factory)
            client.cookies.clear()
            world = _world(route, client, session_factory)
            probe.seen = _misfits(session_factory)
        made = {f.name: _fresh(world, f.name) for f in fields if f.where == "path"}
        return {**_typed(world, route, world.tick(), made), **way}

    def control(label: str, way: dict):
        values = values_for(way)
        resp = probe(label, lambda: _send(world, route, fields, values))
        return resp is not None and _accepted(route, resp), resp

    refused, drifted = [], []
    for way in WAYS.get(route.path, [{}]):
        # The control first: a careful request has to get through, or the
        # hostile ones only ever met the first check.
        ok, resp = control(f"control {way or ''}".strip(), way)
        if not ok and resp is not None:
            where = resp.headers.get("location", "")
            refused.append(f"{way or 'control'}: {resp.status_code} {where}".strip())
        for f in fields:
            if f.where == "file":
                continue
            for label, value in (HOSTILE_JSON if f.where == "json" else HOSTILE).items():
                values = values_for(way)
                values[f.name] = [value] if isinstance(values.get(f.name), list) else value
                if f.name == "password":
                    values["confirm"] = value
                probe(f"{f.name}={label}", lambda: _send(world, route, fields, values))
        # And again at the end. Accepted first and refused now means the
        # route closed its own door, so most of the above met that refusal
        # rather than the check it was aimed at.
        if ok and not control(f"closing control {way or ''}".strip(), way)[0]:
            drifted.append(str(way or "control"))

    _ratchet(_key(route), probe.problems)
    if _key(route) not in NOT_REACHED:
        assert not refused, (
            f"the careful values were refused ({'; '.join(refused)}): fix `_typed`, "
            "or list the route in NOT_REACHED saying why")
    assert not drifted, (
        f"accepted the control first and refused it last ({', '.join(drifted)}): "
        "the route changes what its next request needs. Add it to REBUILD.")


ISSUER, CLIENT_ID = "https://idp.example.test", "stocktake"
# The claims the app keeps: a new account's name and email, the link's subject
# and email, and the provider's session id, kept for back-channel logout.
CLAIMS = ("name", "email", "sub", "sid")


@pytest.mark.hostile
@pytest.mark.parametrize("returning", [False, True], ids=["a new account", "a returning identity"])
@pytest.mark.parametrize("claim", CLAIMS)
def test_what_a_provider_says_fits_too(
        claim, returning, client, session_factory, sandbox, monkeypatch):
    """An identity provider's claims reach the database as a form's fields do:
    a name and an email for a new account, a subject for the link, and a fresh
    email every time a known identity signs in again. Nobody typed them, so
    they are the app's to fit, not to refuse (decisions.md #131)."""
    from app.models import OidcState
    from appcore.testing import FakeIdp

    idp = FakeIdp(issuer=ISSUER, client_id=CLIENT_ID).install(monkeypatch)
    conf = main_mod.settings.auth.oidc           # put back by _restore_settings
    conf.enabled, conf.issuer, conf.client_id = True, ISSUER, CLIENT_ID
    conf.client_secret = "a-secret"
    conf.redirect_uri = "https://stocktake.example.test/login/oidc/callback"
    conf.provisioning = "open"
    make_login(client, session_factory)
    client.cookies.clear()
    probe = Probe(session_factory)

    def sign_in(claims: dict):
        start = client.get("/login/oidc", follow_redirects=False)
        state = re.search(r"[?&]state=([^&]+)", start.headers["location"]).group(1)
        with session_factory() as s:
            nonce = s.scalars(select(OidcState).order_by(OidcState.id.desc())).first().nonce
        idp.claims = {"nonce": nonce, **claims}
        try:
            return client.get(f"/login/oidc/callback?code=abc&state={state}",
                              follow_redirects=False)
        finally:
            client.cookies.clear()

    if returning:
        probe("first sign-in", lambda: sign_in({"sub": "returning", "email": "back@example.test"}))
    # Strings only. A claim of the wrong type is a provider that does not follow
    # the spec, and checking types is the token parser's job, in appcore.
    for n, (label, value) in enumerate(HOSTILE.items()):
        claims = {"sub": "returning" if returning else f"subject-{n}",
                  "email": f"member{n}@example.test"}
        claims[claim] = value
        probe(f"{claim}={label}", lambda: sign_in(claims))

    _ratchet(f"provider {claim}-{'a returning identity' if returning else 'a new account'}",
             probe.problems)


def test_every_route_that_takes_input_is_walked():
    """The inventory. A new route is walked from the day it exists; this keeps
    the exceptions honest."""
    keys = {_key(r) for r in ROUTES}
    stale = sorted(set(NOT_REACHED) - keys)
    assert not stale, f"NOT_REACHED names routes that no longer exist: {stale}"
    cases = keys | {f"provider {c}-{w}" for c in CLAIMS
                    for w in ("a new account", "a returning identity")}
    stale = sorted({r for r, _, _ in KNOWN} - cases)
    assert not stale, f"KNOWN names cases that no longer exist: {stale}"
    stale = sorted(set(BY_HAND) - {r.path for r in ROUTES})
    assert not stale, f"BY_HAND names routes that no longer exist: {stale}"
    undeclared = sorted(r.path for r in ROUTES
                        if _reads_body_by_hand(r) and r.path not in BY_HAND
                        and _key(r) not in NOT_REACHED)
    assert not undeclared, (
        f"these read their body by hand, so FastAPI cannot list their fields: "
        f"{undeclared}. Add them to BY_HAND.")

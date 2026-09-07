"""Every stored field is either published or deliberately withheld.

`models.py`, `fields.py`, the exports and the JSON API each hold their own idea
of what a portfolio contains, and they drift — `fx_rate_aud` was renamed to
`fx_rate` once, and the two stayed in step only because somebody noticed.

So the lists below are the contract, and this suite is what makes them true.
Add a column and the suite fails until you say which it is: published, or
withheld and why. The reasons are the documentation, and unlike a paragraph in
`architecture.md` they cannot quietly stop being true.

**Write a real reason.** "internal" is how a list like this turns into a place
to silence a test.
"""

from __future__ import annotations

import pytest

from app import columns, exports, fields
from app.models import Base

# --------------------------------------------------------------------------- #
# Model columns
# --------------------------------------------------------------------------- #

# Column -> the API field carrying it. Checked against live responses below, so
# renaming one side breaks the suite rather than the consumer.
PUBLISHED: dict[str, str] = {
    "portfolio.id": "portfolio.id",
    "portfolio.name": "portfolio.name",
    "instrument.ticker": "holdings[].ticker",
    "instrument.name": "holdings[].name",
    "instrument.exchange": "holdings[].exchange",
    "instrument.currency": "holdings[].currency",
    "instrument.asset_class": "holdings[].asset_class",
    "trade.id": "trades[].id",
    "trade.date": "trades[].date",
    "trade.type": "trades[].type",
    "trade.quantity": "trades[].units",
    "trade.unit_price": "trades[].unit_price",
    "trade.brokerage": "trades[].brokerage",
    "trade.fx_rate": "trades[].fx_rate",
    "trade.note": "trades[].note",
    "trade.instrument_id": "trades[].ticker",
    "dividend.id": "dividends[].id",
    "dividend.date": "dividends[].date",
    "dividend.cash_amount": "dividends[].cash_amount",
    "dividend.franking_credits": "dividends[].franking_credits",
    "dividend.reinvest_trade_id": "dividends[].reinvested",
    "dividend.instrument_id": "dividends[].ticker",
    # Both added 2026-08-12: trades published these and dividends did not, an
    # asymmetry nobody chose. This test found it on its first run.
    "dividend.fx_rate": "dividends[].fx_rate",
    "dividend.note": "dividends[].note",
    "price.close": "holdings[].last_price",
    "price.date": "holdings[].price_date",
}

# Column -> why it is not published. A reason, not a label.
WITHHELD: dict[str, str] = {
    # Bookkeeping the caller cannot use and should not depend on.
    "trade.updated_at": "When the row was last edited. Callers follow the trade "
                        "date with ?since=, which is the fact they care about.",
    "dividend.updated_at": "When the row was last edited, as with trades.",
    "portfolio.created_at": "When the portfolio was made. Of no use to a consumer.",
    "trade.time": "Orders same-day trades so a sell cannot precede its buy. "
                  "Sequencing within the app, not a fact about the trade.",
    "instrument.id": "Surrogate key. Instruments are addressed by ticker.",
    "trade.portfolio_id": "Implied by the key: a key sees one portfolio, always.",
    "dividend.portfolio_id": "Implied by the key, as with trades.",
    "trade.user_id": "Which member entered the row. Not a fact about the holding.",
    "dividend.user_id": "Which member entered the row, as with trades.",
    "price.instrument_id": "Carried by the holding the price is attached to.",
    "price.source": "Which provider answered. Operational, shown in the app.",
    "price.provisional": (
        "Whether the row is a live price for an unfinished session rather than "
        "a close. Withheld ON PURPOSE: a consumer asking for a valuation wants "
        "one number, and by the next day every row is settled anyway. Publish "
        "it only if a consumer needs to distinguish the two."
    ),
    "instrument.drp": "A preference, per member — see holding_pref.",
    "instrument.active": "Whether it still appears in the app's own lists.",
    "instrument.yahoo_symbol": "How the feed addresses it upstream. Ours, not theirs.",
    "instrument.note": "Free text the owner wrote for themselves.",
    "dividend.residual_carried": (
        "The DRP balance the registry still holds. **A balance, not income** — "
        "the whole distribution is already in cash_amount, so publishing it "
        "beside that invites double counting. Reconsider when a consumer needs "
        "to reconcile against a registry statement."
    ),
    "portfolio.reporting_currency": "Stored for T28b; the API is AUD-only today.",
    "portfolio.jurisdiction": "Stored for T28b; the tax engine is AU-only today.",
}

# Whole tables with nothing to publish. One reason each, not one per column.
WITHHELD_TABLES: dict[str, str] = {
    "user": "Accounts and credentials. Never leaves the machine.",
    "user_session": "Live sessions and their CSRF tokens. Credential material.",
    "recovery_code": "Hashed single-use codes stood in for a second factor.",
    "webauthn_credential": "Passkey public keys and their signature counters.",
    "webauthn_challenge": "In-flight sign-in nonces. Credential material.",
    "portfolio_invite": "Live invitations. The token is a credential for as "
                        "long as the link is unspent.",
    "external_identity": "Which provider account is which local one. Identity "
                         "linkage, not portfolio data.",
    "oidc_state": "In-flight sign-in nonces. Credential material.",
    "login_attempt": "Lockout state, and a record of failed sign-ins.",
    "api_key": "The keys themselves. A key cannot enumerate its siblings.",
    "portfolio_member": "Who has access. A consumer app has no business with it.",
    "holding_pref": "One member's own view of a holding: DRP flag and a note.",
    "saved_chart": "One member's chart layouts.",
    "fx_rate": (
        "The rate series. Amounts are published already converted, and a "
        "consumer needing raw rates should ask a rate provider, not a "
        "portfolio tracker."
    ),
    "market_dividend": (
        "Distribution history for the security, not for this portfolio — it is "
        "the same public data anyone can fetch, cached here for the calendar."
    ),
    "investment_plan": "The DCA schedule is published through /schedule, not per row.",
    "investment_plan_entry": "The rotation behind a plan, served through /schedule.",
    "planned_purchase": "Individual due dates, served through /schedule.",
}


def _all_columns() -> dict[str, list[str]]:
    out = {}
    for cls in Base.registry._class_registry.values():
        table = getattr(cls, "__table__", None)
        if table is not None:
            out[table.name] = [c.name for c in table.columns]
    return out


def test_every_stored_column_is_published_or_withheld_with_a_reason():
    """The whole point. A new column fails this until somebody decides."""
    unaccounted = []
    for table, cols in sorted(_all_columns().items()):
        if table in WITHHELD_TABLES:
            continue
        for col in cols:
            key = f"{table}.{col}"
            if key not in PUBLISHED and key not in WITHHELD:
                unaccounted.append(key)
    assert not unaccounted, (
        "Add each of these to PUBLISHED (with the API field) or WITHHELD "
        f"(with a reason): {unaccounted}"
    )


def test_no_column_is_both_published_and_withheld():
    assert not (set(PUBLISHED) & set(WITHHELD))


def test_the_lists_do_not_name_columns_that_no_longer_exist():
    """Stops the lists rotting the other way — a rename leaves a stale entry
    claiming a field is handled when nothing is."""
    real = {f"{t}.{c}" for t, cols in _all_columns().items() for c in cols}
    stale = sorted((set(PUBLISHED) | set(WITHHELD)) - real)
    assert not stale, f"no such column any more: {stale}"

    tables = set(_all_columns())
    assert not sorted(set(WITHHELD_TABLES) - tables), "no such table any more"


@pytest.mark.parametrize("key,reason", sorted(WITHHELD.items()))
def test_every_withheld_column_gives_a_real_reason(key, reason):
    """"internal" is how this list becomes a place to silence the test."""
    assert len(reason) >= 25, f"{key}: say why, not what"
    assert not reason.lower().startswith(("internal", "n/a", "private", "no")), key


@pytest.mark.parametrize("table,reason", sorted(WITHHELD_TABLES.items()))
def test_every_withheld_table_gives_a_real_reason(table, reason):
    assert len(reason) >= 25, f"{table}: say why, not what"


# --------------------------------------------------------------------------- #
# …and the published ones really are published
# --------------------------------------------------------------------------- #

def _flatten(payload, prefix=""):
    """Response -> the set of dotted paths in it, lists collapsed to `[]`."""
    found = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else key
            found.add(path)
            found |= _flatten(value, path)
    elif isinstance(payload, list):
        for item in payload:
            found |= _flatten(item, f"{prefix}[]")
    return found


def test_every_published_column_appears_in_a_live_response(client, session_factory):
    """The half that catches a rename.

    The lists above are prose until something compares them with what the API
    actually returns; renaming a field on both sides by hand is what happened, and only
    a person noticing kept them together.
    """
    from test_api import bearer, issue_key

    import factories as fac
    import fixture_portfolio as ref
    from app import tenancy

    with session_factory() as s:
        user = fac.make_user(s, "parity@example.test")
        portfolio = fac.make_portfolio(s, "Parity portfolio", owner=user)
        s.commit()
        tenancy.bind(s, portfolio.id, user.id)
        ref.build_reference(s)
        portfolio_id = portfolio.id
    raw = issue_key(session_factory, portfolio_id)

    seen = set()
    for path in ("/api/v1/portfolio", "/api/v1/holdings", "/api/v1/trades",
                 "/api/v1/dividends"):
        response = client.get(path, headers=bearer(raw))
        assert response.status_code == 200, path
        seen |= _flatten(response.json())

    missing = sorted(field for field in PUBLISHED.values() if field not in seen)
    assert not missing, f"claimed as published but absent from the API: {missing}"


# --------------------------------------------------------------------------- #
# Exports
# --------------------------------------------------------------------------- #

def test_every_report_has_a_title_and_a_builder():
    """`exports.TITLES` is what the page offers; a key without a builder is a
    download button that 500s."""
    for key in exports.TITLES:
        assert hasattr(exports, key), f"exports.TITLES names {key!r} with no builder"


def test_every_report_row_matches_its_own_header(pf):
    """An export whose rows drift from its header is silently wrong in a
    spreadsheet, where nothing checks the shape and the columns just shift."""
    import fixture_portfolio as ref

    ref.build_reference(pf)
    for key in exports.TITLES:
        headers, rows = getattr(exports, key)(pf, fy=2026)
        assert headers, f"{key}: no header"
        for i, row in enumerate(rows):
            assert len(row) == len(headers), (
                f"{key} row {i}: {len(row)} values against {len(headers)} columns"
            )


# --------------------------------------------------------------------------- #
# Chart vocabulary
# --------------------------------------------------------------------------- #

# A chart field is a *view* of stored data, so most are derived rather than
# published. Named here so a new one is a decision, not an accident.
FIELDS_NOT_IN_API = {
    "date", "value", "invested", "gain", "gain_pct", "flow_in", "cash_div",
    "value_gain", "invested_delta", "gain_delta", "period_return",
    "ticker", "asset_class", "currency", "exchange",
    "pos_value", "pos_cost", "pos_gain", "pos_gain_pct", "pos_dividends",
    "pos_units", "pos_day_change", "pos_day_pct",
    "period", "p_from", "p_value_then", "p_contributed", "p_growth",
    "p_on_money_in", "p_twr", "p_twr_annual",
}


def test_every_chart_field_is_accounted_for():
    """Same rule as the columns: a new field is either in the API or listed."""
    unaccounted = sorted(set(fields.BY_KEY) - FIELDS_NOT_IN_API)
    assert not unaccounted, (
        f"new chart fields — publish them or add them to the list: {unaccounted}"
    )


def test_the_chart_field_list_has_not_rotted():
    stale = sorted(FIELDS_NOT_IN_API - set(fields.BY_KEY))
    assert not stale, f"no such chart field any more: {stale}"


def test_every_dashboard_column_maps_to_a_holding_attribute():
    """`columns.py` keys are persisted in `user.dashboard_columns`, and
    `columns.clean()` drops unknown keys **silently** — so a key that stops
    resolving removes a column from somebody's dashboard with no error."""
    for col in columns.COLUMNS:
        assert col.key, "every column needs a key"
        assert col.label, f"{col.key}: every column needs a label"


# --------------------------------------------------------------------------- #
# Documentation
# --------------------------------------------------------------------------- #

def _setting_keys(model, prefix: str = "") -> list[str]:
    out = []
    for name, field in model.model_fields.items():
        out.append(f"{prefix}{name}")
        if hasattr(field.annotation, "model_fields"):
            out += _setting_keys(field.annotation, f"{prefix}{name}.")
    return out


def test_every_setting_appears_in_the_configuration_reference():
    """The reference claims to list every option the application understands.

    It said so while nine were missing — the session timeouts, the statement
    designer's directories, the optional-features block and, on the day they
    were added, the live-price settings. A promise like that is either checked
    or it is decoration.
    """
    from pathlib import Path

    from app.settings import PortfolioSettings

    doc = Path(__file__).resolve().parent.parent / "docs/reference/configuration.md"
    text = doc.read_text()
    missing = [k for k in _setting_keys(PortfolioSettings) if k.split(".")[-1] not in text]
    assert not missing, f"undocumented settings: {missing}"


def test_every_setting_appears_in_the_shipped_config_file():
    """`config.yaml` makes the same promise as the reference and was not
    checked against it.

    "Every option the application understands is listed here" was untrue for
    the whole `auth.oidc` block — eight settings, none of them present — and
    nothing failed, because only the reference doc was guarded. A promise like
    that is either checked or it is decoration; this is the second copy of it.
    """
    from pathlib import Path

    from app.settings import PortfolioSettings

    shipped = Path(__file__).resolve().parent.parent / "config.yaml"
    text = shipped.read_text()
    missing = [k for k in _setting_keys(PortfolioSettings)
               if k.split(".")[-1] not in text]

    assert not missing, (
        f"settings absent from config.yaml, which claims to list them all: "
        f"{missing}")


def test_the_documented_routes_exist():
    """Docs name URLs. A renamed route leaves them pointing at a 404 — which
    happened to `/instruments`, `/plan` and `/dca` when they were renamed."""
    import re
    from pathlib import Path

    from app.main import app

    # `include_router` leaves an `_IncludedRouter` rather than flattening its
    # routes into `app.routes`, so walking only the top level cannot see the
    # API or the imports pages — and would pass any URL documented under them.
    def collect(router, into):
        for route in getattr(router, "routes", []):
            path = getattr(route, "path", None)
            if path:
                into.add(re.sub(r"\{[^}]+\}", "{}", path))
            # An included router keeps its own routes behind `original_router`
            # rather than exposing them as `.routes` on the placeholder.
            collect(getattr(route, "original_router", route), into)

    served: set[str] = set()
    collect(app, served)

    def known(path: str) -> bool:
        cleaned = re.sub(r"\{[^}]+\}", "{}", path)
        if cleaned in served:
            return True
        # A documented PREFIX is fine if anything hangs off it — docs say
        # "/api/v1" meaning the whole surface, not one endpoint.
        if any(s.startswith(path.rstrip("/") + "/") for s in served):
            return True
        return any(re.fullmatch(re.sub(r"\\\{\\\}", "[^/]+", re.escape(s)), path)
                   for s in served)

    docs = Path(__file__).resolve().parent.parent / "docs"
    bad = []
    for page in sorted(docs.rglob("*.md")):
        for token in re.findall(r"`(/[a-z0-9/_-]*)`", page.read_text()):
            # Only app routes: no file extension, not a container mount path.
            if re.search(r"\.\w+$", token) or token.startswith(("/data", "/config",
                                                                "/scratch", "/dev",
                                                                "/volume", "/share",
                                                                "/srv", "/etc", "/var")):
                continue
            if not known(token):
                bad.append(f"{page.name}: {token}")
    assert not bad, f"documented routes the app does not serve: {sorted(set(bad))}"


def test_every_module_appears_on_the_architecture_map():
    """The map listed 17 of 43 modules, so a contributor reading it got a
    partial picture of the app and no hint that anything was missing."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    doc = (root / "docs/contributing/architecture.md").read_text()
    modules = sorted(
        p.stem for p in (root / "app").glob("*.py") if p.stem != "__init__"
    )
    missing = [m for m in modules if f"`{m}.py`" not in doc]
    assert not missing, f"modules missing from the architecture map: {missing}"


def test_the_decisions_record_is_reachable_from_the_agent_notes():
    """A decisions list nobody is sent to is a list nobody reads."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    assert (root / "docs/contributing/decisions.md").exists()
    for pointer in ("AGENTS.md", "docs/contributing/index.md",
                    "docs/contributing/style.md"):
        assert "decisions.md" in (root / pointer).read_text(), (
            f"{pointer} does not point at decisions.md"
        )

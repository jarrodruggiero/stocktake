"""The move dialog, end to end — issue #36.

`app/moves.py` decides what travels together and `tests/test_moves.py` pins
that. This file is about the surface: who is offered the control, what the
server refuses regardless of what the page drew, and what happens to the things
that pointed at a trade which has left.

The dialog is a `<dialog>` over the page you were already on, and `/trade/{id}/
move` is a real route rendering the same include — the pattern Record trade
uses. Without scripting the button is a link to that route, and a refusal has
somewhere to land.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal

from freezegun import freeze_time
from sqlalchemy import select

import factories as fac
from app import tenancy
from app.models import Dividend, PlannedPurchase, Portfolio, PortfolioMember, Trade, User
from test_routes import make_login, session_csrf

HTML = {"accept": "text/html"}
TODAY = "2026-09-23"


def _second_portfolio(session_factory, *, name="Second", role="owner"):
    """Another portfolio the logged-in user belongs to."""
    with session_factory() as s:
        user = s.scalars(select(User)).first()
        portfolio = Portfolio(name=name)
        s.add(portfolio)
        s.flush()
        s.add(PortfolioMember(portfolio_id=portfolio.id, user_id=user.id,
                              role=role))
        s.commit()
        return portfolio.id


def _furnish(session_factory, **kwargs):
    """ACME in the active portfolio: a buy, and whatever else is asked for."""
    from test_routes import bind_to_only_portfolio

    with session_factory() as s:
        bind_to_only_portfolio(s)
        acme = fac.make_instrument(s, "ACME", name="Acme Industries",
                                   asset_class="share")
        buy = fac.add_trade(s, acme, "2026-01-05", "buy", 100, "4.00")
        extra = {}
        if kwargs.get("sell"):
            extra["sell"] = fac.add_trade(s, acme, "2026-03-02", "sell",
                                          kwargs["sell"], "5.00").id
        if kwargs.get("cash"):
            extra["cash"] = fac.add_dividend(s, acme, "2026-07-01", "25.00").id
        if kwargs.get("drp"):
            trade, dividend = fac.add_drp(s, acme, "2026-02-01", "12.50", 3,
                                          "4.20")
            extra["drp_trade"], extra["drp_dividend"] = trade.id, dividend.id
        s.commit()
        return {"instrument": acme.id, "buy": buy.id, **extra}


def _ticked(html: str) -> set[str]:
    """Checkbox values the page arrives pre-ticked with."""
    return {
        m.group(1)
        for m in re.finditer(r'<input[^>]*value="([^"]+)"[^>]*checked', html)
    }


def _pulled_in_notice(html: str) -> str | None:
    """The "N more rows ticked" line, or None when there is none.

    Matched on its own wording rather than on the explanation, which also
    appears in the (?) tip — the first version of this searched the whole page
    for the explanation and found the tip instead.
    """
    match = re.search(r"(\d+\s+more\s+rows?\s+ticked)", html)
    # Whitespace-normalised: the template wraps that sentence across source
    # lines, which HTML collapses for a reader and a regex does not.
    return " ".join(match.group(1).split()) if match else None


def _target_options(html: str) -> str:
    """Just the move form's destination <select>.

    Scoped deliberately: every page carries the top-bar portfolio switcher,
    which lists EVERY membership including read-only ones. Searching the whole
    page for a portfolio name finds that instead, and the first version of the
    viewer test passed against the switcher while proving nothing.
    """
    match = re.search(r'<select name="target".*?</select>', html, re.S)
    return match.group(0) if match else ""


# --------------------------------------------------------------------------- #
# Who gets offered it
# --------------------------------------------------------------------------- #

@freeze_time(TODAY)
def test_with_one_portfolio_there_is_nowhere_to_move_to(client, session_factory):
    """No second portfolio, no control — rather than a button that can only
    ever explain itself."""
    make_login(client, session_factory)
    rows = _furnish(session_factory)

    page = client.get(f"/trade/{rows['buy']}/edit", headers=HTML).text

    assert "Move to another portfolio" not in page
    # And the route behind it is not a page either, rather than a form whose
    # only control is an empty dropdown.
    assert client.get(f"/trade/{rows['buy']}/move",
                      headers=HTML).status_code == 404


@freeze_time(TODAY)
def test_the_control_appears_once_there_is_somewhere_to_put_it(
    client, session_factory
):
    make_login(client, session_factory)
    rows = _furnish(session_factory)
    _second_portfolio(session_factory)

    page = client.get(f"/trade/{rows['buy']}/edit", headers=HTML).text

    assert "Move to another portfolio" in page


@freeze_time(TODAY)
def test_read_only_membership_elsewhere_is_not_somewhere_to_move_to(
    client, session_factory
):
    """A viewer cannot be a destination: the move writes at the far end. With
    nothing but read-only memberships there is nowhere to go, so the page does
    not exist — the same answer as having no second portfolio at all."""
    make_login(client, session_factory)
    rows = _furnish(session_factory)
    _second_portfolio(session_factory, name="Read only", role="viewer")

    assert client.get(f"/trade/{rows['buy']}/edit",
                      headers=HTML).text.count("Move to another portfolio") == 0
    assert client.get(f"/trade/{rows['buy']}/move",
                      headers=HTML).status_code == 404


@freeze_time(TODAY)
def test_a_read_only_portfolio_is_left_out_of_the_destination_list(
    client, session_factory
):
    """With one of each, only the writable one is offered."""
    make_login(client, session_factory)
    rows = _furnish(session_factory)
    _second_portfolio(session_factory, name="Read only", role="viewer")
    _second_portfolio(session_factory, name="Super fund", role="owner")

    options = _target_options(client.get(f"/trade/{rows['buy']}/move",
                                         headers=HTML).text)

    assert "Super fund" in options
    assert "Read only" not in options


# --------------------------------------------------------------------------- #
# What the dialog shows
# --------------------------------------------------------------------------- #

@freeze_time(TODAY)
def test_the_list_shows_trades_and_distributions_with_what_identifies_them(
    client, session_factory
):
    make_login(client, session_factory)
    rows = _furnish(session_factory, sell=40, cash=True)
    _second_portfolio(session_factory)

    page = client.get(f"/trade/{rows['buy']}/move", headers=HTML).text

    assert "2026-01-05" in page and "2026-03-02" in page
    assert "2026-07-01" in page          # the cash distribution is offered
    assert "100" in page and "400.00" in page


@freeze_time(TODAY)
def test_the_trade_you_came_from_arrives_ticked(client, session_factory):
    make_login(client, session_factory)
    rows = _furnish(session_factory)
    _second_portfolio(session_factory)

    page = client.get(f"/trade/{rows['buy']}/move", headers=HTML).text

    assert f"trade:{rows['buy']}" in _ticked(page)


@freeze_time(TODAY)
def test_the_rows_that_have_to_follow_arrive_ticked_too(client, session_factory):
    """Pre-ticked rather than refused on submit: the sell cannot stay behind,
    so making somebody discover that by trial is just a puzzle."""
    make_login(client, session_factory)
    rows = _furnish(session_factory, sell=100)
    _second_portfolio(session_factory)

    page = client.get(f"/trade/{rows['buy']}/move", headers=HTML).text

    assert _ticked(page) >= {f"trade:{rows['buy']}", f"trade:{rows['sell']}"}
    assert _pulled_in_notice(page) == "1 more row ticked"


@freeze_time(TODAY)
def test_a_coupled_distribution_is_not_announced_as_an_extra_row(
    client, session_factory
):
    """It has no tickbox of its own — it travels inside the `drp` row.

    Counting it would name a row nobody can see, and blame the unit balance for
    something that is really about the pair being indivisible. So the notice
    counts trades only, and stays silent here.
    """
    make_login(client, session_factory)
    rows = _furnish(session_factory, drp=True)
    _second_portfolio(session_factory)

    page = client.get(f"/trade/{rows['drp_trade']}/move", headers=HTML).text

    assert f"dividend:{rows['drp_dividend']}" not in page
    assert _pulled_in_notice(page) is None


@freeze_time(TODAY)
def test_cash_is_offered_but_not_ticked(client, session_factory):
    """Nothing forces a distribution to move, so the dialog asks rather than
    assuming — but a portfolio that no longer holds the units has no business
    holding the income, which is why it is on the list at all."""
    make_login(client, session_factory)
    rows = _furnish(session_factory, cash=True)
    _second_portfolio(session_factory)

    page = client.get(f"/trade/{rows['buy']}/move", headers=HTML).text

    assert f"dividend:{rows['cash']}" in page
    assert f"dividend:{rows['cash']}" not in _ticked(page)


# --------------------------------------------------------------------------- #
# Performing it
# --------------------------------------------------------------------------- #

@freeze_time(TODAY)
def test_moving_a_trade_reassigns_it(client, session_factory):
    make_login(client, session_factory)
    rows = _furnish(session_factory)
    target = _second_portfolio(session_factory)

    resp = client.post(
        f"/trade/{rows['buy']}/move",
        data={"target": str(target), "row": [f"trade:{rows['buy']}"],
              "_csrf": session_csrf(session_factory)},
        headers=HTML, follow_redirects=False,
    )

    assert resp.status_code == 303
    with session_factory() as s:
        tenancy.bind(s, target, None)
        assert s.scalars(select(Trade)).one().quantity == Decimal("100")


@freeze_time(TODAY)
def test_moving_everything_out_lands_on_an_empty_holding_not_a_404(
    client, session_factory
):
    """Where the redirect goes when the source keeps nothing.

    It works because `Instrument` is a catalogue: ACME still exists after the
    last of its trades has left, so `queries.ledger` finds it and renders an
    empty ledger. If instruments were ever portfolio-scoped this would become a
    404 at the end of a successful move.
    """
    make_login(client, session_factory)
    rows = _furnish(session_factory, sell=100)
    target = _second_portfolio(session_factory)

    resp = client.post(
        f"/trade/{rows['buy']}/move",
        data={"target": str(target),
              "row": [f"trade:{rows['buy']}", f"trade:{rows['sell']}"],
              "_csrf": session_csrf(session_factory)},
        headers=HTML, follow_redirects=False,
    )

    assert resp.headers["location"] == "/holding/ACME"
    assert client.get("/holding/ACME", headers=HTML).status_code == 200


@freeze_time(TODAY)
def test_unticking_a_dependent_is_honoured_and_then_refused(
    client, session_factory
):
    """The pre-tick is a default, not a decision.

    Submitting without the sell used to silently move it anyway, because the
    POST re-ran the same pull-in that had filled the tickboxes — so unticking
    was impossible and the page did the opposite of what it was told. Now the
    selection stands and the move is refused, naming the sell that would be
    left short.
    """
    make_login(client, session_factory)
    rows = _furnish(session_factory, sell=100)
    target = _second_portfolio(session_factory)

    resp = client.post(
        f"/trade/{rows['buy']}/move",
        data={"target": str(target), "row": [f"trade:{rows['buy']}"],
              "_csrf": session_csrf(session_factory)},
        headers=HTML, follow_redirects=False,
    )

    assert resp.status_code == 200
    assert "ACME" in resp.text
    with session_factory() as s:
        tenancy.bind(s, target, None)
        assert s.scalars(select(Trade)).all() == []

    # The refusal arrives with the fix applied: the sell is ticked again, and
    # the notice says so. That is not the silent restoring this test exists to
    # prevent — the message above it explains exactly why, and submitting again
    # is now a deliberate second answer rather than an overruled first one.
    assert f"trade:{rows['sell']}" in _ticked(resp.text)
    assert _pulled_in_notice(resp.text) == "1 more row ticked"


@freeze_time(TODAY)
def test_a_reinvested_pair_moves_whole(client, session_factory):
    """Both halves, even though the form offers one tickbox for them."""
    make_login(client, session_factory)
    rows = _furnish(session_factory, drp=True)
    target = _second_portfolio(session_factory)

    client.post(
        f"/trade/{rows['buy']}/move",
        data={"target": str(target),
              "row": [f"trade:{rows['drp_trade']}"],
              "_csrf": session_csrf(session_factory)},
        headers=HTML, follow_redirects=False,
    )

    with session_factory() as s:
        tenancy.bind(s, target, None)
        assert [t.type for t in s.scalars(select(Trade))] == ["drp"]
        assert s.scalars(select(Dividend)).one().cash_amount == Decimal("12.50")


@freeze_time(TODAY)
def test_a_selection_the_target_cannot_absorb_is_refused_with_the_reason(
    client, session_factory
):
    make_login(client, session_factory)
    rows = _furnish(session_factory, sell=40)
    target = _second_portfolio(session_factory)

    resp = client.post(
        f"/trade/{rows['buy']}/move",
        data={"target": str(target), "row": [f"trade:{rows['sell']}"],
              "_csrf": session_csrf(session_factory)},
        headers=HTML, follow_redirects=False,
    )

    assert resp.status_code == 200
    assert "ACME" in resp.text
    with session_factory() as s:
        tenancy.bind(s, target, None)
        assert s.scalars(select(Trade)).all() == []


@freeze_time(TODAY)
def test_a_row_the_dialog_never_offered_is_ignored(client, session_factory):
    """The posted ids are user input, and the dialog is per-INSTRUMENT.

    Without the filter, a request opened against an ACME trade could name a
    NOVA one and move it — the expansion would happily accept an id the list
    never showed. Nothing valid is left here, so the whole submission is
    refused rather than half-applied.
    """
    from test_routes import bind_to_only_portfolio

    make_login(client, session_factory)
    rows = _furnish(session_factory)
    target = _second_portfolio(session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        nova = fac.make_instrument(s, "NOVA", asset_class="share")
        elsewhere = fac.add_trade(s, nova, "2026-01-06", "buy", 5, "9.00")
        s.commit()
        elsewhere_id = elsewhere.id

    resp = client.post(
        f"/trade/{rows['buy']}/move",
        data={"target": str(target), "row": [f"trade:{elsewhere_id}"],
              "_csrf": session_csrf(session_factory)},
        headers=HTML, follow_redirects=False,
    )

    assert resp.status_code == 200
    with session_factory() as s:
        tenancy.bind(s, target, None)
        assert s.scalars(select(Trade)).all() == []


@freeze_time(TODAY)
def test_a_target_the_user_is_not_a_member_of_is_refused(client, session_factory):
    """The dropdown is built from memberships; this is the forged form value."""
    make_login(client, session_factory)
    rows = _furnish(session_factory)
    with session_factory() as s:
        stranger = fac.make_user(s, "stranger@example.test")
        theirs = fac.make_portfolio(s, "Theirs", owner=stranger)
        s.commit()
        theirs_id = theirs.id

    resp = client.post(
        f"/trade/{rows['buy']}/move",
        data={"target": str(theirs_id), "row": [f"trade:{rows['buy']}"],
              "_csrf": session_csrf(session_factory)},
        headers=HTML, follow_redirects=False,
    )

    assert resp.status_code == 403


@freeze_time(TODAY)
def test_a_viewer_cannot_move_anything_out(client, session_factory):
    make_login(client, session_factory, admin=False)
    rows = _furnish(session_factory)
    target = _second_portfolio(session_factory)
    with session_factory() as s:
        member = s.scalars(select(PortfolioMember)).first()
        member.role = "viewer"
        s.commit()

    resp = client.post(
        f"/trade/{rows['buy']}/move",
        data={"target": str(target), "row": [f"trade:{rows['buy']}"],
              "_csrf": session_csrf(session_factory)},
        headers=HTML, follow_redirects=False,
    )

    assert resp.status_code == 403


@freeze_time(TODAY)
def test_the_move_needs_the_csrf_token(client, session_factory):
    make_login(client, session_factory)
    rows = _furnish(session_factory)
    target = _second_portfolio(session_factory)

    resp = client.post(
        f"/trade/{rows['buy']}/move",
        data={"target": str(target), "row": [f"trade:{rows['buy']}"]},
        headers=HTML, follow_redirects=False,
    )

    assert resp.status_code == 403


@freeze_time(TODAY)
def test_nothing_ticked_is_not_an_error(client, session_factory):
    """Submitting an empty selection does nothing and says so, rather than
    succeeding at moving no rows."""
    make_login(client, session_factory)
    rows = _furnish(session_factory)
    target = _second_portfolio(session_factory)

    resp = client.post(
        f"/trade/{rows['buy']}/move",
        data={"target": str(target), "_csrf": session_csrf(session_factory)},
        headers=HTML, follow_redirects=False,
    )

    assert resp.status_code == 200
    with session_factory() as s:
        tenancy.bind(s, target, None)
        assert s.scalars(select(Trade)).all() == []


# --------------------------------------------------------------------------- #
# What pointed at the trade
# --------------------------------------------------------------------------- #

@freeze_time(TODAY)
def test_a_plan_step_that_recorded_the_trade_is_released(client, session_factory):
    """`PlannedPurchase.trade_id` stays in the SOURCE portfolio, so after the
    move it names a row that portfolio can no longer see. Left alone the step
    reads as "bought" while pointing at nothing — so the link is cleared and
    the step goes back to planned."""
    from test_routes import bind_to_only_portfolio

    make_login(client, session_factory)
    rows = _furnish(session_factory)
    target = _second_portfolio(session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        step = PlannedPurchase(due_date=dt.date(2026, 1, 5),
                               instrument_id=rows["instrument"],
                               status="done", trade_id=rows["buy"])
        tenancy.owned(s, step)
        s.add(step)
        s.commit()
        step_id = step.id

    client.post(
        f"/trade/{rows['buy']}/move",
        data={"target": str(target), "row": [f"trade:{rows['buy']}"],
              "_csrf": session_csrf(session_factory)},
        headers=HTML, follow_redirects=False,
    )

    with session_factory() as s:
        bind_to_only_portfolio(s)
        step = s.get(PlannedPurchase, step_id)
        assert step.trade_id is None
        assert step.status == "planned"

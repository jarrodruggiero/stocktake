#!/usr/bin/env python
"""Print the app's headline numbers, using the app's own query code.

Run it against the same database before and after a change and diff the two —
that is the check any change to `queries.py`, `fyreport.py`, `charts_build.py`
or `exports.py` has to pass.

    uv run python tools/headline_numbers.py > before.json
    # ...make the change...
    uv run python tools/headline_numbers.py > after.json
    diff <(jq -S . before.json) <(jq -S . after.json)

Two things make it trickier than it looks:

  * **Compare with the price feed OFF** (`APP_PRICE_FEED__ENABLED=false`).
    Otherwise the second run fetches the day's closes, every price moves, and
    it reads exactly like a calculation change. That produced 95 false
    differences the first time.
  * The dashboard's path is `cached_holdings` -> `split_positions` -> `totals`.
    Any other route measures something the page does not show.

The output is whatever the dataclasses hold — fields are discovered, never
named — so a renamed field shows up as a rename rather than as a number that
changed.

The numbers are real holdings, so send the output somewhere untracked and
delete it afterwards; it is never something to commit.
"""
import json
import sys
from dataclasses import fields, is_dataclass
from pathlib import Path

# `python tools/headline_numbers.py` puts tools/ on the path, not the root that
# `app` and `appkit` sit in, so importing them needs this. Run it from anywhere
# the repository is checked out; to run it against a container instead, mount
# or copy the file into the application root rather than into /tmp.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app import clock, queries, tenancy  # noqa: E402
from app.models import Portfolio  # noqa: E402
from app.settings import PortfolioSettings  # noqa: E402
from appkit import load_config, make_session_factory  # noqa: E402


def plain(value):
    """Anything comparable, as a string — Decimals and dates included."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, (list, tuple, set)):
        return sorted(plain(v) for v in value)
    return str(value)


def dump(obj):
    if is_dataclass(obj):
        return {f.name: plain(getattr(obj, f.name, None)) for f in fields(obj)}
    return plain(obj)


settings = load_config(PortfolioSettings)
clock.configure(settings.timezone)
factory = make_session_factory(settings.database)
tenancy.install(factory)

out = {}
with factory() as s:
    for pf in s.scalars(select(Portfolio).order_by(Portfolio.id)).all():
        tenancy.bind(s, pf.id, None)
        # Exactly the dashboard route's path; any other would measure
        # something the page does not show.
        holdings = queries.cached_holdings(s)
        open_positions, closed = queries.split_positions(holdings)
        entry = {
            "open": len(open_positions),
            "closed": len(closed),
            "totals": dump(queries.totals(open_positions)),
            "per_holding": {},
        }
        for h in open_positions:
            entry["per_holding"][h.instrument.ticker] = dump(h)
            entry["per_holding"][h.instrument.ticker].pop("instrument", None)
            entry["per_holding"][h.instrument.ticker].pop("trades", None)
        out[pf.name] = entry
print(json.dumps(out, indent=2, sort_keys=True, default=str))

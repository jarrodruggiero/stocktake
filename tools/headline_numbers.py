#!/usr/bin/env python
"""Print the app's headline numbers, using the app's own query code.

    POD=$(kubectl get pods -n portfolio -o jsonpath='{.items[0].metadata.name}')
    kubectl cp tools/headline_numbers.py portfolio/$POD:/tmp/h.py
    kubectl exec -n portfolio $POD -- python /tmp/h.py > before.json
    # ...deploy...
    kubectl exec -n portfolio $POD -- python /tmp/h.py > after.json
    diff <(jq -S . before.json) <(jq -S . after.json)

**This exists because "numbers are sacred" is a ground rule** and doing it by
hand each time gets it wrong. Four things caught me building it:

  * The pod label is `app.kubernetes.io/component=portfolio`, not `app=portfolio`.
  * The package path differs by image age — older images used `/srv/app`,
    newer ones `/srv/apps/portfolio`. Both are on sys.path below.
  * The dashboard's path is `cached_holdings` -> `split_positions` -> `totals`.
    Any other route measures something the page does not show.
  * **Run the comparison with the price feed OFF**
    (`APP_PRICE_FEED__ENABLED=false`) when the "after" side is a test
    container. Otherwise it fetches the day's closes, every price moves, and it
    looks exactly like a calculation change. This produced 95 false
    differences the first time.

Kept out of the repo: these are real holdings. The file lives in the session
scratchpad and the values are only ever compared before/after a deploy.

Every field is discovered rather than named, for two reasons: the package moved
between two images, and comparing them means the script has to run
unchanged on both. Naming fields would make a rename look like a number change.
"""
import json
import sys
from dataclasses import fields, is_dataclass

for candidate in ("/srv/app", "/srv/apps/portfolio"):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

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

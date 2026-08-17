# Development tools

Not part of the application — nothing in `app/` imports these. They exist
because three things this project treats as rules are tedious enough by hand
that they get skipped or got wrong, and each of these was rebuilt from scratch
more than once before being kept.

| Tool | Rule it serves |
| --- | --- |
| `verify_migration.py` | "Every migration is verified against a **populated** predecessor, including downgrade" — which the test suite does not cover, because it builds every schema from nothing at head |
| `headline_numbers.py` | "Numbers are sacred" — the before/after comparison required of any change touching `queries.py`, `fyreport.py`, `charts_build.py` or `exports.py` |
| `screenshot.py` | Rendering the pages and *looking* at them, which is the only thing that catches layout |

```sh
../../.venv/bin/python tools/verify_migration.py 0014        # sqlite
PG=1 ../../.venv/bin/python tools/verify_migration.py 0014   # postgres
../../.venv/bin/python tools/screenshot.py                   # -> shots/
```

`headline_numbers.py` runs *inside* the pod — see its docstring for the
`kubectl cp` / `kubectl exec` pair, and for the four things that make it
trickier than it looks.

**The one that will catch you:** compare numbers with the price feed **off**
(`APP_PRICE_FEED__ENABLED=false`) when the "after" side is a test container.
Otherwise it fetches the day's closes, every price moves, and it looks exactly
like a calculation change. That produced 95 false differences the first time.

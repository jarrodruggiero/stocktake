# Development tools

Not part of the application — nothing in `app/` imports these, and the app does
not need them to run. They exist because the things this project treats as rules
are tedious enough by hand that they get skipped or got wrong, and most of these
were rebuilt from scratch more than once before being kept.

Run them from the repository root.

## The rules they enforce

| Tool | Rule it serves |
| --- | --- |
| `verify_migration.py` | "Every migration is verified against a **populated** predecessor, including downgrade" — which the test suite does not cover, because it builds every schema from nothing at head |
| `headline_numbers.py` | "Numbers are sacred" — the before/after comparison required of any change touching `queries.py`, `fyreport.py`, `charts_build.py` or `exports.py` |
| `screenshot.py` | Rendering the pages and *looking* at them, which is the only thing that catches layout |
| `visual_check.py` | The same pages at desktop **and** mobile width, with the failure stated as a number — every width-specific bug so far rendered perfectly at 1400px |

```sh
uv run python tools/verify_migration.py 0002        # sqlite
PG=1 uv run python tools/verify_migration.py 0002   # postgres

uv run python tools/screenshot.py                   # -> shots/
uv run python tools/visual_check.py                 # then check them
```

**The one that will catch you:** compare numbers with the price feed **off**
(`APP_PRICE_FEED__ENABLED=false`) when the "after" side is a test container.
Otherwise it fetches the day's closes, every price moves, and it looks exactly
like a calculation change. That produced 95 false differences the first time.

## The brand

`app/branding.py` holds the drawing; everything else is generated from it.

| Tool | Does |
| --- | --- |
| `render_brand.py` | Regenerates the Jinja partial, the favicon and the README banners from `branding.py`. **`tests/test_branding.py` re-runs this in memory and fails if anything on disk has drifted**, so it is the one tool here the test suite depends on |
| `solve_spacing.py` | Solves the wordmark's letter offsets by measuring rather than by eye. Needed after any change to a letterform's width |

Both need headless Chrome for the PNG steps.

## Working on the source itself

| Tool | Does |
| --- | --- |
| `prose.py` | Ranks a tree by long comment blocks and docstrings, so a documentation pass can work highest-first |
| `codeshape.py` | Fingerprints the code with comments and docstrings stripped: `save` before a comment pass, `check` after. A pass that claims to touch only prose has to leave this unchanged |

```sh
uv run python tools/prose.py app        # blocks of 4+ comment / 8+ docstring lines
uv run python tools/codeshape.py save   # ...then edit, then:
uv run python tools/codeshape.py check
```

See `docs/contributing/style.md` for what earns a comment, and
`docs/contributing/decisions.md` for where the long reasoning goes instead.

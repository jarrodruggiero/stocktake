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

`app/branding.py` holds the drawing; everything else is generated from it by
`render_brand.py`, which writes the Jinja partial, the favicon and the README
banners. **`tests/test_branding.py` re-runs it in memory and fails if anything
on disk has drifted**, so it is the one tool here the test suite depends on —
a redraw cannot land in the top bar and miss the favicon.

```sh
uv run python tools/render_brand.py     # needs headless Chrome for the PNGs
```

The wordmark's letter offsets are the typeface's own advances and kerning, so
there is nothing to solve: spacing questions are answered by re-exporting from
the font, not by fitting a model to the outlines.

## Working on the source itself

`codeshape.py` fingerprints `app`, `appkit`, `tests` and `tools` with comments
and docstrings stripped, so an edit that claims to touch only prose can be
checked rather than hoped for. It caught a 531-line duplicated region that the
suite also caught — but only because that region happened to be executed.

```sh
uv run python tools/codeshape.py save   # ...then edit, then:
uv run python tools/codeshape.py check
```

See `docs/contributing/style.md` for what earns a comment, and
`docs/contributing/decisions.md` for where the long reasoning goes instead.

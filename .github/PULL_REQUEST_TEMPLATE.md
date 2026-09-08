<!--
Thanks for contributing.

If you are adding a broker, a statement layout, a chart field, a column, a
report or a price source, there is a recipe for it in docs/contributing/ that
names the files to touch and the test to write.
-->

## What this changes

<!-- One or two sentences. What is different after this is merged? -->

## Why

<!-- The reason, especially if it is not obvious from the diff. If it fixes
     something that went wrong, say what went wrong. -->

## Always

- [ ] **The test was written first**, and I watched it fail on its assertion
      before writing the code — see `docs/contributing/testing.md`
- [ ] I then **broke the change on purpose** and watched the same test go red.
      Which mutation? _(one line — "removed the X check and test_Y failed")_
- [ ] Tests pass on SQLite — `pytest tests/ -q`
- [ ] Tests pass on **Postgres** — see `docs/contributing/testing.md`
- [ ] `ruff check app tests tools` is clean
- [ ] Non-obvious decisions carry a comment saying *why*, not what
- [ ] **No personal data.** Fictional tickers, redacted fixtures, no account
      numbers, names or addresses — including in test files and screenshots

## If this touches…

<!-- Delete the lines that do not apply. -->

- [ ] **A migration** — run against a *populated* database, downgrade included.
      "Works on an empty database" has never been the interesting case
- [ ] **A new table holding personal data** — added to `SCOPED_MODELS` in
      `tenancy.py`, or portfolios leak into each other
- [ ] **Anything feeding the daily series** — joined `_series_fingerprint`, or
      charts serve stale numbers after a write
- [ ] **AUD conversion** — figures that cannot be converted come out **blank**,
      never at 1:1. A blank is a question; a wrong number gets totalled
- [ ] **Dates** — from `app/clock.py`, never `datetime.date.today()`
- [ ] **A stored timestamp comparison** — through `appcore.ensure_utc()`
- [ ] **A new config key** — documented in `config.yaml` with its default
      commented out, and in the reference
- [ ] **A new dependency** — said below what it buys and what it costs at
      import time (startup memory is budgeted; see `app/memory.py`)
- [ ] **A price provider** — meets the verification bar in
      `docs/contributing/recipe-provider.md`: documented terms,
      coverage verified against named real instruments, symbols only
- [ ] **User-facing text** — says what to do next, not just what went wrong

## Discussed first?

These are the parts where a well-meaning change can be quietly wrong, so please
open an issue before a large PR against them:

- [ ] This touches the **data model**, **tenancy**, **auth**, or the **tax
      logic** — and I have discussed the approach (link the issue), *or* this
      does not touch any of them

## Anything else

<!-- Trade-offs you made, things you were unsure about, things you would like a
     second opinion on. "I wasn't sure about X" is welcome and useful. -->

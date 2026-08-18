# Notes for AI assistants

The contributor documentation in `docs/contributing/` is written to be acted
on, by people and by you. Read it before changing anything — it is shorter than
the code and it records decisions the code cannot show.

## Start with these

| File | Why |
| --- | --- |
| `docs/contributing/architecture.md` | The one idea (everything computes from trades) and the two safety rules |
| `docs/contributing/testing.md` | **Test-first is a rule here** — plus both backends, and the conventions that waste an afternoon |
| `docs/contributing/style.md` | What deserves a comment here, and what user-facing text should sound like |
| `docs/contributing/decisions.md` | **Choices that look wrong until you know why.** Read before "fixing" anything surprising |

The recipes (`recipe-*.md`) are task-shaped. If the job is "add a broker", "add
a column", "add a report", "add a price source", "add a chart field" or "add a
statement layout", there is a page naming the exact files and the test to write.

**Comments are for two things only** — code a competent reader would find
genuinely hard, and a decision that looks wrong until you know why. Keep them to
two or three lines. Reasoning that needs a paragraph is a decision: put it in
`decisions.md` and point at it by number. Do not narrate what the code plainly
does.

## House rules that are easy to get wrong

- **Everything is computed from trades.** Do not add a column that stores a
  derivable value. If it is slow, the fingerprinted cache in `queries.py` is
  the answer, and any new table feeding the series must join
  `_series_fingerprint`.
- **Tenancy fails closed.** A new table holding personal data goes in
  `SCOPED_MODELS` in `tenancy.py`. That is the whole registration step, and
  skipping it is how portfolios leak into each other.
- **Auth refusals live in `auth.load_session`**, not in routes. Do not add a
  per-route session check; add it there.
- **Dates come from `app/clock.py`**, never `datetime.date.today()`. The
  container runs UTC and the portfolio does not.
- **Compare stored timestamps through `appkit.ensure_utc()`.** SQLite returns
  naive datetimes, Postgres aware ones; the raw comparison raises on one.
- **Never fabricate a number.** An unconvertible AUD figure is blank, not
  converted at 1:1. A missing rate excludes the holding and says so.
- **Only ticker symbols may leave the machine.** No quantities, no holdings, no
  telemetry. This is the app's headline promise.
- **The test suite must not touch the network.** `conftest._no_network` blocks
  it; stub `providers._get_json` with a canned payload.

## Working here

### Write the failing test first. This is not optional.

```text
1. RED     write the test. Run it. Watch it fail ON ITS ASSERTION.
2. GREEN   write the smallest code that passes it.
3. BREAK   break the code on purpose. Watch the same test fail again.
```

Step 1 does not mean "run it and see an `ImportError`" — that only proves you
have not written the function. Make it fail on the assertion, with a message
describing the behaviour you want. Stub the function to return the wrong answer
if that is what it takes.

Step 3 is the one this project leans on hardest, and it is not satisfied by
reasoning about it. Actually delete the guard, actually invert the comparison,
actually run the suite. Tests that could not fail have been found here
repeatedly — including one where a security control could be deleted with
nothing complaining, and one asserting a probe ran that passed with the probe
removed. **Coverage catches none of these**: it says a line ran, not that
anything would notice if the line were wrong.

If you are reporting on work you did, say which mutations you tried and what
happened. "Tests pass" is not evidence; "I removed the `recovery_spent` check
and `test_a_code_cannot_also_answer_the_2fa_challenge_it_led_to` went red" is.

### Then

Run `pytest tests/ -q` **and** the Postgres variant before claiming a change
works — see `docs/contributing/testing.md` for the command. Run
`ruff check app appkit tests tools`.

If you changed a template or a stylesheet, also run `pytest tests/ -m visual`.
Those are deselected by default and drive a real browser over every page at
desktop and phone width; a layout fault is invisible to every other test here.

When you make a non-obvious decision, write the reason in a comment. Not what
the code does — why it does it that way, and what happens if someone changes it.

## Do not

- Weaken a security control to make a test pass. Fix the test, or say the
  control is wrong and why.
- Delete a test that pins reasoning (for example
  `test_equities_have_no_fallback_and_that_is_deliberate`) to make room for a
  change. Update it to reflect the new decision.
- Add a dependency without saying what it buys and what it costs at import
  time — startup memory is budgeted here (see `app/memory.py`).
- Edit a shipped migration. Migrations only ever roll forward.

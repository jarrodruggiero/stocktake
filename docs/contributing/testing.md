# Testing

```sh
pytest tests/ -q                    # SQLite, ~50s
pytest tests/ -q --cov              # enforces the coverage gate
pytest tests/ -m visual             # layout, in a real browser, ~2.5min
ruff check app appkit tests tools
```

## Write the test first

**This project is test-first, and pull requests are reviewed on that basis.**
Not as a matter of taste — because of what keeps happening when it is skipped.

The loop is three steps, and the middle one is the point:

```text
1. RED     write the test. Run it. Watch it fail, and read the failure.
2. GREEN   write the smallest code that passes it.
3. BREAK   break the code on purpose. Watch the same test fail again.
```

**Step 1 is not "write a test and see an error".** A test that fails with
`ImportError` or `AttributeError` has told you nothing except that you have not
written the function yet. It has to fail on its *assertion*, with a message
describing the behaviour you want and do not have. If it cannot fail that way
yet, write a stub that returns the wrong answer and make it fail properly.

**Step 3 is not optional here.** Delete the guard clause, invert the
comparison, drop the `where`, return the wrong constant — and confirm the test
goes red. Then put it back.

### Why this is a rule and not a preference

Every one of these was a real, green, passing test in this repository:

| What it looked like | What it actually did |
| --- | --- |
| A test for the absolute session cap | Passed with the cap deleted — a clamp elsewhere masked it |
| A cache-invalidation test | Could not fail; the session held a stale object either way |
| A test that a database probe ran before connecting | Passed with the probe removed, because the failure path looked the same |
| A test asserting the tenancy filter was installed | Passed with the install removed, because the fixture installed its own |

Coverage found none of them. Coverage says a line ran, not that anything would
notice if the line were wrong. **A test written after the code tends to assert
what the code does; a test written before it asserts what you meant.**

### What a good failure message looks like

```python
assert leaked == [], "the tenancy filter is not attached to this factory"
```

When that fails at 11pm in CI, the message is the whole diagnosis. Add one to
any assertion whose bare form would print two opaque values.

### The exceptions, stated so nobody has to guess

Write the test after, or alongside, when:

- **you are reproducing a reported bug** — write the failing test first anyway,
  it is the cheapest way to prove you have understood the report;
- **you are exploring an unfamiliar API** and do not yet know what the
  behaviour should be. Spike it, throw the spike away, then start at RED;
- **the change is a comment, a docstring, or a rename with no behaviour.**

"It is only a small change" is not on that list. Nor is "it is just wiring".

## Both backends, every time

The app ships on SQLite and claims to run on Postgres. An untested claim is
worth nothing, so the suite takes either:

```sh
docker run -d --name pf-pg -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=portfolio_test -p 55432:5432 postgres:16-alpine

PORTFOLIO_TEST_DB=postgres PGHOST=localhost PGPORT=55432 \
  PGDATABASE=portfolio_test PGUSER=postgres PGPASSWORD=postgres \
  pytest tests/ -q
```

Please run both before opening a PR. Most of the backend-specific bugs this
project has had were found exactly here, and they are all the same shape:
SQLite is permissive where Postgres is strict.

The two that will catch you:

- **Datetimes.** SQLite returns naive, Postgres returns aware. Comparing a
  stored timestamp against `now()` without `appkit.ensure_utc()` works on one
  and raises `TypeError` on the other.
- **Column widths.** SQLite ignores `VARCHAR(80)`; Postgres rejects the 81st
  character with a raw database error. Validate lengths in code.

**Never assert on a backend's representation.** Assert on the answer. A test
that checks a datetime came back naive is a test that fails on Postgres for no
good reason.

## The schema comes from the migrations

The test database is built by running Alembic to head, once per session, then
copied per test. `Base.metadata.create_all` would test a schema production
never has — and it would not catch a migration that fails on a table with data
in it.

If you add a migration, also run it against a **populated** database by hand.
Every migration in this project was verified that way, including the downgrade;
"it works on an empty database" has never been the interesting case.

## Never use a real holding as an example

Tickers, amounts and dates in tests, fixtures, docs and UI placeholders are
**made up**, and there is a house set to draw from: `ALPHA`, `BETAX`, `GAMMA`,
`OMEGA`, `ZULU`, plus `ACME`, `NOVA` and `WIDGET` in the factories. Exchange
suffixes and currencies still behave normally — `ALPHA.AX` is an ASX listing,
`NOVA` a US one.

The reason is not squeamishness. A test suite that reaches for the symbols its
author happens to own publishes a portfolio one commit at a time, and it reads
as real data to anyone who finds it later. Addresses follow the same rule:
tests use the RFC 5737 documentation ranges.

## Conventions that will otherwise cost you an afternoon

Each of these cost someone one:

- **`session_csrf()` reads the NEWEST session.** Capture the token *before*
  creating a session for anybody else, or you get theirs and a 403.
- **`client` and `pf` together deadlock on SQLite** — two writers, one file.
  Use `client` with `session_factory`, and `reading(session_factory)` for
  bound verification reads.
- **Jinja escapes apostrophes.** `assert "don't match" in page.text` never
  matches. Assert on a fragment without punctuation.
- **`Numeric` columns come back as `Decimal`.** Compare with `Decimal("1.23")`,
  not `1.23`.
- **Fixtures run before the test body.** Anything needing a logged-in user must
  be created after `make_login`, not in a fixture.
- **The suite must never touch the network.** `conftest._no_network` blocks the
  HTTP helper. Stub it with a canned payload.
- **Settings priority is env > YAML > constructor kwargs.** Passing kwargs in a
  test does not override what the config file says.

## The coverage gate

`pytest --cov` fails under 92%. The gate ratchets: it goes up as coverage
improves and never back down. `tests/test_coverage_policy.py` enforces two more
rules — every `# pragma: no cover` and every `omit` entry needs a written
justification saying *why it cannot be tested*, not that it is inconvenient.

Coverage is a floor, not a goal. A test that executes a line without asserting
anything about it is worse than no test, because it makes the gate lie.

## Prove your test can fail

The habit this project leans on hardest: **break the code on purpose and watch
the test go red.** It takes thirty seconds and it is the only way to know a
test does anything.

It has repeatedly caught tests that could not fail. Most recently, a test for
the session absolute cap passed with the cap removed, because another mechanism
was masking it — the real hole only showed up when the check was deleted and
nothing complained.

## The visual tests

`tests/test_visual.py` renders every page through `tools/screenshot.py` and
measures it in headless Chrome at 1400px and 412px, failing on four faults: the
document scrolling sideways, an element past the viewport edge, text outside its
own box, and a popup opening off screen.

They are **deselected by default** — a full pass drives a browser over 28 pages
twice and takes about two and a half minutes, which is not what `pytest tests`
should cost. Select them with `-m visual`; CI runs them as their own job.

Two things to know before changing them:

* **Add a new page to `PAGES`** when you add one to `screenshot.py`.
  `test_every_rendered_page_is_listed` fails if you forget, which is deliberate:
  the alternative is a page that renders, is never measured, and nobody notices.
* **The width comes from an iframe, not from `--window-size`.** Headless Chrome
  silently clamps its window to 500px, so a check that trusts the flag measures
  a 500px viewport while claiming to measure a phone. Everything between 412 and
  500 — the width real phones report — went untested for as long as this did
  that.

An element crossing the viewport edge is only a fault if nothing between it and
the page scrolls. `.tablewrap` exists precisely so a twelve-column table can
scroll on its own, and a check that cannot tell that from a bug gets switched
off, so the probe walks ancestors before reporting.

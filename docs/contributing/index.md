# Contributing

These pages are written to be followed, not admired. Each one names the files
to touch, the pattern to copy and the test to write — enough that you can open
a pull request without reading the whole codebase first.

They are also the context an AI assistant needs. If you are using one, point it
at this directory (there is an `AGENTS.md` at the repo root that does this for
you). The recipes are deliberately task-shaped for that reason: "add a broker"
is a better unit of work than "here is the module layout".

## Start here

- **[Architecture](architecture.md)** — the one idea the whole app is built on,
  and the two rules that keep it safe.
- **[Testing](testing.md)** — **this project is test-first**: the failing test
  comes before the code, and both backends run before the pull request. That
  page explains why it is a rule here rather than a preference, with the list
  of green tests that turned out to prove nothing.
- **[House style](style.md)** — what gets a comment and what doesn't.
- **[Decisions](decisions.md)** — the choices that look wrong until you know
  why. Worth reading before changing anything that surprises you.

## Recipes

| I want to add… | Recipe | Code needed? |
| --- | --- | --- |
| Support for my broker's CSV export | [Broker imports](recipe-broker.md) | Usually **none** — it is configuration |
| Support for my registry's PDF statements | [Statement layouts](recipe-statement.md) | A regex or two |
| A field or measure I can chart | [Chart fields](recipe-chart-field.md) | Small |
| A column on the holdings table | [Dashboard columns](recipe-column.md) | Small |
| An export for my accountant | [Export reports](recipe-report.md) | Medium |
| A price source for my country | [Price providers](recipe-provider.md) | Medium, plus **verification** |

## Before you open a pull request

The PR template checks these off for you — it is the same list, in the form of
a checklist you tick. If something on it does not apply, delete the line.

1. **The tests pass on both backends.** `pytest` defaults to SQLite; the
   Postgres run is one environment variable. Both, please — most of the
   backend-specific bugs this project has had were caught exactly here.
2. **The test was written before the code, and you watched it fail.** Then
   break your change on purpose and watch the same test go red again. A
   test that passes either way is documentation, not a test.
3. **`ruff check app tests` is clean.**
4. **Non-obvious decisions carry their reason in a comment.** Not what the code
   does — why it does it that way. See [house style](style.md).
5. **No personal data.** Fixtures use fictional tickers (ALPHA, BETAX, GAMMA)
   on purpose, so a real holding can never be mistaken for test data. Redact
   statement fixtures.

## What is likely to be accepted

Broker and registry support, price providers for other countries, columns,
fields, reports, accessibility fixes, documentation, and bug fixes with a test.

## What to discuss first

Anything that changes the data model, the tenancy rules, the auth flow, or the
tax logic. Not because they are off limits — because they are the parts where a
well-meaning change can be quietly wrong, and it is kinder to agree the
approach before you write it.

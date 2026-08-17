# Recipe: add a price source

Prices, FX and crypto each have an **ordered list** of providers in
`app/providers.py`. The first one that answers wins, and the `source` column on
every stored row records who actually served it.

This is the extension point most likely to matter outside Australia. The
shipped set is deliberately small:

| Kind | Providers |
| --- | --- |
| Equities | Yahoo only |
| FX | Yahoo, then Frankfurter |
| Crypto | Yahoo, then CoinGecko |

**Equities have no fallback on purpose.** That is not an oversight to be
quietly filled — read the next section before writing code.

!!! tip "Start with the test"
    Write it against a **canned payload** first — see
    [Testing it](#testing-it). The suite must never touch the network, so the
    test you write first is also the only test you can write at all. Watch it
    fail, then write the parser. See
    [Testing](testing.md#write-the-test-first).

## The bar a new provider has to clear

A price provider that is wrong is worse than one that is missing, because the
number looks fine and the `source` column says it came from somewhere real.
When this was last researched (2026-08), three candidates were rejected:

- **Stooq** — the CSV endpoint now answers with a JavaScript proof-of-work
  challenge and there is no documented API. Getting data would mean defeating
  an anti-bot measure, which is not something this project will do.
- **Alpha Vantage** — does not document ASX coverage, and users report it
  working intermittently. Unverifiable without an account.
- **Twelve Data** — genuinely covers ASX, but only on a paid add-on.

So, before you propose one:

1. **It must have documented API terms** that permit this use. Not "it works if
   you send a browser user-agent".
2. **You must have verified coverage of real instruments**, and said which ones
   in the PR. "It supports the LSE" is not the same as "GBP-denominated ETFs
   resolve and return daily closes back to 2020".
3. **Keyless is strongly preferred.** If it needs a key, it must be optional
   and off by default, so a fresh install works without anyone signing up for
   anything.
4. **Only symbols may leave.** No quantities, no holdings, no identifiers. This
   is a hard line, not a preference.

## Writing it

A provider is a function. That is the whole interface:

```python
def myprovider_closes(symbol: str, start: dt.date, end: dt.date) -> Rows:
    """One paragraph: who they are, what the endpoint returns, and any
    direction/units gotcha — e.g. whether a rate is AUD-per-USD or the
    inverse, which is the single easiest thing to get backwards."""
    payload = _get_json(f"{MYPROVIDER_URL}/daily", {"symbol": symbol})
    out: Rows = []
    for day, price in payload.get("prices", {}).items():
        value = _decimal(price, 6)
        if value is not None:               # skip junk, don't invent zeroes
            out.append((dt.date.fromisoformat(day), value))
    return out
```

Then register it:

```python
PROVIDERS["equity"] = [("yahoo", yahoo_closes), ("myprovider", myprovider_closes)]
```

Rules the framework already enforces, so you do not have to:

- **Errors are caught** and recorded as a failed `Attempt`; the next provider
  is tried. Never swallow an exception yourself — raise, and let `_try_each`
  report it.
- **Empty counts as no answer**, and the next provider is tried.
- **A short answer is still an answer.** Markets close. Do not pad missing days
  and do not treat fewer rows than requested as a failure — that would flap
  between sources and rewrite `source` on every run.
- **Every call is timed out.** Use `_get_json`, which sets one.

## Testing it

`conftest._no_network` blocks outbound HTTP for the whole suite. This is not
ceremony: an earlier version of this feature had a test that silently passed
against live Frankfurter data, which would have failed on a plane and in CI.

Stub the transport with a canned payload:

```python
def test_myprovider_reads_a_daily_series(monkeypatch):
    canned(monkeypatch, {"prices": {"2026-03-01": 1.50, "2026-03-02": 1.60}})

    out = providers.myprovider_closes("ALPHA.AX", START, END)

    assert out == [(dt.date(2026, 3, 1), Decimal("1.5")),
                   (dt.date(2026, 3, 2), Decimal("1.6"))]
```

Cover: the happy path, a response with missing or null values, a response with
nothing in it, and the symbol format you send (a provider handed the wrong
symbol shape returns an empty series, which looks exactly like a quiet market).

If you are adding an equity provider, also update
`test_equities_have_no_fallback_and_that_is_deliberate` — it pins the current
provider lists *and the reasoning*. Change it to reflect the new state; do not
delete it to make room.

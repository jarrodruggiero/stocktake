# Decisions

Choices that look wrong until you know why. Each one cost something to learn,
and each is the kind of thing a reasonable person would "fix" without this page.

Code points here by number rather than repeating the reasoning inline — see
[style.md](style.md). Add to the end; do not renumber.

## Shape of the app

**1. Everything is computed from trades.** No stored balance, no cached
valuation. Correcting a trade from three years ago corrects every number,
because none of them were written down. Do not add a column holding something
derivable; if it is slow, use the fingerprinted cache in `queries.py`.
→ [architecture.md](architecture.md)

**2. Tenancy fails closed.** Scoped queries are filtered by an event hook in
`tenancy.py`, and a scoped query with no portfolio bound **raises**. A new table
holding personal data goes in `SCOPED_MODELS`; that is the whole registration.
A filter each query must remember is one that will eventually be forgotten.

**3. Auth refusals live in `auth.load_session`.** Expiry, the idle window, the
absolute cap, the half-authenticated 2FA state — one choke point, not per route,
for the same reason as #2.

**4. Optional features remove their routes, not just their links.** An install
that switches a feature off is not carrying an unlinked door to it
(`features.py`).

## Money and truth

**5. Never fabricate a number.** A figure that cannot be computed is blank, not
guessed. The rule has three parts: use the nearest stored FX rate within a
tracked pair; **withhold and name the holding** when the pair is unknown; never
fall back to 1:1, which books a foreign holding as though the currency did not
exist. `totals.excluded` and `dividends_excluded` are how the withholding gets
said out loud.

**6. …but a same-currency row needs no rate at all.** AUD → AUD is 1 by
arithmetic, so requiring a stored `fx_rate` on an AUD row turns a fact about
completeness into a question with no content. One distribution imported without
a rate voided its instrument's entire dividend history, and with most holdings
affected the dashboard's dividend total read as a fraction of the truth —
silently, because the number still looked like a number. `queries.in_aud()` is
the one place that distinction lives. This is not #5
bending: there is no conversion to invent.

**7. Rounded parts must sum to the whole.** CGT proceeds and cost base are
apportioned by running-total differencing, not by rounding each parcel
independently — otherwise three parcels out of $14.00 report $14.01, and a
schedule that does not tie out is worse than one that is a cent coarse.
`fyreport._Parcel.take`.

**8. A DRP residual is a balance, not income.** The whole distribution is
already assessable through `cash_amount`. Never add `residual_carried` to a
dividend total, and note that a DRP parcel's cost base already includes whatever
residual was applied to it.

**9. Only ticker symbols leave the machine.** No quantities, no holdings, no
telemetry. Statement parsing and OCR are local subprocesses with no network.
This is the app's headline promise and it constrains every new integration.

## Market data

**10. Ask an exchange only for sessions it has finished.** "What day is it
here" is a different question from "what has that market finished doing" — at
the 18:00 Sydney feed it is 04:00 in New York, so asking for today's US close
requests a session that has not opened. `pricefeed.MARKETS` holds each
exchange's timezone and closing time; `SETTLE` adds 30 minutes because the bell
is not the same as a settled price.

**11. Public holidays are deliberately not modelled.** A per-market calendar
has to be maintained forever, and being wrong costs one empty fetch. Weekends
roll back; holidays do not. The same reasoning sets `GAP_WEEKDAYS = 3`: a
market shuts for one weekday, or two at Christmas and Easter, never three — so
a shorter threshold would make every holiday a permanent "gap" the feed re-asks
about on every run.

**12. Live prices are stored beside closes, flagged `provisional`.** The app
needs both meanings of "price": what a holding is worth now, and what the day
closed at. Storing only the first corrupts history — a mid-session snapshot
recorded as a close, which happened, permanently, to 99 of 359 closes.

**13. …and the daily run resumes from the last SETTLED day.** This is what makes
#12 safe rather than a relabelled version of the same bug: the run comes back
for the provisional day and replaces it. Without it, one missed evening leaves a
snapshot on the books looking like a close for ever.

**14. Crypto never settles, on purpose.** It has no close, so its bar is only
final at 00:00 UTC and waiting would make the holding always show yesterday.
A fresh number is preferred. Crypto rows stay snapshots;
do not "fix" this without asking.

**15. yfinance for closes, plain JSON for quotes.** Not inconsistency: a failed
quote costs nothing (the page shows the last close), while a failed close feed
makes the history wrong. yfinance's real value is being maintained against
Yahoo's anti-bot measures, so it stays where failure matters. Quotes use the
batched `spark` endpoint — one request for the whole portfolio.

**16. The "Live" badge is built from holdings, not from stored rows.** It
speaks for the portfolio on the page. Built from the instrument table it was lit
around the clock by a zero-unit BTC row — a security nobody owned.

## Operational

**17. Heavy imports are deferred, and that buys less than it looks.** `yfinance`
(~101 MiB of pandas and numpy) and `pdfplumber` import on first use. But the
feed's catch-up runs seconds after boot, so an install with the feed **on** pays
it anyway and keeps it for life. The laziness protects the feed-disabled
install, the CLI and the test suite. Do not quote it as the reason the pod is
small.

**18. Measure memory after the first feed run, never at startup.** A 63 MiB
start-up reading is what set the request to 128Mi when the real working set was
182 MiB.

**19. Background polls must not slide the session window.** Anything the page
calls on a timer belongs in `auth.SLIDING_EXEMPT`, or a tab left open keeps its
own session alive for ever and the idle timeout is decorative.

**20. `versions/` is a single generated `0001_initial`.** It is generated from
`models.py` — change the models and regenerate; never hand-edit it. The drift
test (`test_the_models_and_the_migration_have_not_drifted`) is what makes that
safe.

**21. The API's published/withheld list is the contract.** Every stored column
is either published with the field carrying it, or withheld with a reason, in
`tests/test_api_parity.py`. Adding a column fails the suite until somebody
decides which. The reasons are the documentation — the only kind that cannot go
stale.

**22. Scratch files live in `/scratch`, never in the data directory.** The
statement mapper renders page images of a document somebody is mapping — their
name, address and holder number — kept 0700 and deleted 30 minutes after last
use. `/data` is wrong for them twice over: it is the directory the deploy docs
tell you to back up, so a retention policy would keep exactly what the page
promises to throw away, and a backup mover that drops capabilities loses
`DAC_OVERRIDE` and cannot traverse a 0700 directory it does not own. `/scratch`
exists in the image, so this needs no volume; mount an `emptyDir` for a size
limit. Losing a mapping session on restart is intended.
**23. `user.public_id` exists ahead of need, and must never be reissued.** An
auto-increment `id` cannot be an OIDC `sub`: it leaks how many accounts exist
and when this one was made, and every app's "user 1" collides. Adding it later
means backfilling across every federated app at once. Reissuing one would
silently unlink the account everywhere it had ever signed in.

**24. Some schema declarations look redundant and are not.** `index=True`
without `unique=True`, alongside a named constraint in `__table_args__`,
produces a plain index plus a named UNIQUE — not one unique index. Same
guarantee, different catalogue entries, and the difference is exactly what a
schema comparison reports. Applies to `user.public_id` and
`webauthn_credential.credential_id`.

**25. A sortable header may cycle through four states, not two.** Gain and
Today show an amount with its percentage underneath, and both are worth ordering
by: a $2 gain on $10 beats a $200 gain on $100,000, and "what made the most" and
"what grew fastest" are different questions. The header cycles value ↑, % ↑,
value ↓, % ↓ rather than growing a second control. The state must be *rendered*,
not just explained — after two clicks nobody can tell which of four they are
in.
**26. The CGT module refuses financial years it does not understand.** From
1 July 2027 the 50% discount is replaced by cost-base indexation plus a 30%
minimum tax, and an asset held across that date is apportioned on its market
value at the boundary rather than a time split — announced, not legislated, with
the CPI series and the approved apportionment method still unsettled. Applying
a repealed discount to an FY2028 disposal would return a confident wrong number
on a page people use to prepare a tax return, which is worse than refusing.
`fyreport.CGT_INDEXATION_START` is the boundary.

**27. The navigation holds places, not controls.** A control belongs beside the
thing it acts on, not in a list of destinations repeated on every page — so
Charts, Record trade and Manage holdings live on the Portfolio page, and Admin
is a cog in the top bar because it is app-level rather than a destination. What
is left is the three things that are genuinely places.

**28. A split chart of a ratio is grouped, never stacked.** Ratios do not add
up: splitting a +20% / +40% asset class by ticker drew a stack 0.60 tall where
the unsplit bar of the same holdings is 0.30 — twice the height, same data, and
a number nobody had computed. Grouped rather than refused, because "each
ticker's gain % within its class, side by side" is a real question and the
segments are individually correct; only putting them end to end invented a
total. `charts.js` groups when `stacked` is false.

**29. Three paths are public to the middleware but not to the world.**
`/login/code` runs while the session is deliberately half-authenticated, and
refuses without a live `awaiting_totp` session. `/session/status` must answer
the idle overlay rather than return a 401 it has to interpret, and discloses
only whether the cookie already presented is still live. `/metrics` has no
cookie to present; it is off unless enabled and 404s when off, and **that check
is the whole access control** — read `metrics.py` before adding a series.

**30. Sixteen recovery codes, not eight and not a passphrase.** They stand in
for a forgotten password as well as a missing second factor, and an
authenticator-less sign-in burns one every time until the person re-enrols, so
the list drains faster than eight allows for. Sixteen single-use codes beat one
long secret: a spent one is worthless to a shoulder-surfer and the list survives
being partly used. A 12/24-word phrase is the wrong shape here — those are
offline-derived master keys held by their owner; these are server-issued, stored
hashed, and revocable one at a time.
**31. Many columns carry both `default=` and `server_default=`, on purpose.**
`default=` is Python-side and covers rows this app inserts; `server_default=` is
DDL and covers everything else, including a migration adding a NOT NULL column
to a populated table. The migrations declared them and the models did not, which
is invisible in normal use and poisonous in one way: autogenerate diffs the
*models* against the database, so each looked like a default to drop — an
autogenerated migration would have removed eighteen of them. Keep the pairs in
step; the drift test compares them.

**32. The `periods` chart grain cannot be split, and that is not a limitation.**
The grain *is* a fixed set of performance windows, and a window has no sub-parts
to break out. Before the rule existed a split here was accepted and then
silently dropped — `charts_build._periods` ignores `split` — so the chart drawn
was not the chart asked for, with nothing saying so. The builder reaches it by
clicking the Period chip twice.

**33. `app_name` is defaulted so the app boots with no config file.** A
first-run wizard cannot ask you anything if the process will not start without
the answers, and "create config.yaml before starting" is the instruction people
skip. Every other setting already had a default, so this one makes the whole
file optional and the wizard writes it. **Changing it moves the database**:
appcore derives the SQLite path from `app_name`, so a rename points a running
install at a file that does not exist and it will cheerfully create an empty
one. Set `database.path` explicitly if you ever change it.

**34. Two different things both sounded like "admin", so the labels name the
scope.** `user.is_admin` is app-wide — accounts and settings. `member.role ==
"owner"` is rights over one portfolio — its people and its keys. Collapsing them
to one word would make them look identical while behaving differently, so the
interface says "Site admin" and "Portfolio admin". The stored values stay
`owner`/`member`/`viewer`.

**35. Annualise on the window asked for, not the span it snapped to.** `start`
is the first price date on or after the cutoff, so a weekday-only feed lands on
a weekend about two days in seven and the span comes to 363–364 days. The window
is still the year the row is labelled, and annualising a 364-day one moves it by
~0.1%. The arithmetic still uses the real span. "All time" passes no window and
is judged on its actual span: it is only a year old when it is a year old.
## Imports, and the documents they read

**36. A recovery code substitutes for the password, never for the second
factor.** There is no reset email: a self-hosted app has no mailbox it can rely
on, and a reset link is only as strong as the inbox it lands in. Somebody
holding only the code list therefore gets as far as choosing a new password and
is then stopped by the 2FA challenge — because a session that has already spent
a code cannot spend another (`UserSession.recovery_spent`). That single rule is
what stops a stolen list from being the whole account.

**37. The document never leaves the machine, and inference is testable.** The
statement designer and the broker mapper both work on text already extracted
in-process and posted back with the form — nothing is uploaded anywhere and
nothing is stored, because a broker export is a list of everything somebody owns
and when they bought it. Inference lives in `docformats.py` and
`brokerdesign.py` rather than in a route or in JavaScript: a wrong label
produces a template that reads the wrong number on somebody else's statement,
which is the failure the whole feature exists to avoid. What a template exports
describes **where to look**, never what was found.

**38. Templates can be installed through the interface only because they are
inert.** Declarative YAML, loaded with `YAML(typ="safe")`, whose patterns never
reach `re.compile` — so a hostile file is a parsing problem, not code execution.
Without this route, using a template meant putting a file on the container's
disk by hand, which on Kubernetes means a read-only ConfigMap. **If templates
ever gain an executable escape hatch, this route goes with it.**

**39. Two-factor enrolment is two requests, and the pending secret is held on
the user row.** Showing a secret and then confirming it with a code derived
from it is what stops somebody enabling 2FA with a secret they never
successfully scanned and locking themselves out.

It used to ride in the form so that an abandoned enrolment left nothing. That
was the wrong trade: the secret was regenerated on every render, so refreshing
the page — or coming back to it after a mistyped code, which the wizard does on
its own — silently invalidated the QR just scanned, and the phone's code stopped
matching with nothing on screen to explain it. Found in sundries and present
here twice, on the profile page and in the wizard.

So an enrolment in progress is **resumed**: the secret is stored, and returning
shows the same QR. Storing it enables nothing, because `is_enabled` wants
`totp_enabled_at` as well — an unfinished enrolment is inert and signs nobody
in. It also closed something the form version allowed: any secret posted with a
matching code used to be accepted, so a caller could enrol an authenticator the
account had never scanned.

**40. A back button reads `?return=`, never `Referer` or `history.back()`.**
The holding page is reached from the dashboard, an FY page, Manage holdings and
the DCA schedule, so a fixed "← Portfolio" was wrong three times in four. The
header and the history stack are guesses that vanish on a refresh or a shared
link, and the button would then be *labelled* for a page it does not go to. The
rule the charts page set: it says where it goes.

**41. The database connection is deliberately conditional.** A container with
empty appdata and no configuration has nothing to connect to, and the first-run
wizard cannot ask if importing the module already failed trying. An unconfigured
install boots with `database.is_ready()` false, serves the wizard, and connects
once somebody has chosen. When one *is* configured, migrations run here before
any traffic is served.

**42. Recovery codes are 15 characters, and the throttle stays regardless.**
NIST SP 800-63B wants a look-up secret to carry 64 bits, or 20 if failed
attempts are throttled. At log2(31) = 4.95 bits a character, 13 clears 64; 15 is
used because it groups into three blocks of five, which is the difference
between a code transcribed correctly and one that is not — 74 bits. **The
throttle is not removed when the arithmetic stops needing it**: it turns a wrong
guess into one wrong guess rather than the first of millions.
**43. A stale login form gets the login page back, not a 403.** Nobody is
signed in without a valid token, so this changes only how a refusal is
presented. A cross-site POST carries no `SameSite=Lax` cookie, has nothing to
echo, lands in the same branch and gets a login page — the same outcome the 403
produced, without accusing somebody whose page was simply open too long.

**44. Two session limits, because they stop different things.**
`session_idle_minutes` is the sliding one — an hour, not days, because this is a
finance app left open on shared machines. `session_absolute_days` is a hard cap
from creation that the sliding renewal cannot extend; without it a session used
daily never expires, and a stolen cookie stays valid forever as long as it keeps
being used.

**45. One recovery code per sign-in.** Otherwise somebody holding the list
spends one for the password and a second for the second factor, and the list
alone is the whole account. NIST SP 800-63B is the reasoning: a code is a
look-up secret, one authenticator of type "something you have", and two factors
must be of different types — password + code counts, code + code does not.
`UserSession.recovery_spent` enforces it.

**46. `jurisdiction`, not `country`, and the label may differ from the column.**
It decides which tax rules apply — the financial-year boundary, the CGT rules,
and the words the reports use. It follows tax residency rather than an address,
and the two genuinely differ for some people. The Admin page still says
"Country" because that is the word people recognise: the user-facing term and
the column name are allowed to differ, and the code is where the distinction has
to be unambiguous.

**47. NULL in `user.display_currency` means "the portfolio's".** A default of
`"AUD"` would be a claim that somebody chose it, and would silently pin the
wrong currency for the first person running this outside Australia. Same shape
as any preference column: absent is not a value.

**48. `fx_rate` is captured at the trade's date and never re-derived.**
Re-deriving history from today's rates changes last year's tax return. NULL
means not captured, and the price feed can backfill it. The name says
reporting-currency-per-native rather than AUD so it does not contradict
`portfolio.reporting_currency` the day somebody sets that to something else.

**49. Two-factor is a setup step again, but an opted-into one.** It was removed
in 2026-08-08 as a second door to a room `/profile/2fa` already opened, and the
cost named at the time is the one that arrived: **a step is a prompt and a
toggle is not.** Nobody enrolled. It is a step again — offered only when the
account step's tickbox asks for it, so the run that does not want it is not
lengthened, and `setupwizard.skipped()` keeps the rail and the router agreeing
about whether it exists. The line about doing it later moved to that tickbox's
tooltip, where the question is actually being asked.

**50. "Avg price (AUD)" is not `cost_aud / units`.** `cost` is buys only and
includes brokerage, and `units` is net of sales, so that quotient is a third
number — and it would sit beside a column headed "Avg price" in the native
currency that means something else. Two columns of the same name must differ
only by the exchange rate, so it is computed on the same basis as the native
one: buys and DRP allocations, no brokerage, each converted at its own rate.

**51. A spent recovery code is refused indistinguishably from a wrong one.**
Saying "that code was real, but you may not use it here" would confirm to
somebody who stole the list that they hold a valid one. See #45 for the rule
being enforced.

**52. Two columns sort by something other than what they display.** `ticker`
hands the renderer the whole holding — a link plus a currency badge — and
`Holding` objects do not compare. `drp` renders "yes" or nothing, and nothing
sorts as blank, which would push every non-reinvesting holding to the bottom in
*both* directions and make one of the two clicks appear to do nothing.
Everything else sorts by what it shows.

## Testing

**53. The suite must not reach the network, and one route out is not obvious.**
Adding an instrument calls `main._kick_feed()`, which under TestClient finds a
running loop and schedules a *real* feed run on a worker thread — importing
yfinance, calling out, then running a full `gc.collect()`. That thread outlives
the test that started it, so the failure does not look like a test failure: the
suite passes while background threads make live calls, and the operating system
logs crash reports from GC traversing pandas on a stack that is not the main
one. `conftest._no_network` blocks that, `providers._get_json`, and yfinance
itself (`pricefeed._yf`). The last is easy to miss: yfinance's HTTP client is C
and never touches Python's socket module, and `pricefeed.lookup` reads any
failure as "not found", so an unstubbed lookup made a real call and passed. The
stand-in fails the test outright, which `lookup` cannot catch.
`tools/screenshot.py`, which the visual tests run as a separate process, is
outside conftest's reach, so it stubs `_kick_feed` itself and asserts at the end
that yfinance was never imported — it started a real feed run on every visual
pass until it did.

**54. The performance line is cumulative gain over money in, not a
time-weighted return.** "Don't count new investments as a gain" first read as
TWR, which removes contributions by chaining daily returns — it compounds, and
on a long dollar-cost-average history it showed 125% where the portfolio had
made 47%. Arithmetically fine, and not the number anybody means. The line uses
the same definition as the Gain tile, the FY pages and the exports.

**55. An HTTPS-configured instance reached over plain HTTP says so.** This is
the nastiest failure in the app and it is silent: with `cookie_secure` on, a
browser on plain HTTP **discards** the session cookie. The password is accepted,
`Set-Cookie` is sent, the browser throws it away, and the person lands back on
the login page with nothing to explain it — so they try the password again. No
second factor helps; the cookie cannot be stored however you authenticated. The
only useful response is to say so before they try.

**56. Sorting must not be able to move a figure.** It reorders the lists totals
are computed from. That it cannot change one is true — Decimal addition is exact
and commutative, and the FY report totals before it reorders — but true is not
the same as pinned, and this is the rule with the least tolerance for a
regression. `tests/test_sorting.py` holds the pins.

**57. One definition of "did this template read the right values".**
`test_format_samples.py` owns that comparison and everything else imports it.
Two implementations would eventually disagree, and the wrong one would be the
copy. It matters more than it sounds: an expected row lists the fields worth
pinning, not every field a template returns, so a strict dict comparison fails
on a perfectly good read.

**58. `back` is a convenience that must not become an open redirect.** The
column chooser posts the page's current query back so saving does not throw away
the sort you were reading under — which makes it a form field, attacker-supplied
and headed for a `Location` header. `_safe_query` and `_safe_path` are the
guards. Found untested by mutation: deleting the validation broke nothing.

**59. A scan has no text layer, so the mapper falls back to OCR.**
`extract_words` reads the text layer, and the first version of the statement
mapper therefore rendered a page with nothing clickable on precisely the
documents it was built for. Found by running a real scanned PDF through it, not
by any test above it.

**60. The setup rail leaves out steps that do not apply, and `done` beats
`skipped`.** An install with its database and config supplied should not be
looking at nine steps it will never visit. `_skipped_steps()` asks whether a
database is *configured*, and after the database step there is one — so without
this rule a step just completed is reclassified as "not needed" and vanishes
from the rail. **Completing something is the strongest fact about it.**
**61. `python -m app.recover` clears the second factor and forces a password
change.** It is the last resort — for somebody who has lost both their password
and their recovery codes — so leaving 2FA on would hand back an account they
still cannot reach, having spent the only tool left (`--keep-2fa` covers the
narrower case). `must_change_password` is set because the ordinary use is an
administrator recovering somebody else's account, which ends with the
administrator knowing that password and the second factor gone. One extra form
closes it, and self-recovery pays the cheaper of the two mistakes.

**62. Dashboard columns render in the order they were chosen.** Declaration
order was deliberate once — a table whose columns move between sessions is
harder to read — but that argument is about *incidental* movement, and an order
somebody picked is stable by definition. Unknown keys are dropped silently,
because a stored preference outlives the column it names and a saved choice must
never be what stops the dashboard rendering; duplicates collapse for the same
reason. A locked column keeps its position — locked means it cannot be turned
off, not that it must come first.

## Boundaries

**63. Tenancy is a boundary inside the app, not against the database.**
`tenancy.py` stops a bug in a route or query serving one person's portfolio to
another. It is no defence against anyone holding the file: the importer CLI,
`sqlite3 /data/stocktake.db`, a copy of the PVC or a restic snapshot all read
every account, and no login check could change that — it would run in the
process that already has the file. The boundary is "can reach the volume", not
"has an account". Real separation needs per-user encryption keyed on their
password, which would also lock out the price feed, the FY reports and the
backups.
**64. Formats are data because nobody can test a format they have no document
for.** Whoever maintains this holds accounts at almost none of these
registries, so a parser per document is code the merger cannot verify. A
template plus a redacted sample inverts that: the person with the statement
authors it, and CI checks it forever on hardware that has never seen the real
document.

**65. The setup draft lives in memory, and nothing is written until it works.**
One uvicorn worker means no second process to disagree; a restart mid-wizard
should lose a draft nothing was written from; and persisting it would mean
writing setup state into the database the wizard has not chosen yet. The
database is probed before it is connected and connected before it reaches
config.yaml, which is written once, at the end — so an abandoned wizard leaves
no file, which is what makes starting again safe.

**66. Recovery codes come before two-factor enrolment.** They are what makes
skipping the next step survivable. The usual reason to put them after is that
they are a by-product of enrolling — but here they are also the whole of
password reset, so they are issued whether or not the next step runs and cannot
belong to it. Ordering them first also closes the window where a second factor
is live and nothing can recover it. Issued once and re-shown, never reissued:
see #111.

**67. A provider falls back on error or empty, never on "fewer rows than
asked for".** Three days when five were requested is normal — markets close.
Treating it as failure flaps between sources and rewrites the same rows with a
different `source` every run.

**68. Equities have no fallback provider, deliberately.** Stooq answers with a
JavaScript proof-of-work challenge and publishes no API, so using it would mean
defeating an anti-bot measure. Alpha Vantage does not document ASX coverage;
Twelve Data lists ASX only on a paid add-on. A provider whose coverage of the
actual holdings cannot be verified is worse than none — it fails silently while
`source` claims the numbers came from somewhere real. The seam works:
`PROVIDERS["equity"]` is a list, and adding one is a function plus a config
entry.

**69. Redaction masks what has a mechanical shape and says plainly what it
cannot.** Long digit runs, TFNs, emails, phone numbers and money are masked; a
name and a street address are just words, and no pattern separates
"MR JOHN SMITH" from "ACME REGISTRY PTY LIMITED". The result is shown in an
editable box before anything leaves the machine. A redaction that misses a line
is worse than none, because somebody trusting it publishes their address.

**70. Redacted amounts are shifted, not zeroed.** Zeroing makes every expected
value `0.00`, so the file whose job is proving the template read the right
fields cannot tell `net_amount` from `franking_credits`. Each figure keeps its
shape — digit count, grouping, decimals — with different digits derived from
the original, so the same amount appearing twice stays the same amount.

**71. Account recovery is a command, not documented SQL.** Setting a password
by hand means generating an argon2id hash by hand, and pasting a plaintext
string produces an account that can never log in and an error explaining
nothing. The command runs the same hashing, validation and session invalidation
the web UI does. It is not a backdoor: it needs shell access to the machine
holding the database, and anyone with that can already read every holding,
session hash and TOTP secret (#63).
**72. The portfolio is the tenant, not the person.** Several people can
contribute to one (`portfolio_member`, roles owner/member/viewer), so trades,
dividends, plan history and per-holding notes carry `portfolio_id`, and that is
what `tenancy.py` filters on. `user_id` survives as *who recorded it* — an
audit trail once a portfolio has several contributors — and must never be
filtered on. Market data (`instrument`, `price`, `fx_rate`) is deliberately
shared: one person adding ALPHA gives everyone its price history, and nothing
about a holding is inferable from a ticker existing.

**73. Rendered statement pages are never held between requests.** They go to
disk and are served from there, so a pod that has run the visual mapper once is
not carrying bitmaps until its next restart. The session token is random and
the directory records who created it, so another signed-in user presenting the
token gets nothing. Thirty idle minutes ends a session, and ending it deletes
the files rather than refusing to serve them — swept on the next upload, and by
the daily maintenance job for the install where nobody uses the feature again.

**74. The restart button says what the deployment can actually do.**
Kubernetes replaces a stopped container immediately. Docker only restarts when
the container was created with a restart policy, and nothing inside can see
whether it was. A bare `uvicorn` will not come back at all, and an unqualified
"Restart" there would be offering to take the application down. So
`supervision()` reports which of the three it is and the page words the button
accordingly.

**75. "Configured but failed" is a different state from "not configured".** The
answer to the first is "fix this" and to the second "choose one". Silently
falling back to an empty SQLite file would be the worst response available: it
looks like it worked and the data is gone.

**76. The OFX reader never hands an upload to a general XML parser.** External
entity expansion and entity-expansion denial of service both live in Python's
stdlib parsers unless deliberately disabled. A reader that understands only
tags and text has no entity machinery to abuse.

**77. The CGT discount is applied per parcel in the export, and after losses in
the FY report.** The ATO allows the 50% discount on a parcel held over twelve
months, and applies capital LOSSES before it. The export shows the per-parcel
figure because it is the working; the FY report nets losses first because that
is the number to lodge. Both are right for their job, so the caveat travels
inside the exported file rather than living only in the interface.

**78. config.yaml is edited in the app, under three constraints.** It may not
be writable — mounted from a ConfigMap or baked into an image — which is a
deployment choice, not a fault, so the page shows every value and explains why
it cannot save rather than failing when tried. Comments must survive, because
the shipped file documents every option and a plain YAML dump would strip it on
the first save. And `database` and `app_name` are read-only: changing where the
data lives, or the file's own name, from a web form is a way to lose a
database.

**79. Five things the second factor gets right that are commonly got wrong.**
The secret is stored as issued, in the clear — encrypting it needs a key in the
process that reads it, and anyone who can read that column can already read
every holding (#63). The QR is rendered locally, because the obvious shortcut
sends the secret to a third party in a URL. One step of clock drift is accepted:
stricter generates support requests, looser widens replay for no gain. Recovery
codes are hashed like session tokens and marked used rather than deleted, so a
replay is distinguishable from a code never issued. And no TOTP code is
accepted twice (RFC 6238 §5.2; GHSA-28h6-x798-2qv5, from freeb5d): the step of
the last accepted code is kept on the user and only a later one passes. The
rule is the condition on a single UPDATE rather than a read then a write, so
two requests racing with one code cannot both get in. The step is cleared with
the secret.

**80. A currency symbol is part of the value, not decoration.** Templates once
wrote a literal `$` before `{{ x | money }}`, which produced `$-4.00` and left
the symbol outside the privacy blur, sharp beside a smudge. Headline numbers
carry the symbol because no column header can carry it for them; table columns
carry it in the header, because a `$` repeated down two hundred right-aligned
cells is noise that fights the alignment making the column scannable; a column
that can hold more than one currency names it per row.

**81. Which column a table is sorted by lives in the URL.** It survives a
reload, can be linked and bookmarked, and two people looking at one portfolio
do not fight over it — the honest scope, since sorting is something you do
while reading, unlike which columns exist. Sorting is server-side because the
values are Decimals and some are None: in the browser "1,234.50" sorts as text
and an em dash sorts wherever the browser feels like. Blanks sort last in both
directions, since `reverse=True` would otherwise put the rows with no data on
top.

**82. The timezone is process-wide configuration, not an argument.** Threading
it through `queries`, `fyreport`, `plans` and `calendarview` would put a
parameter on two dozen functions with nothing else to say about it, and every
signature would be a chance to forget. Set once at startup from
`settings.timezone`.

**83. The price feed commits per instrument, not once at the end.** SQLite
takes a single writer and this job is mostly network latency, so one
transaction spanning every fetch starves page loads until they fail on
`busy_timeout`.

**84. OCR is local or it does not happen.** A dividend statement carries a
name, an address, a holder number and an amount. That rules out every hosted
OCR API and every "just send it to a model" shortcut, and leaves Tesseract — a
local binary with no network of its own. Measured, not estimated: it adds 107
MB to the image, 37 MB of that `libicu` via leptonica and 15 MB language data.
That cost was weighed and accepted; the alternative was the promise that only
ticker symbols leave.

**85. The metrics endpoint is off by default and has no client library.**
Every other route needs a session, so an upgrade that quietly began answering
an anonymous caller would be a surprise — the answer being harmless is not the
same as the change being expected. Six series in a fixed format is also less
code than wiring up a registry, and the memory budget is the reason not to pull
in a library that costs more than it saves.

**86. The date format is guessed from the values, not the header.** `%d/%m/%Y`
against `%m/%d/%Y` cannot be told apart by a column name, and the wrong choice
is silently correct for eleven rows in twelve — only the first twelve days of a
month disagree, which is the worst kind of wrong. `dates_are_ambiguous()`
exists so the interface can say when the samples genuinely cannot decide, rather
than guessing more confidently.

**87. Switching a feature off hides it; it never deletes anything.** The nav
entry, the calendar and the editor go, and the route says so rather than 404 —
a 404 for a page that exists and is switched off is a lie. The plan, its
rotation and every `planned_purchase` row stay, so turning it back on restores
what was there. Somebody switches a feature off to tidy the nav, not to throw
away two years of schedule.

**88. The API is append-only.** The web UI can edit and delete a trade; the API
cannot, and there is no PATCH or DELETE to find. A key is a long-lived
credential held by another program, and the blast radius of a loop with a bug in
it is very different for "wrote a duplicate trade" than for "rewrote the last
three years". Corrections are a human sitting in front of the ledger.

**89. The theme designer exposes six colours, not every token.** Exposing all
of them lets somebody set their page background to the colour of their text. The
six are the ones carrying meaning rather than structure — the accent, the two
directions money can go, and the three chart series — so a bad choice is ugly,
never unusable. One value applies to both light and dark, because a colour
legible on white rarely is on near-black, so contrast is reported against both
surfaces and the weaker one shown. Advice, not a veto.
**90. The in-app log is WARNING and above, in memory, and never written to
disk.** Error-only says something broke and nothing about what led there; INFO
is every HTTP request, which buries the line that matters. The band between
carries a feed falling back, a statement that parsed with fields missing, a job
that skipped. It stays in memory because these lines can quote a ticker or a
filename, and a log file is one more thing to reason about when the promise is
that the data stays in the deployment. A restart clears it, so the real logs
remain the source of truth for anything historical.
**91. The ledger is not append-only, which is why `trade.updated_at` exists.**
The series cache fingerprints on it; without that an edit changes no row count
and every chart goes on serving pre-edit numbers. A DRP trade is not editable
directly — its units and its dividend's cash are one statement event, so it is
edited through the dividend or not at all.

**92. Every /setup route opens with `_wizard_step()`.** The prefix is public,
because the early steps run before there is a database to hold a session, so
the login middleware cannot gate them. A route under /setup without that call
is one anybody can post to.

**93. Sale proceeds are apportioned across parcels by running-total
differencing.** Per-unit-then-round gives each parcel its own rounding error:
three one-unit parcels out of 14.00 each came to 4.6666…, rounded to 4.67, and
the schedule reported 14.01 of proceeds against 14.00 of money. Carrying the
total forward and taking differences makes the parts sum to the whole by
construction, and the leftover cent lands on a parcel instead of on nobody.

**94. The pre-auth CSRF cookie outlives the session idle window by a wide
margin.** If it lapses first, the login page somebody was just sent to fails its
own double-submit check, with a message that reads as their fault. Twelve hours:
long enough that a login page left open overnight still works, short enough to
stay a bounded credential-free token.

**95. Two returns are reported, never one.** `on_money_in` is growth over what
was at work — the familiar figure, and the one that stays low while somebody is
still buying in. `twr` chains each day's return with contributions removed, so
it says how the investments performed rather than how much was fed in; it runs
higher for a portfolio built by regular buying, because the early money has
compounded for years. Quoting either alone invites the wrong conclusion. A
simple "gain / invested" across two windows divides by different denominators,
so a 5-year and an all-time figure are not comparable.

**96. `rp_id` is declared in config and never read off a request header.** Host
and X-Forwarded-Host are client-controlled, and the rp_id is the anchor every
WebAuthn credential is permanently bound to — taking it from a request would
let a caller choose which domain their credential counts for.

**97. The CSP carries `unsafe-inline`, and that is stated rather than hidden.**
The templates still carry inline blocks and `onclick`/`onchange` handlers, so
dropping it breaks the pages. With it present the CSP is not the XSS backstop
it looks like: the real defence is Jinja's autoescaping plus `|tojson` for data
blocks. Extracting those fragments is what earns the strict version.

**98. The OCR end-to-end document is generated at test time, not committed.**
It is drawn from the redacted sample already shipped for the `generic-au`
template and saved with no text layer. A committed PDF cannot be read in a
diff, carries metadata and embedded fonts nobody reviews, and raises a
licensing question about somebody else's document in an AGPL repository.
Generating it also tests the layout that actually ships, since the sample is
the layout.

**99. A broker import creates instruments it does not know, and asks first.**
Refusing was the old default, and it makes the first import of an install
impossible: every ticker in a broker's own export is unknown at that point, so
the answer was a config edit before anything could be imported at all. What
stops a typo becoming an instrument is the preview's resolve step — the new
tickers grouped by instrument, each with its exchange, editable, and rechecked
against the database on demand. That also settles the case a flag cannot: one
ticker listed on two exchanges, where the file is right and the exchange is
what needs correcting. `annotate()` is re-entrant for this reason; a stale
"unknown-instrument" would keep refusing a ticker that now resolves.

**100. A DRP allotment with no price is refused, not booked at zero.** Some
exports list the allotment as a row with units and no price — the registry
bought them out of the distribution, and the cost base is in the statement
rather than the export. Importing it at zero understates that parcel's cost
base and overstates the gain when it is sold, which is a tax figure. The row is
skipped with the reason, and the dividend statement is the path that carries
the number.

**101. The broker mapper is drag OR tap, and the selects stay the state.**
HTML drag-and-drop does not fire on touch at all, and the layout is checked at
412px, so a drag-only mapper is one that does not work on a phone. Dragging a
chip, tapping a chip then a heading, and choosing from the list all do the same
thing: set a named `<select>` and fire `change`. That is also what makes the
page work with JavaScript off — the list is what a plain browser gets, and the
trays reveal themselves only once the script runs.

**102. Words the app already understands are shown, not flagged.** `Buy` and
`Sell` need no mapping, so outlining them beside a genuinely unknown word says
the whole column needs attention when two rows do. They render greyed with
their resolved type; only a word nothing can read is outlined.

**103. Clearing a mapping is its own control, and tapping a chip re-arms it.**
A tap that both assigns and un-assigns is a mapping lost to a stray touch, and
long-press is invisible, has no keyboard equivalent and fights the operating
system's own context menu. So an assigned chip carries an explicit ×, and
tapping the chip itself arms it — which is also how a field MOVES to another
column without being cleared first.

**104. A column is claimed by at most one field.** Two fields reading the same
column parses without complaint and produces trades whose price is their
brokerage, so taking a column takes it from whoever had it. `guess()` already
worked this way; assigning by hand now does too.

**105. The import pages carry the origin in a hidden field, not a query
string.** They are reached by POSTing a file, so there is no `?return=` to
read. The value goes through the same `navigation.safe_path` guard as every
other back link, because it ends up in an href — `//evil.test` and
`/\\evil.test` both look like paths and neither is one. `safe_path` and
`BACK_LABELS` live in `navigation` rather than `main` so the imports router can
reach them: a guard that is copied is one that will eventually be copied
slightly wrong.

**106. The Unraid template maps ONE folder and sets a restart policy.**
`/data` and `/config` are a real distinction on Kubernetes — a writable volume
and a read-only ConfigMap — and no distinction at all on Unraid, where both
were the same appdata directory. Mapping it twice asked one question twice and
let somebody answer it two different ways, with the database and the config
ending up in different folders and a backup of "the appdata folder" missing
one. `APP_CONFIG_FILE` points inside `/data` instead.

dockerMan also creates containers with no restart policy, and `lifecycle`
restarts by STOPPING and relying on the runtime — so the setup wizard's final
step stopped the app and it stayed stopped. `supervision()` says so on the
page, honestly, but an honest warning is not a working restart. Both shipped
compose files already set `unless-stopped`; the template was the only target
that did not. `unless-stopped` rather than `always`, so a stop from the Unraid
UI is respected.

**107. The app refuses every page while a wizard is in progress.** The database
and the account exist from step 3 onward — the account has to be written
somewhere — so from there a session exists and every route answers. Deleting
`/setup` from the address bar then walked into a half-configured app: no
timezone, no portfolio, no config file written. Deferring the database until
the end is not the alternative it looks like; step 3 needs somewhere to put the
account. Setup is finished when the draft is discarded, so that is the signal.
The redirect goes to the step the wizard is ON, not to `/setup` — welcome
closes the moment an account exists, and sending somebody to a closed step
bounces them to /login, which bounces them to /, which arrives back here.
`/logout` stays open, because abandoning a wizard is a legitimate thing to
want.

**108. The "no database" check runs BEFORE the public-path list, not after.**
`/login` is public and opens a session to look for the account, so an
unconfigured app answered it with `NotConfigured` — a 500 on the one page every
redirect lands on, which reads as an app broken beyond recovery rather than one
asking to be finished. Only static files, the health probes, the wizard and
`/metrics` answer with no database at all; everything else, `/login` included,
is sent to the wizard.

**109. `next_step` derives from STEPS; a back button never offers a closed
step.** Two halves of one loop. `next_step` read from its own hardcoded list,
which still carried "2fa" after that stopped being a step (#49) — harmless
while nothing turned the answer into a URL, and a 404 the moment something did.
It reads STEP_KEYS now, so it cannot name a page that does not exist. And
`welcome`, `database` and `account` shut once an account exists: offering one
as a back target sends you to /login, which sends a signed-in user to /, which
sends a wizard in progress back to the wizard. Three redirects that each look
reasonable. The recovery step therefore offers no way back at all, which is the
answer rather than an omission.

**110. A `<details>` cannot live in a `<p>`.** It is block-level to the HTML
parser whatever its CSS display says, so the paragraph is closed before it and
the (?) lands on its own line — the tip's parent came back as `<section>`. Use
`.tipline` for running text that carries one. And `vertical-align: middle`
aligns an inline-block with the baseline plus half the X-HEIGHT rather than the
optical centre, so a circle taller than the x-height sits low: measured 2.9px
on a 1.15rem (?) beside 1rem text, corrected in em so it scales.

**111. The codes step re-shows the same codes rather than minting new ones.**
It regenerated on every view, which is silent invalidation — write them down,
press Back from the next step to check a character, and the list in your hand
is dead with nothing on screen saying so. They are issued once per wizard and
held in the draft; a fresh set is a deliberate act from the account page. Found
by rendering the two-factor step and following its own back link.

**112. Finishing setup restarts, without asking.** It was a ticked checkbox,
which made "Finish setup" able to mean "finish setup and ignore the settings I
just chose" — the restart is what picks them up. It is now stated rather than
offered, and only appears when there is something to pick up.

**113. A passkey signs in on its own, with no password and no code.** The
ceremony is run with user verification REQUIRED, so the authenticator proves
both possession of the device and a fingerprint, face or PIN before it will
sign anything. That is already two factors, and stopping to ask for a TOTP code
would be asking for a third — while a password user is asked for two. The
requirement is enforced on the assertion, not merely requested in the options:
an authenticator that answers with User Present alone is refused, because
without it the assertion would prove only that somebody was holding the phone.

**114. Signing in with a passkey asks for no email.** The ceremony is
discoverable — the authenticator names the account — so nothing is typed and
nothing is confirmed. Sending `allowCredentials` would mean naming an account's
keys before anyone had proved anything, which answers "does this address have
an account here" to whoever asks.

**115. The WebAuthn challenge lives in a row, not a cookie.** It is stored
hashed and deleted as it is read, exactly like a session token: a ceremony is
two requests, and the nonce between them is what stops a captured response
being replayed. A challenge that survived being used would make the signature
worth nothing.

**116. `/invite/` is public, and the token is the whole authorisation.** The
person opening an invitation has no account yet, so the login middleware has to
let them through — and every route under the prefix checks the token itself.
Signing up is possible ONLY with a live one: without that check the endpoint is
open registration on an app holding somebody's holdings.

**117. An invitation never changes an existing membership.** Accepting one when
already a member consumes the invite and leaves the role alone, so a viewer
link sent to an owner cannot quietly demote them. It is still consumed, because
it was offered and answered — a spent link that still worked would be a live
credential nobody thinks they have.

**118. Invitations are not an OIDC feature.** They were built for it — a
provider can only auto-provision somebody who is authorised to exist — but
membership needed them anyway: adding a member required the account to exist
first, so there was no way to bring in somebody who had never signed in. An
install with no identity provider gets the same link.

**119. An OIDC identity is matched on `(issuer, sub)`, never on email.** An
address is something a directory can often be *told*, so matching on it would
let anybody who can set an email in the provider sign in as an existing local
account. Provisioning refuses a colliding address rather than adopting it: two
people are not one person because a directory says so. Linking an existing
account to a provider is a deliberate act by somebody already signed in.

**120. `user.password_hash` is nullable, and NULL is not a password.** An
account provisioned by a provider never had one. `verify_password` already
returned False for a NULL hash — that was written for the
account-does-not-exist case and turns out to be exactly right here — and
unlinking the last identity is refused while the hash is NULL, because an
account with no way in is not something a settings page should be able to
create quietly.

**121. A public client is asked for, not deduced from a missing secret.**
`auth.oidc.client_auth` is `basic` or `none`, and it defaults to `basic`.
Reading the client type off an absent `client_secret` would be less to
configure, but it makes a credential going missing indistinguishable from a
deployment choice — the token request quietly stops authenticating and nothing
says so. `DatabaseSettings._guard_ambiguous_sqlite` refuses the same shape for
the same reason: an unset field selecting a different mode. So only the exact
value `none` waives the secret — a typo still requires one, and a `basic`
client with nothing set refuses rather than sending an empty password.

The asymmetry is deliberate: `none` with a secret still in the file **ignores
it** rather than refusing. That is what moving a confidential client to a
public one looks like halfway through, and refusing would strand a deployment
between two working states.

**122. `appcore/` names no application configuration key.** Its modules are
written to be lifted into applications that have not shipped yet, so an error
telling somebody to set `auth.oidc.client_secret` is wrong the moment it is
used by an app that calls the setting something else — and wrong in the way
nothing catches, because the sentence still reads fine. The protocol layer
names the *condition*; the caller names the settings that fix it, because only
the caller knows what they are called. Raised by review on #25, where the same
misconfiguration had two messages that disagreed about how many ways out there
were.

**123. Removing the last member offers to delete the portfolio.** They are one
decision, so they are one action. A portfolio is reachable only through
membership — `load_auth` builds the list from `portfolio_member` and there is
no admin override — so removing the only member would leave one holding every
trade and visible to nobody, admins included. It cannot be restored from the
outside, only by re-adding a member nobody can see.

Offered to the **owner**, not gated on admin. The only person who can be in
this position is the sole member of a portfolio nobody else can reach, so
requiring an administrator adds no protection — there is nobody else to
protect — and would leave a non-admin unable to tidy up their own, with no one
able to help them. The confirmation and the tickbox are the guard rails.

**124. A price of zero is a real trade, and the form asks for the trade's
date.** Zero was refused everywhere except the importers, which have always
accepted it — so a bonus issue or an employer's grant could be imported but not
typed. There is nothing in the database to relax: `quantity > 0` is a CHECK
constraint, `unit_price` never had one. Every percentage already guards its
divisor (`gain / cost if cost else None`, `percentage_applies`), so a zero cost
base reports N/A rather than dividing — which is the right answer, not a gap.

The second half is the reason this is here. The instrument picker used to carry
each instrument's **latest** close and rate, and choosing one wrote them into
the form. That is right only for a trade recorded today; on a backdated one it
books the wrong cost base, and the wrong FX rate *permanently* — the price
feed's repair pass fills `fx_rate IS NULL` and skips everything else, so a
prefilled wrong rate is the one thing nothing will ever correct. Both figures
now come from `/holdings/price` for the date on the form, and the option data
was **deleted** rather than fixed: a field with two sources eventually takes
the stale one again.

Where nothing is stored for that date the fields are left EMPTY, and
`queries.close_on_or_before` reaches backwards only. This is deliberately
narrower than `FxBook.rate`, which reaches forward to the earliest stored rate
when a *report* asks about a date before the series — a report must produce a
figure or omit the holding, whereas a form can ask the person to read their
contract note. It is #5 applied to an input rather than an output: an empty
rate is repairable, a plausible wrong one is not.

Zero is still not the right cost base for an employee share scheme, which is
what prompted the change (issue #35). The ATO resets it to market value at the
taxing point, and the taxing point also restarts the 12-month discount clock.
That belongs in [the guide](../guides/free-and-discounted-shares.md), not in a
validator — the app takes the number it is given.

**125. A move between portfolios is one column, and the hard part is deciding
what goes with it.** Reported (#36) on the assumption that the destination
needs its own instrument row and the trade details copied onto it. It does not:
`Instrument` is a shared catalogue, unique on `(exchange, ticker)` and
deliberately not portfolio-scoped, so the move is `portfolio_id = target` and
nothing is copied.

Three rules decide what travels, and only two of them are enforced.

*Balance* pulls rows in. Take a buy out and a sell left behind can exceed what
is still held, and `balance_breach` reports only the FIRST such point — so the
set is a fixpoint, not a query. **It can only ever add a sell**: `_walk` drives
the running total down on sells alone, buys and DRPs add to it, and
`quantity > 0` is a CHECK constraint. That is why one coupling pass is enough,
which is otherwise a very easy thing to get wrong in the safe direction.

*Pairs* are inseparable. A reinvested distribution is one event across `trade`
and `dividend`, joined by `reinvest_trade_id`, and every reader assumes the
halves share a portfolio. So the dialog offers a pair as ONE row — the `drp`
trade, which carries the units — and the coupling is applied server-side in
both directions anyway, because the posted ids are user input and must not be
able to split it. This is also why the move does NOT go through
`_trade_for_edit`: that refuses a DRP trade, correctly, because editing half a
pair leaves the units and the cash disagreeing. Moving both halves does not.

*Cash* is offered and never assumed. A distribution does not touch the unit
balance, so nothing forces it — but a portfolio that no longer holds the units
has no business holding the income either, so it is on the list. Which DRP buy
"belongs" to which portfolio is likewise not derivable: a DRP arose from units
held at a record date, and once those units are being split the attribution is
a judgement. A person decides; the app enforces only what would otherwise leave
the data inconsistent.

**The pre-tick is a default, not a decision.** `expand(pull_in=True)` fills the
dialog's tickboxes with the rows that cannot be left behind; the submitted form
uses `pull_in=False`, because re-running the pull-in there would silently
restore anything somebody unticked and make the page do the opposite of what it
was told. Unticking is honoured and then REFUSED, by `source_problem`, naming
the sell that would be left short. Pairs are coupled either way: unticking a
balance dependent is a judgement somebody may make and be refused for, while
splitting a reinvested distribution is not a judgement but a shape the data
cannot hold.

Neither side is ever auto-corrected. A sell moved somewhere holding nothing
goes negative there, and the fix is to tick more buys — a choice, so it refuses
and names the trade. Reading the destination at all needs
`tenancy.as_portfolio`, which is narrower than `unscoped_session` on purpose
and **checks the membership itself**: a primitive that rebinds the filter on
request is a cross-tenant read waiting for one careless call site, so the
authorisation lives with the scope change rather than in whichever template
decided to draw a button.

A financial year already lodged is explicitly NOT a reason to refuse. Moving a
trade can change a prior year's realised gain through FIFO re-ordering, and
that was considered and rejected as a guard: the app computes from the data it
holds, a return asks the person filing it to attest to what they supply, and a
tool cannot un-lodge anything. See [the disclaimer](../about/disclaimer.md).

A `PlannedPurchase` whose trade leaves goes back to **planned**, unlike the
delete path which keeps it at "done" because a deleted trade still happened.
A moved trade did not happen *here*, so the slot is genuinely unfilled and the
schedule should offer it again.

**126. The FX leg asks only for days that have finished.** A live pod logged
`$USDAUD=X: possibly delisted; no price data found (1d 2026-09-24 ->
2026-09-25)` on every startup. Nothing was delisted and nothing was missing:
the series was current to the 23rd and the feed was asking for the 24th, a day
that had not closed. Yahoo has no answer for an unfinished day and says so in
the only vocabulary it has.

The price loop has never done this — it stops at `settled_through(exchange)`,
the last date that cannot still move. The FX loop ran to `today`, so the first
run of any day asked for that day.

`fx_settled_through` is separate from `settled_through` rather than reusing its
fallback, and the reason is the weekend. That fallback answers `today - 1` for
a market with no known bell, which on a Monday is a Sunday — a day with no rate
at all, producing the same error for a different reason. So it rolls back over
the weekend the way `last_close_date` does for a market it knows.

Worth fixing for the log rather than the data: the stored series is unchanged,
because a rate for an unfinished day was never going to arrive. But "possibly
delisted" at ERROR is indistinguishable at a glance from a pair the app has
genuinely lost, and a log that cries wolf daily is one nobody reads on the day
it is right.

**127. A back-channel logout ends sessions and nothing else.** Issue #28. A
provider that ends a session POSTs a signed `logout_token`; appcore verifies it
and `federation.end_sessions_for` acts on it.

It **never** sets `is_active = False`, and that is the whole shape of the
feature rather than a missing half. Letting a provider disable an account here
is a far larger authority than ending a session, and it would mean a provider
outage could lock everybody out of their own records. So somebody holding both
a password and a provider identity is signed out and signs straight back in
locally. It is **session propagation, not revocation** — and the app already
has revocation, which is stronger and needs no provider: disabling a user calls
`destroy_sessions_for()` and `load_auth` deletes an inactive user's session on
their next request.

Keyed on `sid`, falling back to `sub`. `sid` ends exactly the session the
provider named; `sub` ends every session that identity holds, which is more
than it asked for and all that can be done with a token carrying only a
subject. The `sub` path matches on `(issuer, subject)` and never on subject
alone — subjects are unique only *within* a provider, and an installation that
changed provider keeps the old `external_identity` rows.

That `sid` needs storing is what crossed a boundary. `oidc.Identity`'s
docstring says claims beyond its five stay unread, so `session_id` was added
there deliberately: `sid` is not a claim about the person but an identifier for
the provider's own session, and the boundary exists to refuse *authorisation*
decisions taken from claims — groups mapped to roles — which this is not.

**The endpoint is unauthenticated, and that is the specification.** No cookie,
no CSRF token, no API key: the signature on the token is what proves the caller
is the provider. That is also what lets a public client use it, which is how
the reporter's deployment is configured. Two checks stop an ID token being
accepted as a logout instruction — a required `events` member and a forbidden
`nonce` — because an ID token carries the same signature, issuer and audience
and is the one an attacker is likeliest to have seen.

`jti` is required and not remembered: replaying an ID token would be a sign-in,
whereas replaying a logout token ends a session that is already ended.

The `sid` lookup deliberately does **not** filter on `via_oidc`. `oidc_sid` is
written in exactly one place and always alongside it, so a local session holds
NULL and cannot match; the clause was there first, was unreachable, and a guard
no test can fail implies a threat that does not exist. The `sub` path does
filter on it, and that one is load-bearing.

**128. An API key is its creator's, and does no more than they can now.**
GHSA-cjcx-g69x-63jq, from freeb5d. Deactivating someone or removing them from a
portfolio ended their sessions and left their keys working. The reporter asked
which model was meant: keys owned by the portfolio, or by the person who made
them. The person: they saw the raw key when it was issued, so a key that
outlives their access is their access, kept. Calling it the portfolio's does
not change who holds the secret.

So keys live on the profile page, not the Members page, and anyone with access
can make one — a viewer's key reads what the viewer can already read and
export. A key reaches the portfolios chosen when it is made, picked from the
creator's own (`api_key_portfolio`). One given more than one is told which on
every request with `?portfolio=<id>`; `/api/v1/portfolios` lists them.
Whether that parameter is required depends on how many the key was GIVEN, not
how many it can still reach, so a client's contract does not change shape
because somebody's membership did. An id the key was not given is a 404,
answered the same as one that does not exist.

Checked on every use (`auth.key_reach`, `auth.api_session`), not done by
revoking keys when someone leaves. Revoking would have to be remembered at
every way access ends (deactivation, removal, demotion to viewer) and would
miss the next one added. A creator who is deactivated or gone makes the key a
401 everywhere; one who left a portfolio makes it a 403 there and nowhere
else; one who became a viewer makes a write key read only there. It runs the
other way too: restore the access and the key works again, because nothing was
revoked. The profile page says, per portfolio, where a key has stopped or been
limited. The raw key is shown once in the response that made it — it used to
travel in a redirect's query string, and so in browser history and the proxy's
log.

**129. A release written by hand keeps its notes.** Publishing a release in
GitHub's UI is how releases are usually made here, and it pushes the tag, so
the release already exists when `release.yml` runs. The step that writes notes
(`softprops/action-gh-release`) keeps an existing body only when it is given
none of its own, and it always is. v0.67.0 lost its hand-written Security
section that way, advisory links and all, ten minutes after it was published.
So the workflow looks for a release first, drafts included, and writes one
only when there is none: a tag pushed from the command line still gets the
generated notes and the `docker pull` line. A hand-written release carries
whatever its author put in it, so include the `docker pull` line there. The
lookup failing stops the job, because reading a failure as "no release" is
the overwrite again.

**130. A currency is a three-letter code, and the pages treat data as text.**
GHSA-g75f-hwjq-whr6. An instrument's currency was free text in three places
(adding a holding, the trade form's new instrument, the broker designer) and
only upper-cased, and SQLite does not enforce `String(3)`. The chart builder's
filters and the chart tables built HTML from strings, and the CSP allows inline
handlers, so markup typed as a currency ran for whoever opened a chart next.
Instruments are shared by every portfolio on an instance, which made that any
writer's script in anybody's session.

Two fixes, because either alone leaves a gap. The way in: `currency_problem`
at each route, a `BrokerFormat` validator for formats from config, templates
and the designer, Yahoo's suggestion held to the same rule, and a model
validator under all of them that refuses rather than normalises. The way out:
the builder, the chart tables and the rotation editor build elements and set
text, and a table cell is markup only when `charts.js` made it so. The second
half is what protects an install that already stored a bad value. A bad
broker format already installed is skipped with a warning rather than taking
the imports page down.

**131. Typed text is refused, never cut; values the app picks are cut to fit.**
Issue #60. The inputs carry `maxlength`, which a direct POST ignores. SQLite
stores any length in a `String(n)` column and Postgres refuses with "value too
long", so the same request was a 500. `textfield.fit` checks the length against
the column, which it reads from the column so it cannot drift from the schema,
and refuses, saying how long the text was and how long it may be and none of the
text: a form carries the message in a query string, and a pasted page would make
that URL too long for the proxy. A note somebody typed is theirs, and a cut one
is a sentence they did not write.

The exceptions are values the app chooses itself, where there is nobody to ask:
"Alex's portfolio", an API key's fallback name, the User-Agent, and a blank name
that falls back to the email. Those are cut with `[:n]`, because refusing would
block what the person actually did. Empty text keeps each site's own rule: a
note becomes `None`, a user's name falls back to their email, a plan's to "My
plan".

Two details that were found by running it. An email is measured after
`.lower()`, because that is what is stored and some characters grow ("İ".lower()
is two characters). And the sign-in and recovery forms refuse an over-long email
before it reaches `login_attempt`, with the answer they give for any wrong email,
because anyone can post there and a 500 would be a way to cause errors without
an account. Adding a text field means `textfield.fit` at the route and a test
that asserts nothing was saved: SQLite cannot show the failure, and CI's
Postgres run is where it would.

**132. Every route is walked with values a form cannot send.** Issues #53 and
#60 were the same bug in different fields: a value the page's own form could
not produce got past the server. Fixing fields one at a time leaves the next one
open, so `tests/test_hostile_input.py` takes its routes from the app rather than
from a list, and checks the database rather than the status code, because
SQLite stores what Postgres refuses. A careful control request goes first and
last on every route, because a walker whose requests are all refused at the
door passes without having tested anything.

Its first run, on top of #61's fixes, found 101 more: NUL characters (which
Postgres refuses even in a lookup), ids past what a column holds, values from
Yahoo or an identity provider past their column, and assorted crashes. They are
listed in `KNOWN` and fixed in follow-ups. The list fails when an entry stops
happening, so it can only shrink, and a new finding never goes on it.

**133. What any request can carry is refused once, at the edge.** Three of the
walker's findings (#132) were not about any one field, so they are handled in
one place each rather than per field, which is how #60's gaps happened:

- **A NUL character.** Postgres refuses text containing one even to look
  something up, so it was a 500 wherever one reached a query. `refuse_nul`
  answers 400 for one in the path, the query or a form or JSON body. An upload
  is let through, because a file is bytes and a PDF is full of NULs.
- **An id no row can have.** From 2**63 the lookup itself failed, on both
  backends. Every id a route takes is a `models.RowId`, bounded by what an
  INTEGER column holds, so FastAPI answers 422 first. In a form it has to be
  `Annotated[RowId, Form()]`: `RowId = Form(...)` replaces the bounds and
  drops them without a word.
- **A bare NaN in a JSON body.** Python's parser accepts one, and FastAPI's 422
  echoed it back and then could not encode it, so a request rightly refused
  became a 500. The validation handler gives a non-finite number back as text.

**134. What a provider says is held to its columns too, and cut only where
cutting is harmless.** The walker (#132) sent Yahoo's lookup and an identity
provider's claims the same values as a form, and found each of them reaching a
column it did not fit. #131 says values the app picks are cut to fit, but that
only works for a value that is still itself when cut:

- **A name is cut.** Yahoo's instrument name (in `pricefeed.lookup`, once, for
  every caller) and a provider's display name, with any NUL removed.
- **An email is never cut**, because a shorter address is somebody else's. A
  new account whose email cannot be stored is refused with a message. A
  returning identity keeps the email already on file, because an email only
  describes them and is no reason to lock them out.
- **A subject is never cut**, because the subject is the identity. One that
  cannot be stored refuses the sign-in, checked before anything is looked up
  (Postgres refuses a NUL even there) or made.
- **A provider's session id is dropped** when it cannot be stored. It exists
  for back-channel logout, and without it that one session ends only here.

And the callback rolls back whatever a refused sign-in started, so an account
made for an identity that then fails to link is not left behind with no way in.

**135. A plan starts within the next ten years.** A plan saved with a start in
9999 was accepted, and then `/schedule` failed on every load, overflowing the
date arithmetic that draws the rotation. That is the page where the plan would
be edited, so the portfolio was stuck until somebody changed the database. The
start date anchors a rotation that may have begun years ago, so the past is
left open. The future is bounded at `PLAN_START_YEARS_AHEAD`, ten years, far
past any plan anybody makes and well short of the overflow.

**136. A migration on SQLite runs in one transaction, so a failure leaves
nothing behind.** pysqlite opens a transaction only before INSERT, UPDATE and
DELETE, so Alembic ran SQLite's DDL outside one ("Will assume non-transactional
DDL"). On 2026-10-03, 0010 met a database whose index had an older name: it
created `api_key_portfolio`, failed to drop the index, and kept the table, so
every restart failed "already exists" until the database was repaired by hand.
SQLite can roll DDL back; it was the driver that stopped it. `env.py` now turns
the driver's transaction handling off, emits BEGIN itself, and tells Alembic
the DDL is transactional, so a failed upgrade leaves the database at the
revision it started from and fixing the cause is enough. The cost is that no
migration may need to run outside a transaction: `PRAGMA foreign_keys` is
ignored inside one and VACUUM refuses to run, and none of the current ones do
either.

**137. The release's tag is the only place a version is written.** The Helm
chart picks the image it runs from its `appVersion`, which was kept by hand in
Chart.yaml. It said 0.64.0 for every release from 0.64.0 to 0.67.1, so every
Helm install ran 0.64.0 however often it was upgraded, without the fixes four
advisories told people to upgrade for. `pyproject.toml` said 0.64.0 beside it,
and the test comparing the two passed because neither had moved. Now the
release packages the chart with the tag as its version and appVersion and
pushes it to ghcr.io, and the project reads its version from the tag through
hatch-vcs. The chart in the repository names no release and refuses to render
without `image.tag`, rather than run an image it was not given. A release
needs no commit beforehand; the cost is that the chart is installed from the
registry, not from a clone.

**138. A provider sign-in does not ask for Stocktake's own code.** An account
with 2FA on gets the code step after a password, but not after signing in
through its identity provider: `login_oidc_callback` creates a full session.
The provider authenticated the person, and its own multi-factor policy is
where an organisation running one enforces a second factor; asking again would
give the people who chose a provider two second factors for one sign-in. It is
the reasoning a passkey already follows (#113). The cost is that turning on
OIDC trusts the provider as far as the app's own 2FA, so use one that enforces
MFA. `test_a_provider_sign_in_does_not_ask_for_the_code` pins it.

**139. A portfolio is shown its own instruments, and removes only its own.**
The catalogue stays shared (#125): one row per exchange and ticker, with one
price history. What changed is what a portfolio sees and can do with it.
Manage holdings and the trade and plan dropdowns list the instruments this
portfolio has — a holding setting, a trade, a dividend or a place in its plan —
where they listed the whole catalogue, which showed every portfolio the tickers
the others had added. Remove drops this portfolio's setting; the feed stops
fetching an instrument only once no portfolio has it, which is asked after the
removal commits, because the question goes through a session of its own. Only
this portfolio's trades and dividends block it, and another portfolio's are
not counted on the page. One in the plan comes out of the plan too, with a
warning rather than a refusal; the rotation closes up and carries on from the
last buy still in it, because the slots left keep their rows. A ticker another portfolio added is reused through
"Something not listed", and the add form says "added" either way. "Delete all
trades" clears this portfolio's trades and distributions for one instrument and
leaves it listed, so removing it stays a step of its own.

**140. A password is 10 to 64 characters; how strong it is gets shown, never
enforced.** The length is the only rule, at every form where a password is
chosen and never at sign-in, so an account with a longer one from before still
gets in. No mix of character types: NIST 800-63B-4 and OWASP ASVS 5.0 both
forbid it, and on a self-hosted app a gate on strength mostly produces
`Password1!`. No banned list either, though both ask for one: the meter shows a
common password as Very weak, and the person decides. The meter under each
chosen password is
zxcvbn (Dropbox, USENIX Security 2016), which estimates guesses from what people
actually choose, the app's name and the person's own email included; it is a
word and a bar, and a weak password still saves. zxcvbn is 800 KB, so it loads
on the first keystroke rather than with the page. Admins' temporary passwords
get the cap but no meter: the person replaces them at first sign-in.

**141. A sign-in opens the default portfolio, else the last one opened.** Every
way of signing in goes through `_landing_portfolio`: the portfolio chosen in the
profile, else the one in a cookie set whenever a portfolio is switched to,
created or joined and whenever a sign-in lands, else the first one owned. A
password still waiting on its code sets nothing, so it learns no id. The cookie
holds the portfolio's id and nothing else, lives a year from the last time it
was set and survives sign-out, which is the point. It
is a hint, not a credential: both it and the default count only while the
person is a member of that portfolio, so a cookie naming someone else's opens
nothing of theirs, and a value that is not an id is ignored. Its name follows
the session cookie's, so two apps on one host keep theirs apart. The profile
offers the choice only to someone with more than one portfolio. An invite still
opens the portfolio it invited to.

**142. An export's text is shown by a spreadsheet, never run.** Notes, names and
Yahoo symbols are typed by people, and the catalogue is shared, so a writer in
one portfolio reaches another's export, where a cell starting with `=` is a
formula: a `HYPERLINK` that sends the sheet's numbers elsewhere
(GHSA-w8x6-x99c-773x, from freeb5d). Both renderers neutralise it, not each
report, so a report added later is covered too. CSV has no cell types, so text
starting with `=`, `+`, `-`, `@`, a tab or a carriage return gets a leading
quote, OWASP's rule, and the quote shows. XLSX has them: every text cell is
written as text, which is shown and never evaluated, with nothing added.
Numbers are never touched, so a negative figure keeps its sign.

**143. A holding's shared details can be corrected where that reaches nobody
else.** Name, class, currency and price symbol are the shared catalogue's, so
changing one changes every portfolio that holds the instrument. They are
fields on Manage holdings only for an instrument this portfolio alone has, or
for an instance admin; elsewhere they are plain text. "Has" counts a holding
setting, a trade, a distribution or a place in a plan, asked across all
portfolios and answered as ids alone, as `_anyone_has` is. A new price symbol
keeps the prices already stored and fetches its own from the next run, and a
blank one is the guess, as when adding. This replaces the "no way to edit a
class" of 2026-10-09: test users needed to fix a NASDAQ stock saved as
MSFT.AX. A row only saves for an instrument this portfolio has. A new currency
forgets the rates recorded under the old one, in every portfolio: an AUD row
holds 1, and kept under USD that 1 books US dollars as Australian (#5). They
become 1 again for AUD, and otherwise unknown until the feed fills them from
the stored series.

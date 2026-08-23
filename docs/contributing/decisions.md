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
appkit derives the SQLite path from `app_name`, so a rename points a running
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

**39. Two-factor enrolment is two requests, and the unconfirmed secret never
touches the user row.** Generating and showing a secret, then confirming with a
code derived from it, is what stops somebody enabling 2FA with a secret they
never successfully scanned and locking themselves out of their own portfolio.
The pending secret rides in the form, so an abandoned enrolment leaves nothing.

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

**49. Two-factor is not a setup step, and that has a cost worth naming.** It
moved to the account page in 2026-08-08, where `/profile/2fa` had always been
the way to turn it on — a wizard step was a second door to one room, and a stop
in a flow somebody is trying to finish. The cost is that **a step is a prompt
and a toggle is not**: somebody who would have set it up when asked may never
open the page. The finish step carries a line about it for exactly that reason.

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
one. `conftest._no_network` blocks both that and `providers._get_json`.

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
skipping the next step survivable. Shown once.

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

**79. Four things the second factor gets right that are commonly got wrong.**
The secret is stored as issued, in the clear — encrypting it needs a key in the
process that reads it, and anyone who can read that column can already read
every holding (#63). The QR is rendered locally, because the obvious shortcut
sends the secret to a third party in a URL. One step of clock drift is accepted:
stricter generates support requests, looser widens replay for no gain. Recovery
codes are hashed like session tokens and marked used rather than deleted, so a
replay is distinguishable from a code never issued.
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


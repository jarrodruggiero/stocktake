# House style

Ordinary Python, `ruff`-clean, 92-ish columns. The only unusual thing about
this codebase is what it expects from comments.

## Comments carry the "why"

Do not write comments that restate the code. Write the ones that explain a
decision the code cannot show:

```python
# BAD — says nothing the line doesn't
# increment the counter
count += 1

# GOOD — the reason is not in the code
# Commit per instrument. SQLite allows one writer, and this loop spends most
# of its time on network calls; holding the write lock across all of them
# blocked page loads with "database is locked" (busy_timeout is 5s, a full
# run is far longer).
session.commit()
```

The test is simple: **if someone could reasonably change this line and be
wrong, say why.** If the code is obvious, say nothing.

Only two things earn a comment:

1. **Code a competent reader would find genuinely hard.** Not unfamiliar —
   hard. Cumulative apportionment, the fingerprinted cache, the tenancy event
   hook.
2. **A decision that looks wrong until you know why**, usually one made to fix
   a specific problem. Name the problem in a sentence.

Everything else earns nothing: getters, loop counters, a name that already says
it, and anything a reader learns faster from the code than from the sentence
above it.

**Keep them short.** Two or three lines. A comment that runs to a paragraph is
usually a decision record wearing a comment's clothes — put it in
[`decisions.md`](decisions.md) and leave a pointer:

```python
# Resumes from the last SETTLED close, not the last stored one, so a live
# price written mid-session is always replaced. See decisions.md #14.
```

The failure mode to avoid is a file where the prose outweighs the code and a
reader skims both. At one point this codebase was 29% comment by line; most of
that was narration, and narration is what makes the two comments that matter
invisible.

## Docstrings say what a thing is for

Module docstrings are worth writing properly. Several here are the best
documentation of a subsystem that exists — `tenancy.py` explains the trust
boundary, `providers.py` records which price sources were rejected and why.
If you build something whose design took thinking, write that down at the top
of the file.

Test docstrings should say **what would be broken if this test did not exist**.
"Tests the balance guard" is noise. "The obvious wrong way to fix same-day
selling is to stop checking; this catches that" is the reason the test is
there.

## Naming

- Functions say what they answer: `balance_after`, `session_expired`,
  `loaded_heavyweights`.
- Booleans read as claims: `is_enabled`, `can_write`, `awaiting_totp`.
- Private helpers take a leading underscore and may be terse; anything
  exported gets a full name.
- Prefer a slightly long name over an abbreviation. `fx_book` not `fxb`.

## User-facing text

Written the way you would say it, and it must say what to do next:

> That change would leave ALPHA at −50 units from 2024-03-10: the sell of 100
> units on 2024-03-10 would then be more than the 50 units held. Edit or delete
> that sell first.

Not "Validation error: negative balance". The person reading it is trying to
fix something, and the app knows exactly what is wrong.

## Errors

- Refuse with a reason, at the point the reason is known.
- Never guess at missing data. A blank cell is a question; a fabricated number
  is a wrong answer that will be totalled by a spreadsheet. This is why
  unconvertible AUD figures come out empty rather than converted at 1:1.
- Let one bad item fail without ending the batch — one dead ticker must not
  stop a feed run — but *report* what failed rather than swallowing it.

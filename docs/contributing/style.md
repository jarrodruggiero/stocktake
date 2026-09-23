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

### How much to say

**This is the single most common correction on this project.** The rule:

> **One line at the control. Detail goes behind a `(?)` or a linked guide.
> Nothing at all for things people already understand.**

The reader is a self-hoster who chose to run a share-portfolio tracker. They
know what two-factor authentication is, what dollar-cost averaging is, and what
"optional features" means. Explaining it reads as padding and buries the one
sentence that was worth having.

Four habits to cut, with what they became:

| Habit | Before | After |
| --- | --- | --- |
| Explaining a common concept | "Two-factor authentication adds a second step when you sign in, using a code from an app on your phone…" | "This can be enabled later in account settings." |
| Describing a refusal in advance | "The whole ANZ timeline is re-checked when you save, so a change that would leave a later sell with nothing to sell is refused and tells you which trade to fix first." | *(nothing — the refusal says this, when it happens)* |
| Restating the obvious | "Delete this trade — Removes it from the ledger." | *(nothing — the button says Delete)* |
| Narrating a form | "Name, currency and price symbol fill in from the ticker — override any of them." | *(nothing — the fields visibly fill in)* |

Say it **once**. A notice above a list does not need repeating beside every row
it applies to.

Where a thing genuinely needs explaining, `_tip.html` is the `(?)`: it holds
markup and links, and its text can be selected and copied, which a `title`
attribute cannot.

```jinja
{% set price_tip %}
<p class="tipline">One line, and a <a href="{{ docs_url }}/guides/x/">link</a>.</p>
{% endset %}
{% with body = price_tip, label = "What this means" %}
  {% include "_tip.html" %}
{% endwith %}
```

And where the explanation is longer than a `(?)`, it belongs in
`docs/guides/` with a link to it — never inline on the page.

## Controls

**Primary is the one committing action of a form.** `.secondary` is everything
else: cancel, back, opening another tool, an auxiliary action. `.danger` is for
something that destroys data. One primary per form.

**A link that acts gets wrapped in a button.** Anything that changes state,
opens a dialog or navigates to a form for changing something reads as a
control, not as text that happens to be clickable:

```jinja
<a href="/trade/{{ id }}/edit"><button type="button"
   class="secondary small">Edit</button></a>
```

`button.small` exists for buttons inside a table row, where full size dominates
the data beside it. It changes size only, so it combines with `.secondary` or
`.danger` rather than replacing either.

**A row's actions go top-right, in a `.pagehead` with `.headactions`, at the
same size as every other page's.** Not stretched across the width, and not each
in a panel of its own — a single button does not need a box around it. Use
`.headactions`, not `.menuactions`: the latter puts `flex: 1` on a form so its
rows fill a menu's width, which in a page header pushes the last button off the
corner.

```jinja
<div class="pagehead">
  <div><h1>Edit {{ inst.ticker }} trade</h1></div>
  <div class="headactions">
    <button type="button" class="secondary" id="openmove">Move</button>
    <form method="post" action="…"><button type="submit" class="danger">Delete</button></form>
  </div>
</div>
```

**A dialog is for something you do without leaving the page** — Record trade,
Add an instrument, Move to another portfolio. Three rules make one work:

- `showModal()`, never `show()`. It takes focus, traps it, closes on Escape and
  dims the page behind; a hand-rolled modal gets all four wrong.
- **The dialog includes the same template the standalone route renders.** Two
  copies drift, and the route is what makes the feature work without scripting
  and gives a validation failure somewhere to land.
- A `<noscript>` link to that route, so the button is never a dead end.

**A destructive action confirms, and the confirm says what survives**:

> Delete this plan and its rotation? Buys you have already recorded are kept.

## Colour and contrast

- Never carry meaning in hue alone: gain and loss also carry ▲/▼, and status
  carries a word.
- **A QR code always gets an explicit white background.** It is scanned by a
  camera, and in dark mode an inherited background makes it unreadable.
- The accent is indigo and was chosen under a deutan simulation. Simulate
  before changing any colour that distinguishes one thing from another.

## Errors

- Refuse with a reason, at the point the reason is known.
- Never guess at missing data. A blank cell is a question; a fabricated number
  is a wrong answer that will be totalled by a spreadsheet. This is why
  unconvertible AUD figures come out empty rather than converted at 1:1.
- Let one bad item fail without ending the batch — one dead ticker must not
  stop a feed run — but *report* what failed rather than swallowing it.

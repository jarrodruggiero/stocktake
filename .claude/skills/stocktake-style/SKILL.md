---
name: stocktake-style
description: How user-facing text, buttons, dialogs and colour work in Stocktake. Use BEFORE writing or changing any template, any string a person will read, or any control — and when reviewing a change that touches app/templates or app/static.
---

# Stocktake's house style for anything a person sees

The full rules are in [`docs/contributing/style.md`](../../../docs/contributing/style.md)
and that file is the authority. This is the short version, and the part that
gets got wrong.

## The one rule that is broken most often

> **One line at the control. Detail goes behind a `(?)` or a linked guide.
> Nothing at all for things people already understand.**

The reader is a self-hoster who chose to run a share-portfolio tracker. They
know what two-factor authentication is, what dollar-cost averaging is, and what
"optional features" means. Explaining it reads as padding and buries the one
sentence worth having.

Before adding a sentence to a page, check it is none of these:

| Do not | Instead |
| --- | --- |
| Explain a common concept | Assume they know it |
| Describe a refusal that has not happened | Let the refusal say it, when it happens |
| Restate what a control obviously does | Nothing — the button's label is the explanation |
| Narrate what the form visibly does | Nothing |
| Repeat a notice beside every row it covers | Say it once, above |

If it genuinely needs saying and will not fit in one line, it belongs in
`docs/guides/` with a link — not inline.

## Controls

- **Primary** is the one committing action of a form. `.secondary` is
  everything else — cancel, back, opening another tool. `.danger` destroys
  data. One primary per form.
- **A link that acts gets wrapped in a button.** `<a href=…><button
  type="button" class="secondary small">Edit</button></a>`.
- `button.small` is for a button inside a table row. Size only — it combines
  with `.secondary` or `.danger`.
- **A row's actions go top-right in a `.pagehead`**, the same size as every
  other page's. Not full-width, and not each in its own panel: a single button
  does not need a box around it.
- **A destructive action confirms, and the confirm says what survives.**

## Dialogs

`showModal()`, never `show()`. The dialog **includes the same template the
standalone route renders** — two copies drift, and the route is what makes the
feature work without scripting and gives a validation failure somewhere to
land. Add a `<noscript>` link to that route.

## Colour

Never meaning in hue alone. **A QR code always gets an explicit white
background** — it is read by a camera, and dark mode otherwise makes it
unscannable. The indigo accent was chosen under a deutan simulation; simulate
before changing any colour that distinguishes one thing from another.

## Comments in the code

A different audience, opposite instinct: comments are *wanted*, but only for a
decision the code cannot show. Two or three lines; anything longer is a
decision record wearing a comment's clothes — put it in
[`decisions.md`](../../../docs/contributing/decisions.md) and leave a pointer
(`see decisions.md #124`).

## Before you finish

Read the page you changed, rendered. Every layout fault this project has had
was found by somebody looking at a page — `uv run pytest tests -m visual`
drives a real browser over every page at 1400px and 412px and states the fault
as a number.

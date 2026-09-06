# DCA Schedule

Dollar cost averaging is buying on a cycle rather than in lump sums. This
feature keeps track of where you are up to in that cycle.

Leave it off if you invest in lump sums. It is a planning aid — your trades,
holdings and reports work identically either way.

## How it works

You set a **rotation**: the instruments to buy, in the order you want to buy
them. The Plan page prefills a suggestion from what the portfolio already
holds, and you edit it from there.

The app then shows which instrument is next and when it is due. When you have
made the purchase, record it and the schedule advances to the next slot.

## What is next is computed, never stored

The rotation says what order to buy in, and each recorded purchase is tied to
the specific slot it belongs to. "Next" is simply the slot after the last one
recorded.

This matters when you change your mind: **inserting or reordering instruments
later does not shift the sequence.** If the position were held as a count
instead, adding a slot would silently re-point "next" at the wrong instrument.

## Recording a purchase

Recording against the plan creates an ordinary trade. There is nothing special
about it — it appears in your holdings, your reports and your capital gains
exactly like a trade entered by hand or imported from a broker.

Recording the same trade from **Record a trade** instead does *not* advance the
schedule, because the app cannot know it was the scheduled one.

Skipping a slot is recorded too, so the history stays honest about what you
actually did rather than what the rotation intended.

## Turning it off later

Admin → Settings. Switching it off hides the Plan page and its nav entry;
nothing stored is deleted, so turning it back on restores the rotation and the
purchases you recorded against it.

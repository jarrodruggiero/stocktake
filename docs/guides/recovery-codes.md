# Recovery codes

Recovery codes are how you get back into your account when the usual way is not
available. You are shown a list once, when the account is created, and each
code works a single time.

## What a code can do

- **If you forget your password** — a code stands in for it. You are asked to
  set a new one straight away.
- **If you lose your authenticator** — a code stands in for the six-digit code.

One code covers **one** of those per sign-in, never both. That is deliberate:
somebody who finds your list still cannot get into an account with two-factor
authentication switched on, because they would need your password as well.

## Where to keep them

Somewhere that is **not** the device holding your authenticator app — losing
that device is exactly when you will need these. A printout or a password
manager both work.

If two-factor authentication is **not** switched on, this list is the only
thing besides your password that can reach the account, so treat it exactly
like a password. Turning on two-factor authentication is what makes these codes
safe to keep on paper.

## If you lose the codes as well

An administrator can reset the account from the server:

```sh
python -m app.recover
```

That clears two-factor authentication as a side effect, so it is a last resort
rather than the plan.

## Generating a new list

Any time, from **Account settings**. Generating a new list invalidates the old
one immediately — so save the new codes before closing the page.

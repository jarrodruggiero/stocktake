# Security model

## What the app protects

- **Passwords** are hashed with argon2id and never stored or logged in any
  other form.
- **Sessions** are server-side. The cookie holds a 256-bit random token;
  the database stores only its SHA-256. It is `HttpOnly`, `SameSite=Lax`, and
  `Secure` when you enable `auth.cookie_secure`.
- **Session lifetime** is bounded three ways: an idle window (default one
  hour), an absolute cap from creation (default seven days) that activity
  cannot extend, and the stored expiry. A page left open warns you and then
  signs you out.
- **CSRF** — every state-changing request must echo a session-bound token.
- **Login lockout** counts failures per email *and* source address together, so
  one attacker cannot lock everyone out. It covers the two-factor code step as
  well as the password.
- **Two-factor authentication** (TOTP) with single-use recovery codes. A
  correct password alone reaches no page at all.
- **Between portfolios**, every query for personal data is filtered
  automatically. A query with no portfolio context raises rather than returning
  everything.
- **API keys** are stored as hashes, scoped to one portfolio, and read-only
  unless granted write.

## What it does not protect against, and why

This is the part worth reading.

### Anyone who can reach the database can read everything

Holdings, session token hashes, TOTP secrets, the lot. `sqlite3 stocktake.db`,
a copy of the volume, a backup snapshot, or a shell in the container all bypass
every control above.

This is not a bug to be fixed later. Real separation would need per-user
encryption keyed on your password — which would also lock out the price feed,
the reports, and the backups, since none of those have your password. The
trade was made deliberately.

**The boundary is "can reach the machine or its storage", not "has an
account".** Secure the machine accordingly.

### People you share a portfolio with are trusted

Roles (`owner`, `member`, `viewer`) stop accidents, not adversaries. In
particular the **instrument catalogue and price history are shared** across the
whole instance and writable by any member: someone could repoint an
instrument's price symbol and change what everyone's charts show.

An instance is for one household, or people who trust each other. It is not
multi-tenant hosting.

### Plain HTTP is plain

Over HTTP on a LAN, session cookies and page contents are readable by anyone on
that network. If that matters, put TLS in front of it and set
`auth.cookie_secure: true` and `auth.trusted_proxies`.

Passkeys and security keys additionally *require* HTTPS — a browser rule, not
ours.

### An admin can reset anyone

Admins can reset passwords and clear another user's two-factor. Anyone with
shell access can do the same with `python -m app.recover`. That is the intended
recovery path for a self-hosted app; it also means the admin is trusted.

## Reporting a vulnerability

Please report privately rather than opening a public issue, and give a
reasonable window to fix it before disclosing. See `SECURITY.md` in the
repository root for the current contact.

Useful reports say what an attacker can do that they should not be able to.
"Anyone with the database file can read holdings" is documented above and not a
vulnerability; "a viewer can write trades" would be.

## Hardening checklist

- [ ] TLS in front, `auth.cookie_secure: true`, `auth.trusted_proxies` set to
      your proxy
- [ ] Two-factor enabled on every account
- [ ] Recovery codes stored somewhere that is not the phone with the
      authenticator on it
- [ ] The database file backed up, and the backup protected as carefully as the
      database
- [ ] Not exposed to the internet unless you meant to

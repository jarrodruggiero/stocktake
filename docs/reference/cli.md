# Command line

Three commands, run from inside the container or the app directory. In
Kubernetes: `kubectl exec -n stocktake deploy/stocktake -- python -m app.recover --list`.
In Docker: `docker compose exec app python -m app.recover --list`.

## `python -m app.recover` — get back into a locked account

The escape hatch for the case with no in-app answer: the **only** admin loses
their password or their authenticator. An ordinary user does not need this — an
admin can reset a password and clear two-factor from the users page.

```sh
python -m app.recover --list
python -m app.recover --email you@example.com --password 'a new password'
python -m app.recover --email you@example.com --clear-2fa
python -m app.recover --email you@example.com --make-admin
python -m app.recover --email you@example.com --activate
```

Flags combine — `--password ... --clear-2fa --activate` in one run is fine.

Notes:

- The password is hashed exactly as the app hashes it and works immediately.
  **Do not set passwords by editing the database directly**; you would be
  writing plaintext into a column that expects an argon2id hash, producing an
  account nobody can log into.
- The same length rules as the web form apply. It will refuse a weak password.
- **Every run signs that account out everywhere** and logs a warning. A
  recovery usually means something went wrong, and it is the one moment where
  invalidating every session is clearly right.
- It requires shell access to the machine holding the database. That is not an
  extra privilege: anyone with that access can already read everything (see
  [the security model](../about/security.md)).

## `python -m app.pricefeed` — fetch prices now

Runs one feed pass and exits: daily closes for every instrument with a symbol,
FX for every non-AUD currency held, distribution history, and a repair pass for
trades whose exchange rate was unknown at import.

The app also does this on its own schedule and at startup when data is stale,
so this is for impatience and for debugging. Its log line reports what each
source served, which is the quickest way to see whether a provider is failing.

## `alembic upgrade head` — migrate manually

Not normally needed: the app migrates itself at startup. Useful when you want
to migrate before starting, or to inspect what a release will change:

```sh
alembic -c alembic.ini current
alembic -c alembic.ini history
alembic -c alembic.ini upgrade head
```

Migrations only ever roll forward in normal use. Downgrades exist and are
tested, but rolling an app version back after a migration has run is not a
supported path — take a backup before upgrading.

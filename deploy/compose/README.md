# Run with Docker Compose

```sh
docker compose up -d
```

Open <http://localhost:8000> and a setup wizard takes it from there: it asks
where to keep the data, creates your account (the first one is the
administrator), and writes `config.yaml` for you.

The `config.yaml` in this directory is the annotated default — every option
documented, almost every line commented out. You do not need to edit it before
starting; it is there to read, and to edit afterwards if you would rather
change something by hand than through the admin page.

## Before you expose it

Nothing here terminates TLS, and the app will not pretend otherwise. Put a
reverse proxy in front (Caddy, Traefik, nginx). The wizard's **Environment**
step asks for the HTTPS URL and your proxy's address, and sets the two settings
below from them; to do it by hand afterwards, in `config.yaml`:

```yaml
auth:
  cookie_secure: true          # the session cookie stops travelling in clear
  trusted_proxies:
    - 172.16.0.0/12            # your proxy, and nothing wider
```

`trusted_proxies` matters more than it looks. Without it the app cannot tell
that a TLS-terminating proxy's connection was encrypted: the login page warns
about plain HTTP when you are not on it, passkeys are not offered, and every
sign-in is recorded against the proxy's address. Set too wide, and a caller can
choose the address recorded against their own.

## Upgrading

```sh
docker compose pull && docker compose up -d
```

The app migrates its own database at startup. **Back up first** — migrations
roll forward, and rolling an image back after one has run is not supported.

## Backups

```sh
# Python's sqlite3 module: the image has no sqlite3 command.
docker compose exec app python -c "import sqlite3; sqlite3.connect('/data/stocktake.db').backup(sqlite3.connect('/data/backup.db'))"
docker compose cp app:/data/backup.db ./stocktake-backup.db
docker compose cp app:/config/config.yaml ./stocktake-config.yaml
```

Those two files are the entire application state. Restore them with the app
stopped, as `/data/stocktake.db` and `/config/config.yaml`. Test the restore —
an untested backup is a hope.

## Useful commands

```sh
docker compose logs -f app
docker compose exec app python -m app.recover --list      # locked out
docker compose exec app python -m app.pricefeed           # fetch prices now
```

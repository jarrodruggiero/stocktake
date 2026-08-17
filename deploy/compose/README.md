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
step asks for the HTTPS URL and sets both of the settings below for you; to do
it by hand afterwards, in `config.yaml`:

```yaml
auth:
  cookie_secure: true          # the session cookie stops travelling in clear
  trusted_proxies:
    - 172.16.0.0/12            # your proxy, and nothing wider
```

`trusted_proxies` matters more than it looks. Without it the app sees the proxy
as the client for everybody, so login lockout — which counts per address —
becomes global: one person locking themselves out locks out everyone. Set too
wide, and a caller can forge their own address to dodge it.

## Upgrading

```sh
docker compose pull && docker compose up -d
```

The app migrates its own database at startup. **Back up first** — migrations
roll forward, and rolling an image back after one has run is not supported.

## Backups

```sh
docker compose exec app sh -c 'sqlite3 /data/stocktake.db ".backup /data/backup.db"'
docker compose cp app:/data/backup.db ./stocktake-backup.db
```

That file is the entire application state. Restore by putting it back as
`/data/stocktake.db`. Test the restore — an untested backup is a hope.

## Useful commands

```sh
docker compose logs -f app
docker compose exec app python -m app.recover --list      # locked out
docker compose exec app python -m app.pricefeed           # fetch prices now
```

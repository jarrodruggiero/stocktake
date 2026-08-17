# Deploying Portfolio

Several ways in, in order of how much you already run.

| | For | Database |
| --- | --- | --- |
| [`compose/`](compose/) | One machine, a NAS, a home server | SQLite |
| [`nas/`](nas/) | Unraid · TrueNAS · HexOS · Synology · QNAP · CasaOS · Proxmox | SQLite |
| [`kubernetes/`](kubernetes/) | A cluster, without Helm | SQLite |
| [`helm/stocktake/`](helm/stocktake/) | A cluster, with Helm | SQLite (default) |
| [`helm/stocktake/`](helm/stocktake/) + values | A cluster, sharing your Postgres | Postgres |

**All of them run the same image.** No platform gets a variant, and nothing
here needs building per-NAS — the differences are entirely in how each one
takes a container definition. Most take the Compose file unchanged.

Nothing needs a config file written beforehand either: with no configuration
the app boots into a first-run wizard that chooses the database, creates your
account, and writes `config.yaml` itself.

No Redis, no queue, no secrets service — sessions are database-backed tokens,
and the price feed needs no credentials.

## The two directories, and why only one is backed up

**`/data` is everything you own** — the database, and any broker or statement
formats you added. Back it up; it is the whole application state.

**`/scratch` is deliberately disposable.** The visual statement mapper renders
page images there while somebody is using it, and sweeps them thirty minutes
after they stop. The Kubernetes manifest and the Helm chart mount an `emptyDir`
over it, with a size limit; under Docker it is just a directory in the
container. **Do not put it on a persistent volume and do not back it up** —
these are pictures of a dividend statement, they are written `0700` so one
signed-in user cannot read another's, and a snapshot repository keeping seven
daily copies is the opposite of what the app promises about them.

There is a practical reason too. Back /scratch up and the backup itself breaks:
a restic mover that drops `DAC_OVERRIDE` cannot traverse a `0700` directory it
does not own, so it exits non-zero and retries forever.

## Choosing a database

**SQLite, unless you know you need otherwise.** One file, nothing else to run,
backed up by copying it. A personal portfolio is a few thousand rows.

**Postgres** if you want more than one replica, expect a background writer
concurrent with user writes, or already run one and would rather keep
everything in it.

The Helm chart deliberately ships **no bundled Postgres**. A database that
appears and disappears with `helm uninstall` is a way to lose your data — point
the chart at one you run and back up.

## Three things everyone gets wrong

**The timezone.** Containers run UTC. Every date decision in the app — which
financial year today is in, whether a trade is dated in the future — uses the
configured zone, so leaving it unset puts the app a day behind for most of an
Australian morning.

**`trusted_proxies` behind a reverse proxy.** Without it the app sees the proxy
as the client for everybody, so login lockout — which counts per address —
becomes global: one person locking themselves out locks out everyone. Set it to
your proxy's address or CIDR and nothing wider, because a caller whose address
is believed can forge one to dodge lockout entirely.

**`cookie_secure` once TLS is in front.** Until then the session cookie travels
in clear. The Helm chart refuses to render an Ingress without it rather than
letting that happen quietly.

## Storage permissions

The image runs as **uid 1000**, and the volume has to be writable by it:

- **Kubernetes** — `fsGroup: 1000` on the pod. Both the manifests and the chart
  set it. Without it the PVC arrives owned by root and the app cannot create
  its database.
- **Compose** — a *named* volume inherits the image's ownership and just works.
  A **bind** mount does not: `chown -R 1000:1000 ./data` on the host first.

## Upgrading

The app migrates its own database at startup. **Back up first.** Migrations
roll forward; rolling an image back after one has run is not supported.

Config and app version are a matched pair — the app rejects unknown settings
outright, so a typo is loud rather than ignored, and a config written for a
newer version is refused by an older one. Move them together.

## Backups

The database is the entire application state. Nothing else needs preserving,
and there is no external service holding anything.

Test the restore. An untested backup is a hope.

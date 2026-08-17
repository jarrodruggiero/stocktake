# Deployment

Four ways in. All run the same image and need the same two things: a config
file, and somewhere to write.

| Path | For | Database |
| --- | --- | --- |
| [Docker Compose](#docker-compose) | One machine, a NAS, a home server | SQLite |
| [Plain Kubernetes](#plain-kubernetes) | A cluster, without Helm | SQLite |
| [Helm](#helm) | A cluster, with Helm | SQLite |
| [Helm with Postgres](#helm-with-postgres) | A cluster, sharing your Postgres | Postgres |

There is no Redis, no queue and no secrets service. Sessions are
database-backed tokens and the price feed needs no credentials, which is what
makes this a two-file deployment.

## Three things everyone gets wrong

Read these before picking a path — they apply to all four, and each one causes
a failure that looks like something else.

**Set the timezone.** Containers run UTC. Every date decision — which financial
year today is in, whether a trade is dated in the future — is made in the
configured zone, so leaving it unset puts the app a day behind for most of an
Australian morning.

**Set `trusted_proxies` behind a reverse proxy.** Without it the app sees the
proxy as the client for *everybody*, so login lockout — which counts per
address — becomes global: one person locking themselves out locks out everyone.
Set it to your proxy's address or CIDR and nothing wider, because a caller
whose address is believed can forge one to dodge lockout entirely.

**Set `cookie_secure: true` once TLS is in front.** Until then the session
cookie travels in clear. The Helm chart refuses to render an Ingress without
it rather than letting that happen quietly.

## Storage permissions

The image runs as **uid 1000**, and the volume must be writable by it.

- **Kubernetes** — `fsGroup: 1000` on the pod. The manifests and the chart both
  set it. Without it the volume arrives owned by root and the app cannot create
  its database.
- **Compose** — a **named** volume inherits the image's ownership and just
  works. A **bind** mount does not: run `chown -R 1000:1000 ./data` on the host
  first.

---

## Docker Compose

Everything is in [`deploy/compose/`](https://github.com/jarrodruggiero/stocktake/tree/main/deploy/compose).

```sh
cd deploy/compose
docker compose up -d
```

Open <http://localhost:8000> and the [setup wizard](../getting-started/index.md)
takes it from there — no configuration file has to exist first.

`config.yaml` beside the compose file is the annotated default, mounted at
`/config/config.yaml`. Every setting and its default is in the
[configuration reference](../reference/configuration.md).

```sh
docker compose logs -f app
docker compose pull && docker compose up -d          # upgrade
docker compose exec app python -m app.recover --list # locked out
```

### Backing up

```sh
docker compose exec app sh -c 'sqlite3 /data/stocktake.db ".backup /data/backup.db"'
docker compose cp app:/data/backup.db ./stocktake-backup.db
```

That file is the entire application state. Restore it as `/data/stocktake.db`.

---

## Plain Kubernetes

One file, five resources, no Helm:
[`deploy/kubernetes/stocktake.yaml`](https://github.com/jarrodruggiero/stocktake/tree/main/deploy/kubernetes).

```sh
kubectl create namespace stocktake
# edit the four CHANGE ME markers first
kubectl apply -f deploy/kubernetes/stocktake.yaml
kubectl -n stocktake rollout status deploy/stocktake
```

The markers are: the image, the timezone (twice — config and the `TZ`
environment variable), `trusted_proxies`, and the Ingress host. Delete the
Ingress if you reach the Service another way.

!!! warning "`strategy: Recreate` is not a preference"
    The volume is ReadWriteOnce, so a second pod cannot attach it while the
    first holds it — a RollingUpdate deadlocks on Multi-Attach and hangs until
    it times out. More importantly, **SQLite takes one writer**: two pods on
    one file is corruption, not contention.

---

## Helm

```sh
helm install stocktake ./deploy/helm/stocktake \
  --namespace stocktake --create-namespace \
  --set image.repository=ghcr.io/jarrodruggiero/stocktake \
  --set timezone=Australia/Melbourne
```

Defaults give you SQLite on a 2 GiB PersistentVolume, which is the right choice
for almost everyone.

Behind an ingress, all three of these go together:

```yaml
ingress:
  enabled: true
  className: nginx
  host: stocktake.example.com
  tls:
    - secretName: stocktake-tls
      hosts: [stocktake.example.com]
auth:
  cookieSecure: true
  trustedProxies: ["10.42.0.0/16"]   # your ingress controller's pod CIDR
```

The chart **fails to render** if you enable the ingress without
`auth.cookieSecure`. That is deliberate: a warning gets ignored, and the person
deploying it almost certainly believes TLS is handling the cookie.

Anything the chart does not model goes in `extraConfig`, which is merged into
`config.yaml` — so any setting the app supports is reachable without the chart
needing to know about it:

```yaml
extraConfig:
  imports:
    max_upload_mb: 25
```

The data PVC carries `helm.sh/resource-policy: keep`, so `helm uninstall` does
not take your portfolio with it.

---

## Helm with Postgres

Choose Postgres when you want more than one replica, expect a background writer
concurrent with user writes, or already run one and would rather keep
everything in it. Otherwise SQLite is simpler and enough.

**The chart ships no bundled Postgres, on purpose.** A database that appears
and disappears with `helm uninstall` is a way to lose your data. Point it at
one you run and back up.

```sh
kubectl -n stocktake create secret generic stocktake-db \
  --from-literal=password='...'

helm install stocktake ./deploy/helm/stocktake \
  --namespace stocktake \
  --set database.type=postgres \
  --set database.postgres.host=postgres.databases.svc \
  --set database.postgres.name=stocktake \
  --set database.postgres.user=stocktake \
  --set database.postgres.existingSecret=stocktake-db
```

The password comes from a Secret you create and **never from values** — values
end up in `helm get values`, in CI logs and in git.

The app creates its own schema at startup; the database and role need to exist
first. With Postgres the deployment strategy becomes RollingUpdate, since the
single-writer constraint no longer applies.

---

## Upgrading

The app migrates its own database at startup.

**Back up first.** Migrations roll forward; rolling an image back after one has
run is not a supported path.

**Config and app version are a matched pair.** The app rejects unknown settings
outright — a typo should be loud, not silently ignored — which also means a
config written for a newer version is refused by an older one, and vice versa.
Move them together, in one apply.

## Reverse proxies

Nothing here terminates TLS, and the app will not pretend otherwise. Put Caddy,
Traefik or nginx in front, then set `cookie_secure` and `trusted_proxies` as
above.

If you terminate TLS at a tunnel service, note that it sees your traffic in
clear at its edge. That is fine for many people and worth knowing for an app
that shows everything you own.

Passkeys and security keys additionally **require** HTTPS — a browser rule, not
ours. Internal-only HTTPS is fine: a real domain resolved internally with
DNS-01 certificates satisfies it with no inbound exposure.

## Monitoring and alerts

The app never alerts you about anything. It has no notification system, sends no
mail, and talks to no push service. The Helm chart installs no monitoring
either — no Prometheus, no exporters, not even a `ServiceMonitor` — because a
portfolio tracker should not decide what your cluster's observability looks
like.

What it does have is a `/metrics` endpoint, **off by default**:

```yaml
metrics:
  enabled: true
```

It publishes the app's own health — when prices were last fetched, whether the
last fetch worked, whether the database answers — and deliberately nothing about
your holdings, values or trades. It is unauthenticated, because a scraper has no
cookie and nothing to log in with, which is exactly why it is opt-in.

Ready-made alerting rules, for plain Prometheus and for the Prometheus Operator,
live in [`deploy/monitoring/`](https://github.com/jarrodruggiero/stocktake/tree/main/deploy/monitoring)
with the reasoning behind every threshold. The one worth reading even if you
use something else entirely: **an unauthenticated request to this app must be
refused**, so the healthy status code for a probe of `/` is `401` and a `200`
means the login gate has broken open. Probing it with an ordinary "expect 2xx"
check gets you an alert that fires while everything is fine and stays silent
when it is not.

# Monitoring templates

**This application does not alert you about anything.** It has no notification
system, sends no mail, and talks to no push service. Nothing here is a feature
you turn on inside the app — it is a set of files for a monitoring system you
already run.

**The Helm chart does not install monitoring either.** No Prometheus, no
Alertmanager, no Grafana, no exporters, and no `ServiceMonitor`. That is
deliberate: a self-hosted finance tracker should not decide what your cluster's
observability looks like, and most people running one already have something.

What this directory *is*: the rules worth having if you do run Prometheus, with
the reasoning written down, so you can start from something considered rather
than from a blank file.

| File | For |
| --- | --- |
| `prometheus-rules.yaml` | Plain Prometheus — add it to `rule_files:` in `prometheus.yml`. Works with a Docker Compose Prometheus too. |
| `prometheusrule.yaml` | The Prometheus Operator / kube-prometheus-stack — `kubectl apply -f`. Same rules, wrapped in a `PrometheusRule`. |

The two files hold identical rule groups and a test fails if they drift. Edit
`prometheus-rules.yaml` and regenerate the other; do not maintain both.

If you use something else — Zabbix, Uptime Kuma, Netdata, a shell script and
`cron` — the rules still tell you *what is worth watching and why*, which is the
part that took the thinking. The expressions are the easy bit to translate.

## What you need first

### 1. Turn the metrics endpoint on

Most of these rules read `/metrics`, which is **off by default**:

```yaml
metrics:
  enabled: true
```

Restart the app and check it:

```sh
curl http://stocktake.example.com/metrics
```

The endpoint is unauthenticated — a scraper has no cookie and nothing to log in
with, so it cannot be behind the session gate. It is off by default for exactly
that reason: turning it on should be a decision, not something an upgrade does
to you.

**What it publishes is the application's health, never your portfolio.** When
prices were last fetched, whether the last fetch worked, whether the database
answers. There is nothing about holdings, values, trades, or even how many
instruments you track, and a test in this repository pins the list closed so a
future change cannot quietly add one. The full list:

| Metric | Type | Meaning |
| --- | --- | --- |
| `stocktake_database_ready` | gauge | 1 if the database answers, 0 if not (including before the setup wizard has been finished) |
| `stocktake_last_price_date_seconds` | gauge | Trading day of the most recent close held, as unix seconds at UTC midnight. **Absent when there are no prices yet** |
| `stocktake_last_fx_date_seconds` | gauge | Same, for foreign-exchange rates. Absent if you have never needed one |
| `stocktake_feed_runs_total` | counter | Price-feed runs since the process started |
| `stocktake_feed_failures_total` | counter | Of those, how many raised |
| `stocktake_feed_last_success_seconds` | gauge | When the feed last completed without raising. **Absent until the first success**, and reset by a restart |

A scrape config for the plain case:

```yaml
- job_name: stocktake
  static_configs:
    - targets: ["stocktake.example.com:8000"]
```

On Kubernetes, if you scrape by pod annotation:

```yaml
annotations:
  prometheus.io/scrape: "true"
  prometheus.io/port: "8000"
  prometheus.io/path: "/metrics"
```

The rules use `job="stocktake"` in one place (`StocktakeMetricsTargetDown`).
Rename it to match your job.

### 2. Optional: a blackbox exporter, for the availability group

Two probes, and the second one is the interesting one. **A request with no
session must be refused**, so the healthy status code is `401` and a `200` is a
security failure rather than a success. Probing this with an ordinary `http_2xx`
module gets you an alert that fires while the app is fine and stays quiet if the
login gate breaks open — precisely backwards.

Add a module that expects a 401:

```yaml
# blackbox.yml
modules:
  http_401:
    prober: http
    http:
      valid_status_codes: [401]
```

and point a probe job at the app with it, labelled `job: stocktake-auth` (or
rename the label in the rules):

```yaml
- job_name: stocktake-auth
  metrics_path: /probe
  params:
    module: [http_401]
  static_configs:
    - targets: ["http://stocktake.example.com"]
  relabel_configs:
    - source_labels: [__address__]
      target_label: __param_target
    - source_labels: [__param_target]
      target_label: instance
    - target_label: __address__
      replacement: blackbox-exporter:9115
```

Probe the root path, not `/healthz` — `/healthz` and `/readyz` are public by
design (a liveness probe cannot log in), so they prove the process is running
and say nothing about whether the gate in front of everything else is shut.

One caveat worth knowing: a request that asks for HTML gets a **302 to the login
page** rather than a 401, because sending a browser a bare "401" instead of a
sign-in form is a bad experience. Blackbox does not send `Accept: text/html`, so
it gets the 401 — but if you build your own probe and set that header, expect
the redirect and adjust.

### 3. Optional: kube-state-metrics, for the Kubernetes group

Standard cluster metrics. If your cluster already alerts on crash-looping pods
and full volumes generically, delete that group rather than getting each alert
twice.

## The alerts

Everything below is in both files. Delete what does not apply to you — a rule
that cannot be true in your setup is noise you will learn to ignore, and the
habit of ignoring alerts is the thing you are actually trying to avoid.

### Price feed — needs `metrics.enabled`

| Alert | Fires when | Severity |
| --- | --- | --- |
| `StocktakePriceFeedFailing` | The feed has not succeeded in 36 hours | warning |
| `StocktakePriceFeedNeverSucceeded` | No successful run since startup, for 6 hours | warning |
| `StocktakePricesStale` | No closing price newer than 6 days | warning |
| `StocktakeFxRatesStale` | No FX rate newer than 6 days | warning |

**Why both a "failing" rule and a "stale" rule.** They catch different things.
`StocktakePriceFeedFailing` is the fast, precise one: the fetch raised, and you
know within a day and a half. `StocktakePricesStale` is the backstop for the
failure that produces no error at all — a provider that answers cheerfully with
no rows. The app reports success, the log is clean, and the numbers simply stop
moving. Nothing but the data itself can tell you that has happened.

**Why 6 days and not 3.** The metric is a *trading day*, so its age is naturally
lumpy. Over a normal weekend the most recent close reaches about 80 hours old
before Monday's run. A public holiday on either side of a weekend — Easter,
Christmas — stretches that to roughly 5 days and 8 hours, legitimately. Six days
clears every one of those and still fires within a week of a genuine break. If
your market keeps different hours, this is the number to change, and the
arithmetic is written out in the comments in `prometheus-rules.yaml`.

**`StocktakeFxRatesStale` only makes sense if you hold something in a foreign
currency.** If you do not, the app never fetches a rate and this alert fires on
day one — delete it. If you do, it is worth keeping for a non-obvious reason:
when a currency has no usable rate the app *excludes* that instrument from
totals rather than valuing it 1:1. Your numbers do not go wrong, they go
incomplete, and that is much harder to notice.

**`StocktakePriceFeedNeverSucceeded` uses `absent()`**, which is also true when
Prometheus cannot reach the app at all. Route it below your availability alerts
or you will get it alongside every outage. Delete it entirely if you run with
`price_feed.enabled: false` — with no feed, it is permanently and correctly
true.

### Health — needs `metrics.enabled`

| Alert | Fires when | Severity |
| --- | --- | --- |
| `StocktakeDatabaseUnreachable` | `stocktake_database_ready` is 0 for 10 minutes | critical |

This is also true on a brand-new install that has not been through the setup
wizard yet, because there genuinely is no database. Silence it while you
install.

### Availability — needs a blackbox exporter

| Alert | Fires when | Severity |
| --- | --- | --- |
| `StocktakeLoginGateOpen` | An unauthenticated request was answered `200` | critical |
| `StocktakeUnreachable` | The probe fails for 5 minutes | critical |
| `StocktakeMetricsTargetDown` | `/metrics` cannot be scraped for 10 minutes | warning |

`StocktakeLoginGateOpen` is the one to keep if you keep only one. Everything
else here tells you the app is not working; this one tells you it is working for
people who should not have it.

`StocktakeMetricsTargetDown` matters more than it looks: while it is firing,
every rule in the feed and health groups is blind, so an outage of the scrape
target silently disables most of this file.

### Kubernetes — needs kube-state-metrics

| Alert | Fires when | Severity |
| --- | --- | --- |
| `StocktakeCrashLooping` | 3+ container restarts in 15 minutes | critical |
| `StocktakeVolumeAlmostFull` | The data volume is over 85% full | critical |

`StocktakeVolumeAlmostFull` is critical rather than a warning because the SQLite
database lives on that volume: a full volume is not a slow app, it is an app
that cannot record a trade.

## What is deliberately not here

- **Anything about the value of your portfolio.** "Alert me when a holding drops
  10%" is a price alert, not a monitoring alert, and building it here would mean
  publishing your positions to an unauthenticated endpoint. That is the one
  thing this project promises not to do.
- **A Grafana dashboard.** Six series do not need one, and a dashboard is a
  strong opinion about someone else's Grafana.
- **Backup monitoring.** How you back up the volume is entirely yours —
  VolSync, `restic` on a timer, a snapshot on the hypervisor — and there is no
  metric shared between them to write a rule against. Whatever you use, alert on
  it; a backup nobody checks is a backup you find out about during a restore.

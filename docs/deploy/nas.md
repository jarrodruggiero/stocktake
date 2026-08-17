# NAS and home-server platforms

Every platform on this page runs OCI containers, so they all run the same image
and none of them needs a variant of it. The only difference is how you hand the
container definition over, and most take
[the Compose file](index.md#docker-compose) unchanged.

There is nothing to prepare first on any of them. Start the container, open the
web interface, and the [setup wizard](../getting-started/index.md) chooses where
to keep the data, creates your account and writes the configuration.

| Platform | How | Ready-made template |
| --- | --- | --- |
| Unraid | Community Applications | ✅ `deploy/nas/unraid/` |
| CasaOS / ZimaOS | App store manifest | ✅ `deploy/nas/casaos/` |
| TrueNAS SCALE 24.10+ | Apps → Install via YAML | paste the Compose file |
| HexOS | the TrueNAS layer underneath | paste the Compose file |
| Synology DSM 7.2+ | Container Manager → Project | paste the Compose file |
| QNAP | Container Station → Application | paste the Compose file |
| Proxmox VE 9.1+ | pulls the OCI image natively | none needed |

!!! warning "The one thing that catches everyone: ownership"

    The app runs as **uid 1000**, and both `/data` and `/config` must be
    writable by it.

    A platform-managed volume inherits the image's ownership and works
    untouched. A **bind mount to a host path does not** — it arrives owned by
    whoever owns the host directory, and the container starts and immediately
    stops because it cannot create its database.

    Where a platform's convention is a host path — Synology, QNAP, and TrueNAS
    if you choose a dataset — run `chown -R 1000:1000 <that path>` once before
    the first start.

## TrueNAS SCALE

TrueNAS moved from Kubernetes to Docker in 24.10 "Electric Eel", so current
versions take a Compose file directly.

**Apps → Discover Apps → ⋮ → Install via YAML**, name it `stocktake`, and paste
`deploy/compose/docker-compose.yml`. Change the image and `TZ` before saving.

The Compose file uses Docker named volumes, which TrueNAS manages and which
arrive with the right ownership. To keep the data on a dataset you can snapshot
instead — worth doing, since ZFS snapshots of the directory holding
`stocktake.db` are the best backup this app can have:

```yaml
    volumes:
      - /mnt/tank/apps/stocktake/config:/config
      - /mnt/tank/apps/stocktake/data:/data
```

…then `chown -R 1000:1000 /mnt/tank/apps/stocktake` once, from a shell.

## HexOS

HexOS is built on TrueNAS SCALE, and its app catalogue is a curated set rather
than something anyone can publish into. So there is no HexOS template to
submit: reach the TrueNAS interface underneath and follow the section above.

## Synology DSM

Needs **DSM 7.2 or later** for Container Manager's Projects. On older DSM the
Docker package has no Compose support.

1. Install **Container Manager** from Package Center.
2. Make a folder for it, e.g. `/volume1/docker/stocktake`, containing `config`
   and `data`.
3. **Project → Create**, name `stocktake`, point it at that folder, choose
   *Create docker-compose.yml*, and paste the Compose file with the volumes
   changed to those two host paths.
4. `sudo chown -R 1000:1000 /volume1/docker/stocktake` over SSH — see the
   warning above.

## QNAP

**Container Station → Applications → Create**, paste the Compose file. Keep the
named volumes, or point them at `/share/Container/stocktake/{config,data}` and
`chown -R 1000:1000` that path first.

## Proxmox VE

!!! warning "Requires Proxmox VE 9.1 or later"

    Running the container natively depends on Proxmox's OCI image support,
    which **arrived in 9.1 and does not exist in 8.x or 9.0**. Check
    **Datacenter → your node → Summary** for the version before following this
    section.

    On anything earlier, skip to [Before 9.1](#before-91) — the app runs
    perfectly well there, just inside Docker rather than as an LXC of its own.

**Proxmox VE 9.1 pulls OCI images and runs them as LXC containers directly**,
so this project ships no LXC template — and deliberately. Converting a Docker
image into one by hand (`docker export`, repair the `/dev/stdout` symlinks,
recompress) produces an artefact that duplicates the image and then drifts from
it.

1. Storage → **Pull from OCI Registry** → the image name.
2. **Create CT**, pick the pulled image on the *Template* tab, add mount points
   for `/data` and `/config` on *Disks*.
3. Set `TZ` under *Options → Environment* after creation.

Two caveats, from Proxmox rather than from this app:

- It is a **tech preview** in 9.1 — no live migration, and updating means
  recreating the container rather than swapping the image.
- The console shows the main process's output, not a shell. `pct enter <vmid>`
  gets you one, which is where you would run `python -m app.recover`.

### Before 9.1

Run a Debian LXC or a VM with Docker inside it and follow the
[Compose instructions](index.md#docker-compose). For an LXC, **enable nesting**
(*Options → Features → Nesting*) or Docker will not start inside it.

This is also the safer choice on 9.1 for now, given the tech-preview caveats
above — an unprivileged Debian LXC running Docker is a well-worn path, and the
upgrade to native OCI is a re-create rather than a migration whenever you want
it.

## Getting it into an app store

Two of these have open catalogues that accept community submissions:

- **Unraid Community Applications** — see `deploy/nas/unraid/README.md`. Needs the
  public repository, an OSI licence, and a resolving icon URL.
- **CasaOS AppStore** — fork `IceWhaleTech/CasaOS-AppStore`, add
  `Apps/Portfolio/` with the manifest from `deploy/nas/casaos/`, an icon, a
  thumbnail and a screenshot, then open a pull request.

Both are blocked on the same thing: the public repository and the branding
assets. TrueNAS, Synology and QNAP have no equivalent third-party catalogue —
their users install from a Compose file, which is what the page above is for.

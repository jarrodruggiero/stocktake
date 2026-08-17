# NAS platforms

Every platform here runs OCI containers, so they all run the same image and
none of them need a variant of it. What differs is only how you hand the
container definition over — and for most, that means pasting the Compose file
from [`../compose/`](../compose/).

!!! note
    `ca_profile.xml` is at the **repository root**, not in `unraid/`. Community
    Applications validates the maintainer profile as a property of the *repo*,
    and every published CA repository keeps it at the top level. The template
    itself can live anywhere, because the template URL is explicit.

There is nothing to prepare first on any of them. Start the container, open the
web UI, and the first-run wizard chooses where to keep the data, creates your
account and writes `config.yaml` itself.

| Platform | How | Template here? |
| --- | --- | --- |
| [Unraid](unraid/) | Community Applications template | yes — `unraid/stocktake.xml` |
| [CasaOS / ZimaOS](casaos/) | App store manifest | yes — `casaos/docker-compose.yml` |
| [TrueNAS SCALE](#truenas-scale) | Apps → Install via YAML | no — paste the Compose file |
| [HexOS](#hexos) | via the TrueNAS layer underneath | no |
| [Synology DSM](#synology-dsm) | Container Manager → Project | no |
| [QNAP](#qnap) | Container Station → Application | no |
| [Proxmox VE](#proxmox-ve) | OCI image pulled natively (9.1+) | no |

**The one thing worth checking on every platform** is that `/data` and
`/config` are writable by uid 1000. The image creates both directories owned by
that user, so a *managed* volume inherits it and works untouched. A **bind
mount to a host path does not** — it arrives owned by whoever owns the host
directory. Where a platform's convention is a host path (Unraid, Synology,
QNAP), each section below says what to do.

---

## TrueNAS SCALE

TrueNAS moved from Kubernetes to Docker in 24.10 "Electric Eel", so recent
versions take a Compose file directly.

**Apps → Discover Apps → the ⋮ menu → Install via YAML.** Give it the name
`stocktake` and paste the contents of
[`../compose/docker-compose.yml`](../compose/docker-compose.yml).

Two edits before you save:

- Replace `ghcr.io/jarrodruggiero/stocktake:latest` with the real image.
- Change `TZ` to your own timezone.

Storage: the Compose file uses Docker named volumes, which TrueNAS manages for
you and which arrive with the right ownership. If you would rather keep the
data on a dataset you can see and snapshot, replace the volume lines with host
paths and set the dataset's owner to uid 1000:

```yaml
    volumes:
      - /mnt/tank/apps/stocktake/config:/config
      - /mnt/tank/apps/stocktake/data:/data
```

Then, once from a shell: `chown -R 1000:1000 /mnt/tank/apps/stocktake`.

A dataset is worth the extra step here — ZFS snapshots of the directory holding
`stocktake.db` are the best backup this app can have.

## HexOS

HexOS is built on TrueNAS SCALE, and its own app catalogue is a curated set
rather than something anyone can publish to. So there is no HexOS template to
submit, and the route in is the TrueNAS layer underneath: reach the TrueNAS web
UI and follow [the section above](#truenas-scale).

If HexOS later opens its catalogue, the TrueNAS Compose file is what an entry
would be built from — nothing about the image would change.

## Synology DSM

Requires **DSM 7.2 or later**, where Container Manager gained Projects. (On
older DSM the Docker package has no Compose support, and you would be creating
the container by hand.)

1. Install **Container Manager** from Package Center.
2. Create a shared folder or subfolder for the app, e.g.
   `/volume1/docker/stocktake`, with `config` and `data` inside it.
3. **Container Manager → Project → Create.** Name it `stocktake`, set the path
   to that folder, and choose *Create docker-compose.yml*.
4. Paste [`../compose/docker-compose.yml`](../compose/docker-compose.yml),
   replacing the two volume lines with your host paths:

```yaml
    volumes:
      - /volume1/docker/stocktake/config:/config
      - /volume1/docker/stocktake/data:/data
```

5. **Before starting**, SSH in and run
   `sudo chown -R 1000:1000 /volume1/docker/stocktake`.

That last step is not optional and is the usual reason a first start fails: the
folder is owned by your DSM user, the app runs as uid 1000, and it cannot
create its database. The symptom is a container that starts and immediately
stops.

Reach it at `http://<nas>:8000`.

## QNAP

**Container Station 3** takes Compose under *Applications*.

1. **Container Station → Applications → Create.**
2. Name it `stocktake` and paste
   [`../compose/docker-compose.yml`](../compose/docker-compose.yml).
3. Either keep the named volumes (simplest) or point them at a share:

```yaml
    volumes:
      - /share/Container/stocktake/config:/config
      - /share/Container/stocktake/data:/data
```

4. If you used host paths, `chown -R 1000:1000 /share/Container/stocktake`
   over SSH first — same reason as Synology.

## Proxmox VE

> **Requires Proxmox VE 9.1 or later.** OCI image support arrived in 9.1 and
> does not exist in 8.x or 9.0. On anything earlier, use the Docker-in-an-LXC
> path at the end of this section.

**Proxmox VE 9.1 pulls OCI images directly and runs them as LXC containers**,
so no separate LXC template is needed or wanted from this project. That is why
one is not shipped here: converting a Docker image into an LXC template by hand
(`docker export`, fix the `/dev/stdout` symlinks, recompress) produces something
that duplicates the image and then drifts from it.

1. Select your storage → **Pull from OCI Registry** → enter the image, e.g.
   `ghcr.io/jarrodruggiero/stocktake:latest`.
2. **Create CT.** On the *Template* tab pick the pulled image; on *Disks* add
   your mount points for `/data` and `/config`.
3. Set `TZ` afterwards under the container's *Options → Environment*.

Two caveats from the Proxmox side, worth knowing before you commit to it:

- **It is a tech preview** at 9.1. Live migration is unsupported, and updating
  means recreating the container rather than swapping the image.
- The console shows the main process's output rather than a shell. Use
  `pct enter <vmid>` when you need one — which is where you would run
  `python -m app.recover`.

**On older Proxmox**, run an ordinary Debian LXC or VM with Docker inside it
and follow the [Compose instructions](../compose/README.md). Nesting must be
enabled on the container for Docker to work.

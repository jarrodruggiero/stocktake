# Unraid

`stocktake.xml` is a Community Applications template.

**`ca_profile.xml` lives at the repository root**, not here. CA validates the
maintainer profile as a property of the repository, and every published CA repo
keeps it at the top level; the template itself can sit anywhere, because the
`TemplateURL` inside it is explicit. Unraid is filed under `deploy/nas/`
alongside the other NAS platforms — it is one — and the split is the only thing
that convention costs.

## Installing before it is in CA

Community Applications → **Docker** tab → *Add Container* → paste the raw URL of
`stocktake.xml` into the template field. Or drop the file in
`/boot/config/plugins/dockerMan/templates-user/` and it appears in the
*User templates* list.

**Nothing to prepare.** Add the container, start it, and open the WebUI: with
no configuration the app boots into a setup wizard that asks where to keep the
data, creates your account, and writes `config.yaml` for you. This is the
Unraid convention — an app writes its own defaults — and it is why there is no
"copy this file first" step here any more.

Set `TZ` to your own timezone anyway. The wizard asks for it too (and can read
it from your browser), but the two should agree.

## Why the template overrides the user

The image runs as uid 1000. Unraid's appdata share is owned by
`nobody:users` (99:100), so a bind mount from `/mnt/user/appdata` arrives
unwritable by uid 1000 and the app cannot create its database — the container
would start and immediately fail.

`ExtraParams` therefore runs it as `99:100`, which is the Unraid convention and
means the share works untouched. Nothing outside `/data` needs writing, so this
is safe; it was verified by booting the image as 99:100 against a bind mount.

The alternative — telling people to `chown -R 1000:1000` their appdata — works
but is the kind of instruction that gets skipped and then produces a confusing
failure.

## Backing up

`/mnt/user/appdata/stocktake/stocktake.db` is the entire application state.
Include that folder in your appdata backup and you have everything. Test the
restore.

## Submitting to Community Applications

**Not done yet — it needs the public repo first.** When it exists:

1. The repo must be **public**, active, and carry an **OSI-approved licence**
   (AGPL-3.0 qualifies).
2. `ca_profile.xml` must be present **at the repository root** with the
   maintainer details filled in.
3. The `Icon` URL must resolve — it points at `docs/assets/icon.png` on the
   default branch, so the file has to be committed, not just rendered locally.
   Check `TemplateURL` still matches this file's path if either has moved.
4. Point `Support` at a real destination (GitHub Discussions is fine; some
   maintainers use an Unraid forum thread).
5. Submit the repository at <https://ca.unraid.net/submit/new> — sign in with
   an Unraid account, give the GitHub URL, address anything the review flags.

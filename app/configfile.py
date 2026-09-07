"""Reading and writing config.yaml from inside the app.

Admin -> Settings edits it, under three constraints (decisions.md #78): the
file may not be writable and that is normal; comments must survive, so this
round-trips rather than dumps; and `database` and `app_name` are shown
read-only because changing either from a web form is a way to lose a database.

Everything editable records whether it applies immediately or needs a restart.
"""

from __future__ import annotations

import datetime as dt
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from appkit.config import CONFIG_FILE

from . import features


@dataclass(frozen=True)
class Option:
    """One editable setting, and everything the page needs to render it."""

    path: tuple[str, ...]      # where it sits in the YAML, e.g. ("auth", "cookie_secure")
    label: str
    kind: str                  # text | int | bool | date | choice | list
    blurb: str
    restart: bool = False      # does a change wait for a restart?
    optional: bool = False     # may be left empty, and is then written as unset
    # An authoritative reference, where one exists and beats explaining. The
    # blurb is escaped in the template, so a link cannot live inside it.
    link: str = ""
    link_text: str = ""
    choices: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None

    @property
    def name(self) -> str:
        """The form field name — the YAML path, flattened."""
        return ".".join(self.path)


# The editable surface. Anything not listed here is deliberately not editable
# from the web: see the module docstring.
OPTIONS: tuple[Option, ...] = (
    Option(("timezone",), "Timezone", "text",
           "The zone every date decision is made in. An IANA name, e.g. "
           "Australia/Melbourne.",
           link="https://en.wikipedia.org/wiki/List_of_tz_database_time_zones",
           link_text="List of IANA time zones"),
    Option(("log_level",), "Log level", "choice",
           "How much the application writes to its log.",
           restart=True, choices=("DEBUG", "INFO", "WARNING", "ERROR")),

    Option(("price_feed", "enabled"), "Fetch prices", "bool",
           "The daily price and exchange-rate feed. Off means valuations stay at "
           "the last stored close.", restart=True),
    Option(("price_feed", "hour"), "Feed hour", "int",
           "Hour of the daily run. Set it after your market closes.", minimum=0, maximum=23),
    Option(("price_feed", "minute"), "Feed minute", "int",
           "Minute of the daily run.", minimum=0, maximum=59),
    Option(("price_feed", "backfill_start"), "Backfill from", "date",
           "How far back to fetch history when an instrument is first seen."),

    Option(("auth", "session_ttl_days"), "Session length (days)", "int",
           "How long a session lasts. Every request pushes the expiry out again.", minimum=1, maximum=365),
    Option(("auth", "cookie_secure"), "HTTPS-only cookie", "bool",
           "Send the session cookie only over HTTPS. Turning this on without TLS "
           "stops anyone signing in."),
    Option(("auth", "rate_limit", "max_attempts"), "Failed sign-ins allowed", "int",
           "Failures from one email and address before it is locked out.", minimum=1, maximum=100),
    Option(("auth", "rate_limit", "window_minutes"), "Counting window (minutes)", "int",
           "How long failures count towards a lockout.",
           minimum=1, maximum=1440),
    Option(("auth", "rate_limit", "lockout_minutes"), "Lockout (minutes)", "int",
           "How long a lockout lasts.", minimum=1, maximum=1440),

    Option(("auth", "session_idle_minutes"), "Idle timeout (minutes)", "int",
           "How long a session survives with no requests.", minimum=1, maximum=10080),
    Option(("auth", "session_absolute_days"), "Maximum session age (days)", "int",
           "How long a session lasts however active it is. This is the cap a "
           "stolen cookie runs into.", minimum=1, maximum=365),
    Option(("auth", "idle_warning_seconds"), "Idle warning (seconds)", "int",
           "How long before the timeout to warn, and blur the figures on "
           "screen.", minimum=0, maximum=3600),
    Option(("auth", "trusted_proxies"), "Trusted proxies", "list",
           "Addresses or ranges whose X-Forwarded-For and X-Forwarded-Proto "
           "headers are believed. One per line.",
           optional=True, restart=True),

    Option(("auth", "webauthn", "enabled"), "Passkeys", "bool",
           "Whether passkeys can be added and used to sign in. Needs HTTPS and "
           "the two settings below.", restart=True),
    Option(("auth", "webauthn", "rp_id"), "Passkey domain", "text",
           "The domain passkeys are bound to. Change it and every existing passkey "
           "stops working.",
           optional=True, restart=True),
    Option(("auth", "webauthn", "rp_name"), "Passkey prompt name", "text",
           "What the browser's passkey prompt calls this site.",
           optional=True, restart=True),
    Option(("auth", "webauthn", "origins"), "Passkey origins", "list",
           "The full URLs passkeys may be used from. One per line.",
           optional=True, restart=True),

    Option(("auth", "oidc", "enabled"), "Single sign-on", "bool",
           "Whether sign-in can be handed to an external identity provider.",
           restart=True),
    Option(("auth", "oidc", "issuer"), "Provider URL", "text",
           "The provider's issuer URL. Its configuration is read from here.",
           optional=True, restart=True),
    Option(("auth", "oidc", "client_id"), "Client ID", "text",
           "The client ID the provider issued for Stocktake.",
           optional=True, restart=True),
    Option(("auth", "oidc", "redirect_uri"), "Redirect URL", "text",
           "Where the provider sends people back to. Must match what it has "
           "registered.", optional=True, restart=True),
    Option(("auth", "oidc", "button_label"), "Sign-in button", "text",
           "What the button on the login page says.", optional=True),
    Option(("auth", "oidc", "provisioning"), "Who may sign in", "choice",
           "Linked accounts only, holders of an invite, or anyone the provider "
           "authenticates.", choices=("off", "invite", "open"), restart=True),
    Option(("auth", "oidc", "scopes"), "Scopes", "list",
           "Requested at sign-in. One per line.", restart=True),

    Option(("price_feed", "quotes_enabled"), "Live quotes", "bool",
           "Whether prices refresh while a market is open, as well as at the "
           "daily close.", restart=True),
    Option(("price_feed", "quote_interval_minutes"), "Live quote interval (minutes)",
           "int", "How often live quotes refresh while a market is open.",
           minimum=1, maximum=1440),
    Option(("price_feed", "timezone"), "Feed timezone", "text",
           "The zone the daily run is scheduled in. Empty follows the timezone above.",
           optional=True),

    Option(("maintenance", "enabled"), "Daily housekeeping", "bool",
           "Whether the nightly sweep runs: expired sessions, old sign-in "
           "attempts, half-finished imports.", restart=True),
    Option(("maintenance", "hour"), "Housekeeping hour", "int",
           "Hour of the nightly sweep.",
           minimum=0, maximum=23, restart=True),
    Option(("maintenance", "minute"), "Housekeeping minute", "int",
           "Minute of the nightly sweep.", minimum=0, maximum=59, restart=True),
    Option(("maintenance", "attempt_retention_days"), "Keep sign-in attempts (days)",
           "int", "How long failed sign-ins are kept. They drive lockout; they "
           "are not an audit log.", minimum=1, maximum=365),
    Option(("maintenance", "staged_upload_hours"), "Keep unfinished imports (hours)",
           "int", "How long a previewed but uncommitted import is kept. It holds real "
           "trade data.", minimum=1, maximum=720),

    Option(("metrics", "enabled"), "Prometheus metrics", "bool",
           "Whether /metrics is served. It publishes machine health only — no "
           "holdings, no values.", restart=True),

    Option(("imports", "max_upload_mb"), "Maximum upload (MB)", "int",
           "The largest file an import will accept.", minimum=1, maximum=200),
    Option(("imports", "ocr", "enabled"), "Read scanned PDFs", "bool",
           "Whether image-only PDFs are put through local OCR. Nothing is sent "
           "anywhere.",
           restart=True),

    Option(("imports", "allow_new_instruments"), "Create instruments on import", "bool",
           "Whether an import may create instruments it doesn't recognise. Off "
           "stops the import on an unknown ticker."),
)

# Optional features, appended from the registry rather than typed out here:
# a feature's name and description live in `app/features.py`, and repeating
# them would give the settings page a second chance to disagree with the setup
# wizard about what a feature is.
OPTIONS = OPTIONS + tuple(
    Option(("features", f.key), f.name, "bool", f.blurb)
    for f in features.FEATURES
)

BY_NAME = {o.name: o for o in OPTIONS}


def config_path() -> Path:
    return Path(CONFIG_FILE)


# The annotated default, shipped beside the app so the first-run wizard has
# something to write *from* rather than emitting a bare four-line file. The
# Dockerfile copies the repo's config.yaml here; running from a source checkout,
# that same file is the fallback.
def default_template() -> Path | None:
    """The documented config to base a new file on, if it shipped."""
    for candidate in (Path(__file__).resolve().parent.parent / "config.default.yaml",
                      Path(__file__).resolve().parent.parent / "config.yaml"):
        if candidate.is_file():
            return candidate
    return None


# --------------------------------------------------------------------------- #
# Can we write it?
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Writability:
    writable: bool
    reason: str = ""


def writability() -> Writability:
    """Whether this process can actually save the config file.

    Probed by writing, not by inspecting permission bits: the common case is a
    read-only *mount* (a ConfigMap, or a volume mounted ro), where the bits can
    look fine and the write still fails with EROFS. Saving replaces the file
    atomically via a neighbouring temporary file, so the directory has to be
    writable too — which is exactly what this probes.
    """
    path = config_path()
    if not path.exists():
        return Writability(False, f"No configuration file at {path}.")
    try:
        handle = tempfile.NamedTemporaryFile(dir=path.parent, prefix=".config-probe-",
                                             delete=True)
        handle.close()
    except OSError as exc:
        return Writability(False, _explain(path, exc))
    if not os.access(path, os.W_OK):
        return Writability(False, _explain(path, None))
    return Writability(True)


def _explain(path: Path, exc: OSError | None) -> str:
    """Say why the file cannot be written, in terms of how it got there."""
    detail = f" ({exc.strerror})" if exc is not None and exc.strerror else ""
    return (
        f"The configuration file at {path} is presented to the application as "
        f"read-only{detail}, which is normal for a container: it is typically "
        "supplied by a Kubernetes ConfigMap, a read-only volume mount, or baked "
        "into the image. Nothing on this page can be saved from here. Change "
        "these values wherever that file is defined, then restart the "
        "application."
    )


# --------------------------------------------------------------------------- #
# Reading and writing
# --------------------------------------------------------------------------- #

def _yaml():
    from ruamel.yaml import YAML

    yaml = YAML()
    yaml.preserve_quotes = True
    # Keep the shipped file's indentation so a save produces a minimal diff.
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def load() -> dict:
    """The file's own contents — what is *written down*, which is not always
    what the app is running (an environment variable silently wins)."""
    path = config_path()
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return _yaml().load(handle) or {}


def _dig(data: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(data, dict) or key not in data:
            return None
        data = data[key]
    return data


def effective(settings, option: Option) -> Any:
    """The value the app is actually using, from the live settings object."""
    value: Any = settings
    for key in option.path:
        value = getattr(value, key)
    return value


def overridden_by_environment(option: Option) -> bool:
    """Whether an environment variable is winning over the file for this option.

    Worth surfacing: saving the file would change nothing a reader can see, and
    a page that pretended otherwise would be lying.
    """
    return f"APP_{'__'.join(p.upper() for p in option.path)}" in os.environ


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def coerce(option: Option, raw: str) -> Any:
    """Turn one submitted form value into a typed one, or raise ValueError with
    a message meant for the person who typed it."""
    raw = (raw or "").strip()
    if option.kind == "bool":
        return raw.lower() in ("1", "true", "on", "yes")
    if option.kind == "int":
        try:
            number = int(raw)
        except ValueError:
            raise ValueError(f"{option.label} must be a whole number.") from None
        if option.minimum is not None and number < option.minimum:
            raise ValueError(f"{option.label} must be at least {option.minimum}.")
        if option.maximum is not None and number > option.maximum:
            raise ValueError(f"{option.label} must be at most {option.maximum}.")
        return number
    if option.kind == "date":
        try:
            return dt.date.fromisoformat(raw)
        except ValueError:
            raise ValueError(f"{option.label} must be a date, as YYYY-MM-DD.") from None
    if option.kind == "list":
        # One per line. Split here rather than on commas because an IPv6 range
        # and a URL both contain characters a comma-split would ruin.
        return [line.strip() for line in raw.splitlines() if line.strip()]
    if option.kind == "choice":
        if raw not in option.choices:
            raise ValueError(f"{option.label} must be one of {', '.join(option.choices)}.")
        return raw
    if option.path == ("timezone",):
        try:
            ZoneInfo(raw)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(
                f"{raw!r} is not a known timezone. Use an IANA name such as "
                "Australia/Melbourne."
            ) from None
    if not raw:
        if option.optional:
            return None
        raise ValueError(f"{option.label} cannot be empty.")
    return raw


def not_yet_provided(values: dict[str, Any]) -> dict[str, Any]:
    """The subset of `values` this installation does not already supply.

    The wizard applies its choices to the RUNNING settings before the summary
    is drawn, so comparing against those would say everything matches. This
    compares against what survives a restart instead: the file on disk, and any
    environment variable for the same path — an env var wins at load, so a
    value it provides needs nothing written down.

    Without this, somebody whose config.yaml already set their timezone and
    turned the feed on was told to go and set both — issue #18.
    """
    try:
        on_disk = load()
    except OSError:
        # Unreadable is not "absent": telling somebody to add settings when we
        # simply could not look would be worse than saying nothing.
        return dict(values)

    def walk(wanted: dict, existing: Any, path: tuple[str, ...]) -> dict:
        out: dict[str, Any] = {}
        for key, value in wanted.items():
            here = path + (key,)
            current = existing.get(key) if isinstance(existing, dict) else None
            if isinstance(value, dict):
                nested = walk(value, current, here)
                if nested:
                    out[key] = nested
            elif not _already(here, value, current):
                out[key] = value
        return out

    return walk(values, on_disk, ())


def _already(path: tuple[str, ...], wanted: Any, current: Any) -> bool:
    """Whether this installation already provides this value after a restart."""
    if f"APP_{'__'.join(p.upper() for p in path)}" in os.environ:
        return True
    if current is None:
        return False
    # Compared as text: YAML gives back `True` where the form gave "true", and
    # a date where the draft holds one. Both mean the same to a reader.
    return str(current).strip().lower() == str(wanted).strip().lower()


def save(values: dict[str, Any]) -> None:
    """Write the given `{option name: typed value}` back to config.yaml.

    Only the scalars named are touched; every other line, comment and blank
    keeps its place. Written to a temporary file and renamed, so an interrupted
    save cannot leave a half-written config behind.
    """
    path = config_path()
    yaml = _yaml()
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.load(handle) or {}

    for name, value in values.items():
        option = BY_NAME[name]
        cursor = data
        for key in option.path[:-1]:
            if key not in cursor or not isinstance(cursor[key], dict):
                cursor[key] = {}
            cursor = cursor[key]
        # Dates round-trip as strings; ruamel would otherwise emit a bare date
        # that reads back as a string on some loaders.
        cursor[option.path[-1]] = value.isoformat() if isinstance(value, dt.date) else value

    tmp = path.with_name(f".{path.name}.new")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            yaml.dump(data, handle)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def creatable() -> Writability:
    """Whether a config file could be *created* here — the wizard's question.

    `writability()` answers "can I change the file that exists", and reports a
    missing file as unwritable, which is right for the admin page and wrong
    here: on a fresh container the file is supposed to be missing. What matters
    is the directory.
    """
    path = config_path()
    if path.exists():
        return writability()
    parent = path.parent
    if not parent.exists():
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return Writability(False, _cannot_create(parent, exc))
    try:
        handle = tempfile.NamedTemporaryFile(dir=parent, prefix=".config-probe-",
                                             delete=True)
        handle.close()
    except OSError as exc:
        return Writability(False, _cannot_create(parent, exc))
    return Writability(True)


def _cannot_create(parent: Path, exc: OSError) -> str:
    detail = f" ({exc.strerror})" if exc.strerror else ""
    return (
        f"The application cannot create a configuration file in {parent}"
        f"{detail}. That is normal in Kubernetes, where the file comes from a "
        f"read-only ConfigMap. Everything chosen here will still be applied to "
        f"the running application, and the last step will show you the YAML to "
        f"save wherever that file is defined."
    )


def write_tree(values: dict[str, Any]) -> None:
    """Merge a nested dict into config.yaml, creating the file if it is absent.

    Distinct from `save()`, which edits the fixed set of options the admin page
    exposes. The wizard writes keys that page deliberately does not offer —
    `database` above all — and has to be able to produce the file from nothing.

    A new file starts from the shipped annotated default, so what the wizard
    creates is the documented config with a few values filled in rather than a
    stub. That matters more than it sounds: the file is the main documentation
    anybody reads, and one born without its comments never grows them back.
    """
    path = config_path()
    yaml = _yaml()

    if path.exists():
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.load(handle) or {}
    else:
        template = default_template()
        if template is not None:
            with template.open("r", encoding="utf-8") as handle:
                data = yaml.load(handle) or {}
        else:                                   # pragma: no cover - the template ships in both the image and the source tree; this is the belt-and-braces path for a stripped install, and it produces a valid (if undocumented) file.
            data = {}
        path.parent.mkdir(parents=True, exist_ok=True)

    _merge(data, values)

    tmp = path.with_name(f".{path.name}.new")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            yaml.dump(data, handle)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _merge(target: Any, values: dict[str, Any]) -> None:
    """Set the given keys, leaving every other line — and every comment — alone.

    Recursive so that writing `auth.trusted_proxies` does not replace the whole
    `auth:` block and take its documentation with it.
    """
    for key, value in values.items():
        if isinstance(value, dict):
            if not isinstance(target.get(key), dict):
                target[key] = {}
            _merge(target[key], value)
        else:
            target[key] = value


def render(values: dict[str, Any]) -> str:
    """The YAML the wizard would have written, as text.

    For the read-only case: the page cannot save it, so it shows it, and
    someone pastes it into wherever their ConfigMap lives.
    """
    import io

    buffer = io.StringIO()
    _yaml().dump(values, buffer)
    return buffer.getvalue()


def apply_live(settings, values: dict[str, Any]) -> list[str]:
    """Push saved values onto the running settings object.

    Returns the labels of any that will NOT take effect until a restart, so the
    page can say so instead of implying everything is live.
    """
    from . import clock

    pending: list[str] = []
    for name, value in values.items():
        option = BY_NAME[name]
        target = settings
        for key in option.path[:-1]:
            target = getattr(target, key)
        setattr(target, option.path[-1], value)
        if option.restart:
            pending.append(option.label)

    clock.configure(settings.timezone)
    if settings.price_feed.timezone is None:
        settings.price_feed.timezone = settings.timezone
    return pending

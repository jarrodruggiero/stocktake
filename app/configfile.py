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
    kind: str                  # text | int | bool | date | choice
    blurb: str
    restart: bool = False      # does a change wait for a restart?
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
           "Every date decision — whether a trade is in the future, which financial "
           "year is current, what the calendar highlights — is made in this zone. "
           "An IANA name, e.g. Australia/Melbourne or Europe/Dublin."),
    Option(("log_level",), "Log level", "choice",
           "How much the application writes to its log.",
           restart=True, choices=("DEBUG", "INFO", "WARNING", "ERROR")),

    Option(("price_feed", "enabled"), "Fetch prices", "bool",
           "Whether the daily price and exchange-rate feed runs at all. With it "
           "off, valuations stay at the last stored close.", restart=True),
    Option(("price_feed", "hour"), "Feed hour", "int",
           "Hour of the daily run, in the timezone above. Set it after the market "
           "you follow has closed.", minimum=0, maximum=23),
    Option(("price_feed", "minute"), "Feed minute", "int",
           "Minute of the daily run.", minimum=0, maximum=59),
    Option(("price_feed", "backfill_start"), "Backfill from", "date",
           "How far back to fetch price history the first time an instrument is "
           "seen. Earlier means a longer first run and a longer chart."),

    Option(("auth", "session_ttl_days"), "Session length (days)", "int",
           "How long a signed-in session lasts. It slides: every request pushes "
           "the expiry out again.", minimum=1, maximum=365),
    Option(("auth", "cookie_secure"), "HTTPS-only cookie", "bool",
           "Send the session cookie only over HTTPS. Turn this ON once the app is "
           "behind TLS, and leave it off on a plain-HTTP LAN or nobody can sign in."),
    Option(("auth", "rate_limit", "max_attempts"), "Failed sign-ins allowed", "int",
           "Failures from one email and address before that combination is locked "
           "out.", minimum=1, maximum=100),
    Option(("auth", "rate_limit", "window_minutes"), "Counting window (minutes)", "int",
           "How long failures are remembered when counting towards a lockout.",
           minimum=1, maximum=1440),
    Option(("auth", "rate_limit", "lockout_minutes"), "Lockout (minutes)", "int",
           "How long a locked-out combination has to wait.", minimum=1, maximum=1440),

    Option(("imports", "allow_new_instruments"), "Create instruments on import", "bool",
           "Whether a broker CSV may create instruments it does not recognise. Off "
           "means an unknown ticker stops the import so you can check it first."),
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
        raise ValueError(f"{option.label} cannot be empty.")
    return raw


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

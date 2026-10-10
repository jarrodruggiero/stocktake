"""Every configuration field is either editable in Admin → Settings, or is
listed here with a reason it is not.

The failure this prevents is quiet: somebody adds a setting to `settings.py`,
documents it in `configuration.md`, and it can only ever be changed by editing
YAML on the server — which is exactly the trip the settings page exists to save.
Nothing else notices, because nothing else knows the field was supposed to be
reachable.

Adding a field therefore forces a decision: expose it, or say why not.
"""

from __future__ import annotations

from pydantic import BaseModel

from app import configfile
from app.settings import PortfolioSettings

# Fields deliberately not editable from the page, and why. A reason each,
# because "not exposed" without one is indistinguishable from an oversight.
WITHHELD: dict[str, str] = {
    "app_name": "Branding, fixed at build time. The page shows it as a fact.",

    # Changing the database from inside the application that is using it is a
    # way to lock yourself out of both. The wizard sets it once, on an install
    # that has nothing to lose yet.
    "database.type": "Set by the setup wizard; changing it live orphans the data.",
    "database.path": "As above.",
    "database.host": "As above.",
    "database.port": "As above.",
    "database.name": "As above.",
    "database.user": "As above.",
    "database.password": "As above — and a password field that renders its own "
                         "value is a password on a screen.",
    "database.sslmode": "As above.",

    "auth.cookie_name": "Renaming it signs everybody out, and nothing is gained.",

    # Same reason as the database password: a field that renders its own value
    # puts a live credential on a screen somebody may be sharing.
    "auth.oidc.client_secret": "A credential. Set it in the file or the "
                               "environment, never on a page.",

    # Paths the process reads and writes. A typo here breaks imports until
    # somebody edits YAML anyway, which is the opposite of the point.
    "imports.templates_dir": "A server path, not a preference.",
    "imports.visual_dir": "A server path, not a preference.",

    # Per-broker column mappings: structured data with its own editor.
    "imports.brokers.kind": "Edited in the broker designer, not as a scalar.",
    "imports.brokers.exchange": "As above.",
    "imports.brokers.currency": "As above.",
    "imports.brokers.date_format": "As above.",
    "imports.brokers.columns": "As above.",
    "imports.brokers.actions": "As above.",
    "imports.brokers.times_zone": "As above.",
}


def _fields(model: type[BaseModel], prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """Every leaf field in the settings tree, as a dotted path.

    Nested models are walked; the two dict-of-model fields are not, because
    their keys are broker names rather than settings.
    """
    out: list[tuple[str, ...]] = []
    for name, field in model.model_fields.items():
        path = prefix + (name,)
        annotation = field.annotation
        nested = None
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            nested = annotation
        else:
            for arg in getattr(annotation, "__args__", ()):
                if isinstance(arg, type) and issubclass(arg, BaseModel):
                    nested = arg
        if nested is not None:
            out.extend(_fields(nested, path))
        else:
            out.append(path)
    return out


def test_every_setting_is_editable_or_withheld_with_a_reason():
    editable = {".".join(o.path) for o in configfile.OPTIONS}
    unaccounted = [
        ".".join(path) for path in _fields(PortfolioSettings)
        if ".".join(path) not in editable and ".".join(path) not in WITHHELD
    ]

    assert not unaccounted, (
        "Add each of these to configfile.OPTIONS, or to WITHHELD with a reason: "
        f"{unaccounted}")


def test_nothing_is_both_editable_and_withheld():
    """A field in both lists means the reason is stale — it IS reachable, and
    the sentence saying why it isn't is now misinformation."""
    editable = {".".join(o.path) for o in configfile.OPTIONS}

    assert not (editable & set(WITHHELD))


def test_every_option_names_a_real_setting():
    """An option whose path no longer exists renders an empty box that saves a
    key nothing reads. Renaming a settings field is exactly when this happens.
    """
    known = {".".join(path) for path in _fields(PortfolioSettings)}
    # Feature toggles are generated from features.FEATURES onto a dict-shaped
    # settings field, so they are legitimately absent from the leaf walk.
    stray = [".".join(o.path) for o in configfile.OPTIONS
             if ".".join(o.path) not in known and o.path[0] != "features"]

    assert not stray


def test_optional_settings_accept_being_left_empty():
    """`optional` is what lets a box be cleared rather than only re-typed. A
    required field would refuse, which for rp_id would mean passkeys could be
    configured but never unconfigured.
    """
    for option in configfile.OPTIONS:
        if option.optional:
            assert configfile.coerce(option, "") in (None, [])


# --------------------------------------------------------------------------- #
# The defaults the shipped config.yaml writes down
# --------------------------------------------------------------------------- #
# config.yaml documents each setting with its default commented out, and the
# pull request template asks for exactly that. Nothing compared the two: a
# mutation run changed every default (15 minutes to 16, 3 a.m. to 4) and no
# test failed, while the file went on telling people the old value.

# Commented lines that show something to copy rather than the default, and why.
EXAMPLES: dict[str, str] = {
    "database.host": "Where a Postgres server might be; the default suits one cluster.",
    "database.name": "An example name; the default is empty, which the wizard asks for.",
    "database.user": "As above.",
    "price_feed.timezone": "Unset follows the top-level timezone; the line shows a zone.",
    "auth.webauthn.rp_id": "The install's own domain; unset uses the request's.",
    "auth.webauthn.rp_name": "As above, for the name a passkey prompt shows.",
    "auth.oidc.issuer": "The provider's address, which only the operator knows.",
    "auth.oidc.client_id": "As above.",
    "auth.oidc.client_secret": "As above, and a credential.",
    "auth.oidc.redirect_uri": "As above.",
}


def _documented_defaults() -> dict[str, object]:
    """Every commented `key: value` line in config.yaml, by dotted path.

    A commented line's depth is where its key starts once the `# ` is taken
    off, so `#   window_minutes: 15` under `# rate_limit:` under `auth:` is
    auth.rate_limit.window_minutes. Prose comments do not start with a
    lower-case key and a colon, so they are passed over."""
    import re
    from pathlib import Path

    import yaml

    text = (Path(__file__).resolve().parent.parent / "config.yaml").read_text()
    stack: list[tuple[int, str]] = []
    out: dict[str, object] = {}
    line = re.compile(r"^(\s*)(#\s)?(\s*)([a-z_][a-z0-9_]*):(?:\s+(.*?))?(?:\s+#.*)?$")
    for raw in text.splitlines():
        found = line.match(raw)
        if not found:
            continue
        lead, hashed, extra, key, value = found.groups()
        depth = len(lead) + (len(extra) if hashed else 0)
        while stack and stack[-1][0] >= depth:
            stack.pop()
        if not value:
            stack.append((depth, key))
            continue
        if hashed:
            out[".".join([k for _, k in stack] + [key])] = yaml.safe_load(value)
    return out


def test_every_default_config_yaml_writes_down_is_the_default():
    defaults = PortfolioSettings.model_construct()
    compared = 0
    for dotted, written in _documented_defaults().items():
        value = defaults
        for part in dotted.split("."):
            value = getattr(value, part, KeyError)
        if value is KeyError or dotted in EXAMPLES:
            continue
        compared += 1
        assert value == written, f"config.yaml says {dotted}: {written!r}; the default is {value!r}"
    assert compared >= 30, "the sweep found too few documented defaults to mean anything"


def test_every_example_in_the_list_is_still_in_the_file():
    stale = sorted(set(EXAMPLES) - set(_documented_defaults()))
    assert not stale, f"EXAMPLES names lines config.yaml no longer has: {stale}"

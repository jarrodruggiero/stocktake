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

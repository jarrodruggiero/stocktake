"""Installing import templates through the interface, rather than by hand.

The mapper can already *produce* a template. Until now the only way to use one
was to put the file on the container's disk yourself, which is not something
you can ask of anyone testing a change or trying a format somebody shared —
and on Kubernetes the obvious place to put it (`/config`) is a read-only
ConfigMap.

So templates upload, list and delete from the imports page, and land in a
directory the loaders actually read.

Two things here are security properties rather than conveniences:

  * **The filename is attacker-controlled.** `../../etc/cron.d/x` is a filename.
    Everything written is slugged and re-joined, and the result is checked to
    be inside the target directory before anything touches the disk.
  * **A template is validated before it is installed**, not on first use.
    A broken file that parses as YAML but not as a template would otherwise sit
    there until somebody uploaded a statement, and fail as "your statement is
    not recognised".

Templates are declarative and parsed with `YAML(typ="safe")`, so a hostile file
is a parsing problem rather than a code-execution one. That is the reason this
can be a feature at all.
"""

from __future__ import annotations

import io

import pytest

from test_routes import make_login, session_csrf

HTML = {"accept": "text/html"}


@pytest.fixture
def admin(client, session_factory, tmp_path, monkeypatch, app_module):
    """Signed in as an admin, with the drop-in directory pointed at tmp_path.

    Both halves matter: installing a template is admin-only, and the real
    default is `/data/formats`, which a test must never write to.
    """
    monkeypatch.setattr(app_module.settings.imports, "templates_dir", str(tmp_path))
    make_login(client, session_factory)
    return tmp_path

GOOD_STATEMENT = b"""
name: Example Registry
match:
  any_of:
    - Example Registry Services
fields:
  payment_date:
    after: ["Payment date"]
    type: date
  net_amount:
    after: ["Net amount"]
    type: money
"""

GOOD_BROKER = b"""
kind: mapped
exchange: ASX
currency: AUD
date_format: "%d/%m/%Y"
columns:
  date: Trade Date
  action: Buy/Sell
  ticker: Code
  units: Units
  price: Price
  brokerage: Brokerage
"""


def _upload(client, session_factory, *, name="example-registry.yaml",
            body=GOOD_STATEMENT, kind="statement"):
    return client.post(
        "/imports-exports/formats",
        files={"file": (name, io.BytesIO(body), "application/x-yaml")},
        data={"kind": kind, "_csrf": session_csrf(session_factory)},
        headers=HTML, follow_redirects=False)


# --------------------------------------------------------------------------- #
# It works
# --------------------------------------------------------------------------- #

def test_a_statement_template_can_be_uploaded(client, session_factory, admin, tmp_path):
    resp = _upload(client, session_factory)

    assert resp.status_code == 303
    assert (tmp_path / "statements" / "example-registry.yaml").is_file()


def test_an_uploaded_template_is_then_actually_used(client, session_factory, admin, tmp_path):
    """The point of the whole thing. A file that lands somewhere the loader
    does not read is a file that did nothing."""
    from app import docformats

    _upload(client, session_factory)

    keys = [t.key for t in docformats.load_statement_templates(
        (tmp_path / "statements"))]

    assert "example-registry" in keys


def test_a_broker_format_can_be_uploaded(client, session_factory, admin, tmp_path):
    resp = _upload(client, session_factory, name="mybroker.yaml",
                   body=GOOD_BROKER, kind="broker")

    assert resp.status_code == 303
    assert (tmp_path / "brokers" / "mybroker.yaml").is_file()


def test_the_name_is_slugged_to_the_shipped_convention(client, session_factory, admin, tmp_path):
    """`Vanguard DRP Advice.YAML` becomes `vanguard-drp-advice.yaml` — the same
    convention the shipped files and the designer's download use, so an upload
    can be contributed as a pull request without renaming."""
    _upload(client, session_factory, name="Vanguard DRP Advice.YAML")

    assert (tmp_path / "statements" / "vanguard-drp-advice.yaml").is_file()


# --------------------------------------------------------------------------- #
# It refuses what it should
# --------------------------------------------------------------------------- #

def test_a_traversing_filename_cannot_escape_the_directory(client, session_factory, admin, tmp_path):
    """The filename comes from the client. `../../` is a filename."""
    _upload(client, session_factory, name="../../../../tmp/escaped.yaml")

    assert not (tmp_path.parent / "escaped.yaml").exists()
    written = list((tmp_path / "statements").glob("*.yaml"))
    for path in written:
        assert tmp_path in path.resolve().parents


def test_a_file_that_is_not_a_template_is_refused(client, session_factory, admin, tmp_path):
    """Valid YAML, not a valid template. Caught now rather than surfacing later
    as "your statement wasn't recognised"."""
    resp = _upload(client, session_factory, body=b"just: a mapping\n")

    assert resp.status_code == 303
    assert "error" in resp.headers["location"]
    assert not (tmp_path / "statements").exists() or not list(
        (tmp_path / "statements").glob("*.yaml"))


def test_malformed_yaml_is_refused_without_a_traceback(client, session_factory, admin, tmp_path):
    resp = _upload(client, session_factory, body=b"fields: [this is not\n  valid: yaml\n")

    assert resp.status_code == 303
    assert "error" in resp.headers["location"]


def test_only_an_admin_may_install_one(client, session_factory, admin, tmp_path):
    """A template changes how EVERY user's statements are parsed — it is not
    portfolio-scoped, so it is not a per-member decision."""
    # The fixture signed in an admin; drop that and come back as a member.
    client.cookies.clear()
    make_login(client, session_factory, email="member@example.test", admin=False)

    resp = _upload(client, session_factory)

    assert resp.status_code == 403
    assert not (tmp_path / "statements").exists()


def test_it_needs_the_csrf_token(client, session_factory, admin, tmp_path):
    resp = client.post("/imports-exports/formats",
                       files={"file": ("x.yaml", io.BytesIO(GOOD_STATEMENT),
                                       "application/x-yaml")},
                       data={"kind": "statement"}, headers=HTML)

    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# Seeing and removing what you installed
# --------------------------------------------------------------------------- #

def test_installed_templates_are_listed(client, session_factory, admin, tmp_path):
    """You cannot manage what you cannot see — and a template that silently
    overrides a shipped one is exactly the thing to be able to find."""
    _upload(client, session_factory)

    page = client.get("/imports-exports", headers=HTML).text

    assert "example-registry" in page


def test_one_can_be_removed_again(client, session_factory, admin, tmp_path):
    _upload(client, session_factory)

    resp = client.post("/imports-exports/formats/statement/example-registry/delete",
                       data={"_csrf": session_csrf(session_factory)},
                       headers=HTML, follow_redirects=False)

    assert resp.status_code == 303
    assert not (tmp_path / "statements" / "example-registry.yaml").exists()


def test_the_slug_reduces_a_traversing_name_to_a_harmless_one():
    """The delete route re-slugs the name from the URL before using it.

    Deliberately NOT tested through HTTP: Starlette refuses a traversing path
    before routing — `..%2Fvictim`, `..%252Fvictim` and `%2e%2e%2fvictim` all
    404 without the handler running — so an HTTP-level test of this could never
    fail, whatever the route did. It would look like coverage and be none.

    The `_slug` call in the route is therefore belt-and-braces behind the
    router, and this is the assertion that actually holds it in place.
    """
    from app import imports_web

    assert imports_web._slug("../victim") == "victim"
    assert imports_web._slug("../../etc/cron.d/x") == "x"
    assert imports_web._slug("/etc/passwd") == "passwd"
    assert imports_web._slug("Vanguard DRP Advice.YAML") == "vanguard-drp-advice"


def test_the_path_builder_refuses_to_leave_the_directory_on_its_own(
        client, session_factory, admin, tmp_path, app_module):
    """`_slug` strips every dangerous character, so the containment check in
    `_template_path` never fires in practice — which means it is untested by
    every route-level test, and would go unnoticed if it were removed.

    Tested directly, with a slug that never passed through `_slug`. Two layers
    only help if the second one works when the first is loosened.
    """
    import pytest
    from fastapi import HTTPException

    from app import imports_web

    settings = app_module.settings
    with pytest.raises(HTTPException) as caught:
        imports_web._template_path(settings, "statement", "../escape")

    assert caught.value.status_code == 400
    # …and it still builds an ordinary path for an ordinary name.
    good = imports_web._template_path(settings, "statement", "example-registry")
    assert good.parent == settings.imports.user_dir("statement").resolve()


def test_a_shipped_template_cannot_be_deleted(client, session_factory, admin, tmp_path):
    """Shipped formats live in the package, not the drop-in directory. Deleting
    one would mean editing the installed application."""
    resp = client.post("/imports-exports/formats/statement/generic-au/delete",
                       data={"_csrf": session_csrf(session_factory)},
                       headers=HTML, follow_redirects=False)

    from app import docformats
    assert (docformats.BUILTIN_DIR / "statements" / "generic-au.yaml").is_file()
    assert resp.status_code in (303, 404)

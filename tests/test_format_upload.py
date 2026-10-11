"""Installing import templates through the interface, rather than by hand.

Putting a file on the container's disk is not something you can ask of anyone
trying a format somebody shared, and on Kubernetes `/config` is a read-only
ConfigMap. So templates upload, list and delete from the imports page.

Two things here are security properties, not conveniences:

  * **The filename is attacker-controlled** — `../../etc/cron.d/x` is a
    filename. Everything is slugged, re-joined, and checked to be inside the
    target directory before anything touches the disk.
  * **A template is validated before install**, not on first use, or a broken
    one sits there until somebody uploads a statement and fails as "not
    recognised".

Templates are inert, which is what makes this a feature at all (decisions.md
#38).
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


def test_a_name_too_long_to_be_a_filename_is_cut(client, session_factory, admin, tmp_path):
    """The app picks the filename, so a long one is cut rather than refused,
    and it was a filesystem error before (decisions.md #131). Pasted, because
    a name this long in an upload's own header is refused by the framework."""
    resp = client.post(
        "/imports-exports/formats", headers=HTML, follow_redirects=False,
        data={"kind": "statement", "filename": "a" * 5000,
              "body": GOOD_STATEMENT.decode(), "_csrf": session_csrf(session_factory)})

    assert resp.status_code == 303
    assert (tmp_path / "statements" / ("a" * 60 + ".yaml")).is_file()


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


# --------------------------------------------------------------------------- #
# What the last mutation pass found the install routes left unchecked
# --------------------------------------------------------------------------- #

def _installed_or_error(resp) -> str:
    from urllib.parse import unquote_plus

    return unquote_plus(resp.headers["location"])


def test_the_first_install_makes_the_folders_and_a_second_shares_them(
        client, session_factory, admin, tmp_path, app_module, monkeypatch):
    """A fresh install has no formats folder at all; the next template goes
    into the folder the first one made."""
    monkeypatch.setattr(app_module.settings.imports, "templates_dir", str(tmp_path / "formats"))

    first = _upload(client, session_factory)
    second = _upload(client, session_factory, name="other-registry.yaml")

    assert (first.status_code, second.status_code) == (303, 303)
    assert sorted(p.name for p in (tmp_path / "formats" / "statements").iterdir()) == [
        "example-registry.yaml", "other-registry.yaml"]


def test_installing_and_removing_say_who_did_it_in_the_log(client, session_factory, admin,
                                                           caplog):
    import logging

    caplog.set_level(logging.INFO, logger="app.imports_web")
    _upload(client, session_factory)
    client.post("/imports-exports/formats/statement/example-registry/delete",
                data={"_csrf": session_csrf(session_factory)}, headers=HTML,
                follow_redirects=False)

    said = [r.getMessage() for r in caplog.records if r.name == "app.imports_web"]
    assert any(m.startswith("installed statement template example-registry by user ")
               for m in said)
    assert any(m.startswith("removed statement template example-registry by user ")
               for m in said)


@pytest.mark.parametrize("filename, slug", [("pasted-registry.yaml", "pasted-registry"),
                                            ("", "template")])
def test_pasted_yaml_beside_an_empty_file_field_is_what_installs(client, session_factory,
                                                                 admin, tmp_path, filename,
                                                                 slug):
    """The designers post the template as text, and a browser sends the page's
    file input as an empty part beside it."""
    resp = client.post(
        "/imports-exports/formats", headers=HTML, follow_redirects=False,
        files={"file": ("", io.BytesIO(b""), "application/octet-stream")},
        data={"kind": "statement", "body": GOOD_STATEMENT.decode(), "filename": filename,
              "_csrf": session_csrf(session_factory)})

    assert _installed_or_error(resp).endswith(f"installed={slug}")
    assert (tmp_path / "statements" / f"{slug}.yaml").is_file()


@pytest.mark.parametrize("body", ["", "   \n"], ids=["empty", "spaces"])
def test_nothing_to_install_is_said_so(client, session_factory, admin, body):
    resp = client.post("/imports-exports/formats", headers=HTML, follow_redirects=False,
                       data={"kind": "statement", "body": body,
                             "_csrf": session_csrf(session_factory)})

    assert _installed_or_error(resp).endswith("error=Choose a template file.")


@pytest.mark.parametrize("kind, slug, said", [("nonsense", "x", "unknown template kind"),
                                              ("statement", "", "that file needs a name")])
def test_the_path_builder_refuses_an_unknown_kind_and_a_missing_name(app_module, kind, slug,
                                                                    said):
    from fastapi import HTTPException

    from app import imports_web

    with pytest.raises(HTTPException) as caught:
        imports_web._template_path(app_module.settings, kind, slug)
    assert (caught.value.status_code, caught.value.detail) == (400, said)


def test_a_template_of_rows_alone_installs(client, session_factory, admin, tmp_path):
    """A combined advice has no one payment to read: its rows are everything."""
    rows_only = (b'name: Rows only\nrows:\n  shape: "TICKER NUM INT NUM NUM NUM NUM INT NUM"\n'
                 b"  columns: [ticker, drp_price, units_held, per_security, tax_withheld,\n"
                 b"            amount, brought_forward, allotted, carried_forward]\n")

    resp = _upload(client, session_factory, name="rows-only.yaml", body=rows_only)

    assert _installed_or_error(resp).endswith("installed=rows-only")


@pytest.mark.parametrize("body, said", [
    (b"name: No kind\nexchange: ASX\n", "That is not a broker format — it needs a `kind`."),
    # The model's own sentence, not pydantic's "Value error, …" around it.
    (GOOD_BROKER.replace(b"currency: AUD", b"currency: AUSD"),
     "A currency is a three-letter code, like AUD or USD."),
    (GOOD_BROKER.replace(b"exchange: ASX", b"exchange: [1, 2]"),
     "Input should be a valid string"),
], ids=["no-kind", "model-sentence", "type-error"])
def test_a_broker_format_is_refused_with_its_reason(client, session_factory, admin, body, said):
    resp = _upload(client, session_factory, name="bad.yaml", body=body, kind="broker")

    assert _installed_or_error(resp).endswith(f"error=bad.yaml: {said}")


def test_the_slug_keeps_a_dotted_name_and_never_ends_on_a_hyphen():
    from app import imports_web

    assert imports_web._slug("my.registry.yaml") == "my-registry"
    cut = imports_web._slug("a" * (imports_web.SLUG_MAX - 1) + " b.yaml")
    assert cut == "a" * (imports_web.SLUG_MAX - 1)


# The statement designer's three ways out: download, install and contribute.

GOOD_MAPPING = {"net_amount": {"after": ["Net amount"], "type": "money"},
                "payment_date": {"after": ["Payment date"], "type": "date"}}


def _designed(client, factory, route, **fields):
    import json

    return client.post(f"/imports-exports/statement/design/{route}", headers=HTML,
                       follow_redirects=False,
                       data={"_csrf": session_csrf(factory), "name": "Example Registry",
                             "mapping": json.dumps(GOOD_MAPPING), "marker": "", **fields})


def test_a_design_with_one_unreadable_field_is_refused(client, session_factory, admin):
    import json

    bad = {**GOOD_MAPPING, "payment_date": {"after": "Payment date", "type": "date"}}

    assert _designed(client, session_factory, "export",
                     mapping=json.dumps(bad)).status_code == 400


@pytest.mark.parametrize("name, written", [("Example Registry", "Example Registry"),
                                           ("  ", "My registry")])
def test_a_design_carries_its_name_or_a_plain_one(client, session_factory, admin, name,
                                                  written):
    resp = _designed(client, session_factory, "export", name=name)

    assert f'name: "{written}"' in resp.text


def test_a_design_that_would_read_nothing_is_not_installed(client, session_factory, admin,
                                                           tmp_path, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="app.imports_web")
    refused = _designed(client, session_factory, "install", mapping="{}")
    installed = _designed(client, session_factory, "install")

    assert "error=" in _installed_or_error(refused)
    assert _installed_or_error(installed).endswith("installed=example-registry")
    assert [p.name for p in (tmp_path / "statements").iterdir()] == ["example-registry.yaml"]
    assert any(r.getMessage().startswith("installed designed statement template "
                                         "example-registry by user ") for r in caplog.records)


def test_a_designed_install_makes_the_folders_on_a_fresh_install(client, session_factory, admin,
                                                                tmp_path, app_module,
                                                                monkeypatch):
    monkeypatch.setattr(app_module.settings.imports, "templates_dir", str(tmp_path / "formats"))

    first = _designed(client, session_factory, "install")
    second = _designed(client, session_factory, "install", name="Other Registry")

    assert (first.status_code, second.status_code) == (303, 303)
    assert len(list((tmp_path / "formats" / "statements").iterdir())) == 2


@pytest.mark.parametrize("route, fields, filename", [
    ("statement/design/contribute", {"name": "  "}, "my-registry-contribution.zip"),
    # An empty field takes the form's own default; a name with no letters at
    # all is the one that slugs to nothing.
    ("broker/design/export", {"body": "kind: mapped\n", "filename": "!!!.yaml"}, "broker.yaml"),
], ids=["contribution", "broker-format"])
def test_a_download_with_no_usable_name_gets_a_plain_one(client, session_factory, admin, route,
                                                         fields, filename):
    import json

    data = {"_csrf": session_csrf(session_factory), "mapping": json.dumps(GOOD_MAPPING),
            "marker": "", "text": "", **fields}
    resp = client.post(f"/imports-exports/{route}", data=data, headers=HTML)

    assert resp.headers["content-disposition"] == f'attachment; filename="{filename}"'


def _designer(client, factory, route, **fields):
    return client.post(f"/imports-exports/statement/design/{route}", headers=HTML,
                       data={"_csrf": session_csrf(factory), **fields}).json()


def test_pointing_at_a_date_for_a_money_field_says_they_differ(client, session_factory, admin):
    found = _designer(client, session_factory, "infer", index="2", expect="money",
                      text="Payment date 15/03/2026 Net amount 123.45")

    assert found["type"] == "date" and "expects a money" in found["mismatch"]


def test_pointing_past_the_last_word_finds_no_value_rather_than_the_word_none(
        client, session_factory, admin):
    assert _designer(client, session_factory, "infer", index="999",
                     text="Net amount 123.45")["value"] is None


def test_a_typed_label_is_tried_against_the_document(client, session_factory, admin):
    reading = _designer(client, session_factory, "check", label="Net amount", type="money",
                        text="Net amount 123.45")

    assert (reading["value"], reading["problem"]) == ("123.45", None)

"""The Imports & exports page's arrangement.

Two sections of equal halves — Imports (broker file ·
dividend statement) and Exports (download · what's in each) — and moved the
Import templates section into a **Manage templates** dialog.

The dialog is not just tidying: the section it replaces could install and
remove a template but never hand one back, so a correction made here could not
leave the machine. Download is the verb that closes the loop the contribution
flow opens.
"""

from __future__ import annotations

import re

import pytest

from test_routes import make_login

HTML = {"accept": "text/html"}

GOOD = b"""
name: Example Registry
match:
  any:
    - Example Registry Services
fields:
  payment_date:
    after: ["payment date"]
    type: date
  net_amount:
    after: ["net amount"]
    type: money
"""


@pytest.fixture
def admin(client, session_factory, tmp_path, monkeypatch, app_module):
    """Signed in as an admin, with the drop-in directory pointed at tmp_path —
    the real default is `/data/formats`, which a test must never write to."""
    monkeypatch.setattr(app_module.settings.imports, "templates_dir", str(tmp_path))
    make_login(client, session_factory)
    return tmp_path


def _page(client, session_factory) -> str:
    make_login(client, session_factory)
    response = client.get("/imports-exports", headers=HTML)
    assert response.status_code == 200
    return response.text


def test_the_page_has_an_imports_and_an_exports_section(client, session_factory):
    page = _page(client, session_factory)

    assert '<h2 class="sectionhead">Imports</h2>' in page
    assert '<h2 class="sectionhead">Exports</h2>' in page


def test_the_import_templates_section_is_gone_from_the_page(client, session_factory):
    """It became a button. Templates are something you manage occasionally,
    not a third of a page you read every time you import a file."""
    page = _page(client, session_factory)
    body = page[:page.index("<dialog")]

    assert "Import templates" not in body


def test_managing_templates_is_a_dialog_holding_all_three_verbs(client, session_factory):
    page = _page(client, session_factory)

    assert 'id="opentemplates"' in page, "no Manage templates button"
    found = re.search(r'<dialog id="templatedialog".*?</dialog>', page, re.S)
    assert found, "no Manage templates dialog"
    dialog = found.group(0)

    assert 'action="/imports-exports/formats"' in dialog, "cannot install"
    # Download and remove are per-template rows, so they need one installed —
    # see the download tests below. With none, the dialog says so rather than
    # rendering an empty list.
    assert "Nothing installed" in dialog


def test_the_two_import_panels_are_equal_halves(client, session_factory):
    """`.columns` is 1fr 2fr, which pushed the pair off centre for no reason."""
    page = _page(client, session_factory)

    assert '<section class="evenpair">' in page
    assert '<section class="columns">' not in page


def test_the_dialog_is_reachable_without_script(client, session_factory):
    """A `<dialog>` with no `open` is display:none, so the button being the
    only way in would hide template management entirely."""
    page = _page(client, session_factory)
    assert "<noscript>" in page

    opened = client.get("/imports-exports?templates=1", headers=HTML).text
    assert re.search(r'<dialog id="templatedialog"\s+open>', opened), (
        "?templates=1 does not open the dialog")


def test_an_install_result_reopens_the_dialog(client, session_factory):
    """The install redirects back to /imports-exports. Without this the outcome —
    success or the reason it was refused — would render inside a closed dialog
    and be invisible."""
    make_login(client, session_factory)

    page = client.get("/imports-exports?error=that+is+not+a+template", headers=HTML).text

    assert re.search(r'<dialog id="templatedialog"\s+open>', page)
    assert "that is not a template" in page


# --------------------------------------------------------------------------- #
# Downloading a template — the verb that was missing
# --------------------------------------------------------------------------- #

def _install(client, body: bytes = GOOD, name: str = "example-registry.yaml") -> None:
    token = re.search(r'name="_csrf" value="([^"]+)"',
                      client.get("/imports-exports", headers=HTML).text).group(1)
    response = client.post(
        "/imports-exports/formats",
        data={"_csrf": token, "kind": "statement"},
        files={"file": (name, body, "application/yaml")},
        headers=HTML, follow_redirects=True)
    assert response.status_code == 200


def test_an_installed_template_offers_download_and_remove(client, admin):
    _install(client)

    dialog = re.search(r'<dialog id="templatedialog".*?</dialog>',
                       client.get("/imports-exports", headers=HTML).text, re.S).group(0)

    assert "/imports-exports/formats/statement/example-registry.yaml" in dialog, "no download"
    assert "/imports-exports/formats/statement/example-registry/delete" in dialog, "no remove"


def test_an_installed_template_can_be_downloaded_again(client, admin):
    """The loop the contribution flow opens: correct a shipped layout here,
    then get the corrected file back out to send it on."""
    _install(client)

    got = client.get("/imports-exports/formats/statement/example-registry.yaml")

    assert got.status_code == 200
    assert "attachment" in got.headers["content-disposition"]
    assert "example-registry.yaml" in got.headers["content-disposition"]
    assert "Example Registry" in got.text


def test_downloading_a_template_that_is_not_installed_is_a_404(client, admin):
    assert client.get("/imports-exports/formats/statement/nope.yaml").status_code == 404


def test_a_traversing_slug_cannot_read_a_file_outside_the_directory(client, admin):
    """The slug arrives in a URL and becomes a filename. Re-slugged, not
    trusted — the same guard `remove_format` uses, for the same reason."""
    for hostile in ("..%2f..%2fconfig", "....//config", "%2e%2e%2fconfig"):
        got = client.get(f"/imports-exports/formats/statement/{hostile}.yaml")
        assert got.status_code in (404, 400), f"{hostile!r} was not refused"
        assert "database:" not in got.text, f"{hostile!r} read a file it should not"

"""Choosing which template reads a statement, and editing one.

**Two corrections from real use.**

*"I installed the template I created, then parsed the PDF and the values did not
map — perhaps it picked a different template."* That is exactly what happened,
and it was not a fluke. `pick()` orders templates by how much evidence they
demand, and at equal evidence the shipped ones were simply listed first — so a
template somebody installs, with no `match:` block, can never beat the generic
fallback that matches everything. Two fixes: an installed template outranks a
shipped one at equal specificity, and — decisively — **the upload form lets you
name the template**, because guessing should never be the only option.

*"I should be able to edit any existing template, even the preinstalled ones."*
So an editor that loads a template's current mapping, wherever it came from, and
saves or downloads the result. That is how a correction to a shipped format
reaches everyone rather than living in one person's config.
"""

from __future__ import annotations

import pytest

from app import docformats


@pytest.fixture
def store_dir(tmp_path, app_module, monkeypatch):
    """The visual mapper writes page images somewhere; keep it out of /data."""
    monkeypatch.setattr(app_module.settings.imports, "visual_dir", str(tmp_path / "visual"))
    return tmp_path

SHIPPED = docformats.BUILTIN_DIR / "statements"


def a_template(name: str, *, marker: str | None = None, source: str = "builtin"):
    body = f"name: {name}\n"
    if marker:
        body += f"match:\n  any_of:\n    - {marker}\n"
    body += "fields:\n  net_amount:\n    after:\n      - net amount\n    type: money\n"
    template = docformats.parse_statement_template(name, body)
    return docformats.replace(template, source=source)


# --------------------------------------------------------------------------- #
# Which template claims a document
# --------------------------------------------------------------------------- #

TEXT = "Some registry\nNet amount: 104.70\n"


def test_an_installed_template_beats_a_shipped_one_of_equal_evidence():
    """The reported bug. Both match everything, so before this the shipped one
    won on list order and somebody's own template never ran."""
    shipped = a_template("generic", source="builtin")
    installed = a_template("mine", source="installed")

    assert docformats.pick([shipped, installed], TEXT).key == "mine"
    # And the other way round in the list, so it is not order that decides.
    assert docformats.pick([installed, shipped], TEXT).key == "mine"


def test_stronger_evidence_still_wins_over_where_it_came_from():
    """Source is the tie-break, not the rule. A shipped template that names the
    document is a better answer than an installed one that matches anything."""
    shipped = a_template("specific", marker="Some registry", source="builtin")
    installed = a_template("mine", source="installed")

    assert docformats.pick([shipped, installed], TEXT).key == "specific"


def test_a_named_template_is_used_whatever_pick_would_have_said():
    """The decisive fix: no guessing at all when somebody has chosen."""
    shipped = a_template("specific", marker="Some registry", source="builtin")
    installed = a_template("mine", source="installed")

    chosen = docformats.pick([shipped, installed], TEXT, wanted="mine")

    assert chosen.key == "mine"


def test_naming_a_template_that_does_not_match_still_uses_it():
    """If somebody says "read it with this one", the answer is to read it with
    that one — refusing because the markers disagree is the app second-guessing
    a choice it was told to make."""
    other = a_template("other", marker="A DIFFERENT REGISTRY", source="builtin")

    assert docformats.pick([other], TEXT, wanted="other").key == "other"


def test_naming_a_template_that_is_not_installed_falls_back_to_guessing():
    shipped = a_template("generic", source="builtin")

    assert docformats.pick([shipped], TEXT, wanted="deleted-last-week").key == "generic"


def test_shipped_templates_are_marked_as_shipped():
    templates = docformats.load_statement_templates()

    assert templates
    assert all(t.source == "builtin" for t in templates)


def test_installed_templates_are_marked_as_installed(tmp_path):
    (tmp_path).mkdir(exist_ok=True)
    (tmp_path / "mine.yaml").write_text(
        "name: Mine\nfields:\n  net_amount:\n    after:\n      - net amount\n"
        "    type: money\n")

    templates = docformats.load_statement_templates(tmp_path)
    mine = next(t for t in templates if t.key == "mine")

    assert mine.source == "installed"


# --------------------------------------------------------------------------- #
# Turning a template back into a mapping the editor can show
# --------------------------------------------------------------------------- #

def test_a_template_round_trips_into_the_designer_mapping():
    """Editing means loading what is there. The mapping the editor holds is the
    same shape `template_yaml` consumes, so a load-edit-save cycle cannot lose
    a field."""
    original = (SHIPPED / "generic-au.yaml").read_text()
    template = docformats.parse_statement_template("generic-au", original)

    mapping = docformats.mapping_of(template)

    assert set(mapping) == set(template.fields)
    for name, spec in mapping.items():
        assert spec["after"] == template.fields[name].after
        assert spec["type"] == template.fields[name].type


def test_a_round_trip_through_yaml_keeps_every_field():
    original = (SHIPPED / "generic-au.yaml").read_text()
    template = docformats.parse_statement_template("generic-au", original)

    rebuilt = docformats.parse_statement_template("generic-au", docformats.template_yaml(
        name=template.name,
        fields=docformats.mapping_of(template),
        match=template.match_any,
    ))

    assert set(rebuilt.fields) == set(template.fields)
    assert rebuilt.match_any == template.match_any
    for name in template.fields:
        assert rebuilt.fields[name].after == template.fields[name].after
        assert rebuilt.fields[name].type == template.fields[name].type


@pytest.mark.parametrize("path", sorted(SHIPPED.glob("*.yaml")), ids=lambda p: p.stem)
def test_every_shipped_template_survives_a_round_trip(path):
    """A shipped template you open in the editor and save unchanged must come
    back unchanged. Anything the editor cannot represent would be silently
    dropped by a correction somebody meant to be small."""
    template = docformats.parse_statement_template(path.stem, path.read_text())

    rebuilt = docformats.parse_statement_template(path.stem, docformats.template_yaml(
        name=template.name,
        fields=docformats.mapping_of(template),
        match=template.match_any,
        rows=template.rows,
    ))

    assert set(rebuilt.fields) == set(template.fields)
    assert rebuilt.match_any == template.match_any
    # A row shape is the strongest signal a template has. Losing it on save
    # would turn a specific template into one that matches everything.
    assert (rebuilt.rows is None) == (template.rows is None)
    if template.rows:
        assert rebuilt.rows.shape == template.rows.shape
        assert rebuilt.rows.columns == template.rows.columns


# --------------------------------------------------------------------------- #
# Through the routes
# --------------------------------------------------------------------------- #

from test_routes import make_login, session_csrf  # noqa: E402

HTML = {"accept": "text/html"}

ETF_SHAPED = """Distribution Advice
Payment date: 16 July 2026
Net Amount: $123.45
Ordinary units allotted this distribution: 3
"""


def install(client, factory, tmp_path, app_module, monkeypatch, key, body):
    monkeypatch.setattr(app_module.settings.imports, "templates_dir", str(tmp_path))
    (tmp_path / "statements").mkdir(parents=True, exist_ok=True)
    (tmp_path / "statements" / f"{key}.yaml").write_text(body)


MINE = """name: My registry
fields:
  net_amount:
    after:
      - net amount
    type: money
"""


def test_the_upload_form_offers_every_template_by_name(client, session_factory,
                                                       tmp_path, monkeypatch, app_module):
    install(client, session_factory, tmp_path, app_module, monkeypatch, "mine", MINE)
    make_login(client, session_factory)

    page = client.get("/imports-exports", headers=HTML).text

    assert 'name="template"' in page
    assert "Whichever template fits" in page
    assert ">My registry<" in page
    # And the shipped ones, marked as such.
    assert "(shipped)" in page


def test_naming_a_template_on_upload_uses_it(client, session_factory, tmp_path,
                                             monkeypatch, app_module):
    """The reported bug, end to end: without this the generic fallback claimed
    the document and the installed template never ran."""
    from app import statements

    install(client, session_factory, tmp_path, app_module, monkeypatch, "mine", MINE)
    monkeypatch.setattr(statements, "_pdf_text", lambda data: ETF_SHAPED)
    make_login(client, session_factory)

    page = client.post(
        "/imports-exports/statement",
        data={"_csrf": session_csrf(session_factory), "template": "mine"},
        files={"file": ("a.pdf", b"%PDF", "application/pdf")}, headers=HTML)

    assert page.status_code == 200
    assert "My registry" in page.text


def test_not_naming_one_still_guesses(client, session_factory, tmp_path,
                                      monkeypatch, app_module):
    from app import statements

    install(client, session_factory, tmp_path, app_module, monkeypatch, "mine", MINE)
    monkeypatch.setattr(statements, "_pdf_text", lambda data: ETF_SHAPED)
    make_login(client, session_factory)

    page = client.post(
        "/imports-exports/statement",
        data={"_csrf": session_csrf(session_factory)},
        files={"file": ("a.pdf", b"%PDF", "application/pdf")}, headers=HTML)

    assert page.status_code == 200
    # An installed template with no markers now outranks the shipped fallback.
    assert "My registry" in page.text


# ---- the editor ----------------------------------------------------------- #

def _visual_token(client, session_factory, monkeypatch):
    from app import pagemap

    monkeypatch.setattr(pagemap, "_render", lambda data, resolution: [
        pagemap.RenderedPage(png=b"png", width=600, height=800, words=[
            {"text": "Net", "x0": 10, "top": 10, "x1": 40, "bottom": 24},
            {"text": "amount", "x0": 45, "top": 10, "x1": 100, "bottom": 24},
            {"text": "123.45", "x0": 105, "top": 10, "x1": 160, "bottom": 24},
        ])])
    page = client.post(
        "/imports-exports/statement/visual",
        data={"_csrf": session_csrf(session_factory)},
        files={"file": ("a.pdf", b"%PDF", "application/pdf")}, headers=HTML)
    import re
    return re.search(r"/imports-exports/statement/visual/([0-9a-f-]{36})/page/", page.text).group(1)


def test_the_editor_offers_shipped_templates_to_start_from(client, session_factory,
                                                           store_dir, monkeypatch):
    make_login(client, session_factory)
    token = _visual_token(client, session_factory, monkeypatch)

    page = client.post("/imports-exports/statement/visual/edit",
                       data={"_csrf": session_csrf(session_factory), "token": token},
                       headers=HTML)

    assert page.status_code == 200
    assert "map from scratch" in page.text
    assert "(shipped)" in page.text


def test_loading_a_template_shows_where_its_fields_point(client, session_factory,
                                                         store_dir, monkeypatch):
    """The point of editing: you see the current mapping, not a blank slate."""
    make_login(client, session_factory)
    token = _visual_token(client, session_factory, monkeypatch)

    page = client.post("/imports-exports/statement/visual/edit",
                       data={"_csrf": session_csrf(session_factory), "token": token,
                             "template": "generic-au"},
                       headers=HTML)

    assert 'id="startmapping"' in page.text
    assert "net_amount" in page.text
    assert "Editing" in page.text


def test_the_editor_prefills_the_name_and_markers(client, session_factory,
                                                  store_dir, monkeypatch):
    make_login(client, session_factory)
    token = _visual_token(client, session_factory, monkeypatch)

    page = client.post("/imports-exports/statement/visual/edit",
                       data={"_csrf": session_csrf(session_factory), "token": token,
                             "template": "generic-au"},
                       headers=HTML)

    assert "Australian registry statement (generic)" in page.text


def test_an_expired_session_sends_you_back_rather_than_erroring(
        client, session_factory, store_dir, monkeypatch):
    make_login(client, session_factory)

    response = client.post(
        "/imports-exports/statement/visual/edit",
        data={"_csrf": session_csrf(session_factory),
              "token": "00000000-0000-4000-8000-000000000000", "template": "generic-au"},
        headers=HTML, follow_redirects=False)

    assert response.status_code == 303
    assert "expired" in response.headers["location"]


def test_editing_needs_the_csrf_token(client, session_factory, store_dir, monkeypatch):
    make_login(client, session_factory)
    token = _visual_token(client, session_factory, monkeypatch)

    response = client.post("/imports-exports/statement/visual/edit",
                           data={"token": token, "template": "generic-au"}, headers=HTML)

    assert response.status_code == 403


def test_naming_a_shipped_template_overrides_what_guessing_would_pick(
        client, session_factory, tmp_path, monkeypatch, app_module):
    """**Found by mutation.** The two tests above both expect the installed
    template, so dropping the `wanted` argument entirely passed them both —
    guessing happens to give the same answer. This names the SHIPPED one, which
    guessing would never choose now that installed outranks it.
    """
    from app import statements

    install(client, session_factory, tmp_path, app_module, monkeypatch, "mine", MINE)
    monkeypatch.setattr(statements, "_pdf_text", lambda data: ETF_SHAPED)
    make_login(client, session_factory)

    page = client.post(
        "/imports-exports/statement",
        data={"_csrf": session_csrf(session_factory), "template": "generic-au"},
        files={"file": ("a.pdf", b"%PDF", "application/pdf")}, headers=HTML)

    assert "Australian registry statement (generic)" in page.text
    assert "My registry" not in page.text


# --------------------------------------------------------------------------- #
# Unmapping a field
# --------------------------------------------------------------------------- #

def test_every_field_offers_a_way_to_clear_it(client, session_factory, store_dir,
                                              monkeypatch):
    """**Reported: there was no way to undo a mapping.**

    Editing an existing template is mostly *re*-mapping, so a field that can be
    set and never unset makes the editor a one-way door — the only way back was
    to reload the page and lose everything else.
    """
    from app import docformats

    make_login(client, session_factory)
    token = _visual_token(client, session_factory, monkeypatch)

    page = client.post("/imports-exports/statement/visual/edit",
                       data={"_csrf": session_csrf(session_factory), "token": token,
                             "template": "generic-au"},
                       headers=HTML).text

    assert page.count('class="clearfield"') == len(docformats.DESIGNABLE_FIELDS)


def test_the_clear_control_says_what_it_clears(client, session_factory, store_dir,
                                               monkeypatch):
    """A bare × beside six identical rows is a guess. The accessible name has
    to name the field, because that is all a screen reader gets."""
    make_login(client, session_factory)
    token = _visual_token(client, session_factory, monkeypatch)

    page = client.post("/imports-exports/statement/visual/edit",
                       data={"_csrf": session_csrf(session_factory), "token": token},
                       headers=HTML).text

    assert 'aria-label="Clear Payment date"' in page


def test_the_text_designer_offers_it_too(client, session_factory, monkeypatch):
    """Both designers share the field list, so both get the control — one that
    only worked in one of them is the kind of difference nobody expects."""
    from app import docformats

    make_login(client, session_factory)

    page = client.post("/imports-exports/statement/design",
                       data={"_csrf": session_csrf(session_factory),
                             "text": "Net amount: 104.70\n"},
                       headers=HTML).text

    assert page.count('class="clearfield"') == len(docformats.DESIGNABLE_FIELDS)


def test_the_template_picker_comes_before_the_parse_button(client, session_factory,
                                                           tmp_path, monkeypatch,
                                                           app_module):
    """**Reported: it read as belonging to the mapper below it.** Order on the
    page is what says which control belongs to which action; a note beside it
    is not, because people click what looks obvious."""
    import re

    make_login(client, session_factory)
    page = client.get("/imports-exports", headers=HTML).text
    form = re.search(r'<form action="/imports-exports/statement"[^>]*>.*?</form>', page, re.S).group(0)

    assert form.index('name="template"') < form.index(">Parse<")

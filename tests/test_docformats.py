"""Document formats as data.

The point of this module is that adding a broker or a registry needs no Python,
because nobody can test a format they have no document for. So the tests are
mostly about the format being *safe* and *expressive enough*:

  * safe — a template arrives by pull request, so nothing in one may reach
    `re.compile`. A contributor cannot ship catastrophic backtracking, and a
    reviewer does not have to read regex to review a template.
  * expressive enough — every pattern a hand-written parser needs in
    `statements.py` is now a template, and `test_statements.py` still passes
    untouched. That suite is the real proof; these tests cover the machinery.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from app import docformats as fmt

SAMPLE = """
COMPUTERSHARE INVESTOR SERVICES PTY LIMITED
Holding statement — distribution advice

Payment date: 15 March 2026
Net amount paid                                 $1,042.75
Franked amount                                    $980.00
Franking credits                                  $420.00
Units allotted                                         12
Allotment price                                    $86.89
"""


def template(**overrides) -> fmt.StatementTemplate:
    base = dict(
        key="t", name="Test",
        fields={"net_amount": fmt.FieldSpec(after=["net amount"], type="money")},
    )
    base.update(overrides)
    return fmt.StatementTemplate(**base)


# --------------------------------------------------------------------------- #
# Safety: no template ever supplies a pattern
# --------------------------------------------------------------------------- #

def test_a_label_is_escaped_not_interpreted():
    """The whole safety property. If labels were interpolated raw, a template
    could ship `.*` — or something far worse — and a reviewer would have to
    spot it."""
    spec = fmt.FieldSpec(after=["net (amount)"], type="money")

    (pattern,) = spec.compiled()

    assert pattern.search("net (amount) $10.00").group(1) == "10.00"
    # The parentheses are literal, so this is NOT a match on a captured group.
    assert pattern.search("net amount $10.00") is None


def test_a_label_that_looks_like_a_bad_regex_is_harmless():
    """`(a+)+$` against a long non-matching string is the classic catastrophic
    backtrack. Escaped, it is just text that does not appear."""
    spec = fmt.FieldSpec(after=["(a+)+$"], type="money")

    (pattern,) = spec.compiled()

    assert pattern.search("a" * 400 + " $1.00") is None


def test_an_unknown_field_type_is_refused_at_load_time():
    """A broken template must fail when installed, not halfway through
    somebody's import."""
    with pytest.raises(fmt.TemplateError, match="unknown field type"):
        fmt.FieldSpec(after=["x"], type="regex")


def test_a_field_with_no_labels_is_refused():
    with pytest.raises(fmt.TemplateError, match="at least one"):
        fmt.FieldSpec(after=[], type="money")


def test_an_unknown_row_token_is_refused():
    with pytest.raises(fmt.TemplateError, match="unknown row shape token"):
        fmt.RowSpec(shape="TICKER WIDGET", columns=["a", "b"])


def test_a_row_shape_must_line_up_with_its_column_names():
    """Off-by-one here would silently shift every value one column left."""
    with pytest.raises(fmt.TemplateError, match="line up"):
        fmt.RowSpec(shape="TICKER NUM NUM", columns=["ticker", "price"])


# --------------------------------------------------------------------------- #
# Reading values
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("label,expected", [
    ("net amount", Decimal("1042.75")),
    ("franked amount", Decimal("980.00")),
    ("franking credits", Decimal("420.00")),
])
def test_money_is_read_after_its_label(label, expected):
    spec = fmt.FieldSpec(after=[label], type="money")

    assert fmt._first_value(spec, SAMPLE) == expected


def test_the_first_matching_label_wins():
    """Label order is the template author saying which wording is more
    reliable on this layout."""
    spec = fmt.FieldSpec(after=["franking credits", "net amount"], type="money")

    assert fmt._first_value(spec, SAMPLE) == Decimal("420.00")


def test_a_label_that_is_absent_yields_none():
    """Not an exception: a statement that does not quite match is the normal
    case, and every value is shown for correction before anything is written."""
    spec = fmt.FieldSpec(after=["dividend reinvestment surcharge"], type="money")

    assert fmt._first_value(spec, SAMPLE) is None


def test_money_requires_cents_so_a_label_s_own_digits_are_not_read():
    """"Franking credits 2024 ... $420.00" must not read 2024 as an amount."""
    spec = fmt.FieldSpec(after=["credits"], type="money")

    assert fmt._first_value(spec, "Franking credits 2024 total $420.00") == Decimal("420.00")


def test_a_date_is_parsed_not_just_captured():
    spec = fmt.FieldSpec(after=["payment date"], type="date")

    assert fmt._first_value(spec, SAMPLE) == dt.date(2026, 3, 15)


@pytest.mark.parametrize("raw,expected", [
    ("15 March 2026", dt.date(2026, 3, 15)),
    ("15 Mar 2026", dt.date(2026, 3, 15)),
    ("15/03/2026", dt.date(2026, 3, 15)),
    ("15-03-2026", dt.date(2026, 3, 15)),
    ("not a date", None),
])
def test_the_date_formats_registries_actually_use(raw, expected):
    assert fmt._parse_date(raw) == expected


def test_integers_come_back_as_ints_and_money_as_decimal():
    """A unit count is a whole number of securities; an amount is money. Using
    a float for either is how cents go missing."""
    assert fmt.coerce("12", "integer") == 12
    assert isinstance(fmt.coerce("12", "integer"), int)
    assert fmt.coerce("1,042.75", "money") == Decimal("1042.75")
    assert isinstance(fmt.coerce("1,042.75", "money"), Decimal)


def test_thousands_separators_and_currency_symbols_are_stripped():
    assert fmt.coerce("$1,234,567.89", "money") == Decimal("1234567.89")


def test_junk_coerces_to_none_rather_than_raising():
    assert fmt.coerce("n/a", "money") is None
    assert fmt.coerce(None, "money") is None


def test_units_allotted_is_not_confused_with_units_held():
    """The statement says both, and the holding is the larger, more prominent
    number. Anchoring on the verb is what picks the right one."""
    text = "Units held 4,213\nUnits allotted 12\n"
    spec = fmt.FieldSpec(after=["units allotted"], type="integer")

    assert fmt._first_value(spec, text) == 12


# --------------------------------------------------------------------------- #
# Rows
# --------------------------------------------------------------------------- #

VANGUARD = """
Distribution reinvestment plan statement
ALPHA 108.7983 212 0.48829897 0.00 103.52 28.54 1 23.26
BETAX 112.4500 180 0.31000000 0.00 55.80 10.00 0 65.80
"""


def test_a_row_shape_reads_a_table_without_a_regex():
    spec = fmt.RowSpec(
        shape="TICKER NUM INT NUM NUM NUM NUM INT NUM",
        columns=["ticker", "price", "held", "per_sec", "tax", "amount",
                 "brought", "allotted", "carried"],
    )

    rows = fmt._extract_rows(spec, VANGUARD)

    assert [r["ticker"] for r in rows] == ["ALPHA", "BETAX"]
    assert rows[0]["amount"] == "103.52"
    assert rows[1]["allotted"] == "0"   # cash carried forward, no units issued


def test_a_line_that_does_not_fit_the_shape_is_skipped():
    """Headers, footers and totals share the page with the rows."""
    spec = fmt.RowSpec(shape="TICKER NUM", columns=["ticker", "price"])

    rows = fmt._extract_rows(spec, "Fund Price\nALPHA 108.79\nTotal 108.79\n")

    assert [r["ticker"] for r in rows] == ["ALPHA"]


# --------------------------------------------------------------------------- #
# Choosing a template
# --------------------------------------------------------------------------- #

def test_a_template_claims_a_document_by_its_marker():
    t = template(match_any=["Computershare"])

    assert t.matches(SAMPLE)
    assert not t.matches("Some other registry")


def test_matching_ignores_case():
    assert template(match_any=["COMPUTERSHARE"]).matches(SAMPLE)


def test_a_template_with_no_marker_matches_anything():
    """That is how a generic fallback works."""
    assert template().matches("literally anything")


def test_a_specific_template_is_preferred_over_the_catch_all():
    """File order must not decide correctness: a generic template matches
    everything, so it has to be tried last."""
    generic = template(key="generic")
    specific = template(key="vanguard", match_any=["Computershare"])

    assert fmt.pick([generic, specific], SAMPLE).key == "vanguard"


def test_the_catch_all_is_used_when_nothing_specific_claims_it():
    generic = template(key="generic")
    specific = template(key="vanguard", match_any=["Vanguard"])

    assert fmt.pick([generic, specific], SAMPLE).key == "generic"


def test_no_templates_means_no_match_rather_than_a_crash():
    assert fmt.pick([], SAMPLE) is None


# --------------------------------------------------------------------------- #
# Loading the shipped templates
# --------------------------------------------------------------------------- #

def test_every_shipped_template_loads():
    """A malformed shipped template is a broken release, so this is the test
    that stops one being merged."""
    templates = fmt.load_statement_templates()

    assert {t.key for t in templates} >= {"generic-au", "vanguard-drp-advice"}


def test_the_shipped_generic_template_reads_a_whole_statement():
    """End to end through the real files — the ported patterns, doing the job
    the hardcoded ones did."""
    templates = fmt.load_statement_templates()
    chosen = fmt.pick(templates, SAMPLE)

    values = chosen.extract(SAMPLE)

    assert values["payment_date"] == dt.date(2026, 3, 15)
    assert values["net_amount"] == Decimal("1042.75")
    assert values["franked_amount"] == Decimal("980.00")
    assert values["franking_credits"] == Decimal("420.00")
    assert values["drp_units"] == 12
    assert values["drp_price"] == Decimal("86.89")


def test_the_shipped_vanguard_template_claims_and_reads_its_advice():
    templates = fmt.load_statement_templates()
    chosen = fmt.pick(templates, VANGUARD)

    assert chosen.key == "vanguard-drp-advice"
    rows = chosen.extract(VANGUARD)["rows"]
    assert [r["ticker"] for r in rows] == ["ALPHA", "BETAX"]


def test_an_operator_template_overrides_a_shipped_one(tmp_path):
    """Local files win, the same direction the settings layer overrides."""
    (tmp_path / "generic-au.yaml").write_text(
        "name: Mine\nfields:\n  net_amount:\n    after: ['grand total']\n    type: money\n"
    )

    templates = {t.key: t for t in fmt.load_statement_templates(extra_dir=tmp_path)}

    assert templates["generic-au"].name == "Mine"


def test_one_broken_template_does_not_stop_the_others(tmp_path, caplog):
    """A drop-in directory will eventually contain something half-edited. That
    must not take the app down at startup."""
    (tmp_path / "broken.yaml").write_text("fields:\n  x:\n    type: nonsense\n")
    (tmp_path / "fine.yaml").write_text(
        "name: Fine\nfields:\n  net_amount:\n    after: ['total']\n    type: money\n"
    )

    keys = {t.key for t in fmt.load_statement_templates(extra_dir=tmp_path)}

    assert "fine" in keys
    assert "broken" not in keys


# --------------------------------------------------------------------------- #
# Telling someone why a field came back empty
# --------------------------------------------------------------------------- #

def test_a_parsed_statement_reports_which_template_read_it(pf, monkeypatch):
    """A blank field is otherwise indistinguishable from a layout nobody has
    written a template for. Naming the template is what turns "it didn't work"
    into a one-line fix somebody can make."""
    from app import statements

    monkeypatch.setattr(statements, "_pdf_text", lambda _data: SAMPLE)

    parsed = statements.parse_statement(b"", pf)

    assert parsed.template_key == "generic-au"
    assert parsed.template_name
    # The whole text, not just the first lines — the label you need to add is
    # rarely in the top 25 lines of a statement.
    assert parsed.full_text == SAMPLE


def test_the_preview_shows_the_layout_and_the_text(client, session_factory, monkeypatch):
    from app import statements
    from test_routes import make_login, session_csrf

    make_login(client, session_factory)
    monkeypatch.setattr(statements, "_pdf_text", lambda _data: SAMPLE)

    resp = client.post(
        "/imports-exports/statement",
        files={"file": ("advice.pdf", b"%PDF-fake", "application/pdf")},
        data={"_csrf": session_csrf(session_factory)},
        headers={"accept": "text/html"},
    )

    assert resp.status_code == 200
    assert "generic-au.yaml" in resp.text
    assert "Net amount paid" in resp.text        # the full text is shown
    assert "after:" in resp.text                 # and how to fix a blank field


# Authoring: inferring a template from a value somebody pointed at. The
# creator UI's brain, here rather than in JavaScript so it can be tested —
# a wrong label reads the WRONG number on somebody else's statement:
# decisions.md #37.
# --------------------------------------------------------------------------- #

def _index_of(text: str, word: str) -> int:
    return next(t.index for t in fmt.tokenize(text) if t.text == word)


def test_tokenizing_numbers_every_word_and_remembers_its_line():
    tokens = fmt.tokenize("Payment date: 15 March\nNet amount $10.00")

    assert [t.text for t in tokens[:4]] == ["Payment", "date:", "15", "March"]
    assert tokens[0].line == 0
    assert tokens[4].line == 1          # "Net" is on the second line


@pytest.mark.parametrize("word,expected", [
    ("$1,042.75", "money"),
    ("1042.75", "money"),
    ("12", "integer"),
    ("0.48829897", "number"),
    ("15/03/2026", "date"),
    ("ALPHA", "text"),
])
def test_a_value_s_type_is_read_from_its_shape(word, expected):
    assert fmt.infer_type(fmt.tokenize(word), 0)[0] == expected


def test_a_written_date_is_recognised_across_three_words():
    """"15 March 2026" is three tokens. A template capturing only "15" would be
    worse than useless — it would produce a plausible wrong date."""
    text = "Payment date: 15 March 2026"

    assert fmt.infer_type(fmt.tokenize(text), _index_of(text, "15")) == ("date", 3)


def test_a_bare_number_that_is_not_a_date_keeps_its_own_type():
    text = "Units allotted 12"

    assert fmt.infer_type(fmt.tokenize(text), _index_of(text, "12")) == ("integer", 1)


def test_labels_are_offered_longest_first():
    """A longer label is more specific and less likely to collide with another
    line. The author picks; the app orders by how safe each is."""
    text = "Net amount paid $1,042.75"

    labels = fmt.infer_labels(fmt.tokenize(text), _index_of(text, "$1,042.75"))

    assert labels == ["net amount paid", "amount paid", "paid"]


def test_a_label_stops_at_a_number():
    """"Units held 4,213 Units allotted 12" must not yield a label containing
    the holding — that is a template nobody else's statement matches."""
    text = "Units held 4,213 Units allotted 12"

    labels = fmt.infer_labels(fmt.tokenize(text), _index_of(text, "12"))

    assert labels == ["units allotted", "allotted"]


def test_a_label_does_not_reach_onto_the_line_above():
    """A word on the previous line is usually a column heading, describing a
    whole column rather than the value that was pointed at."""
    text = "Franking credits\n$420.00"

    assert fmt.infer_labels(fmt.tokenize(text), _index_of(text, "$420.00")) == []


def test_trailing_punctuation_is_dropped_from_a_label():
    text = "Payment date: 15/03/2026"

    labels = fmt.infer_labels(fmt.tokenize(text), _index_of(text, "15/03/2026"))

    assert labels[0] == "payment date"


def test_inference_proves_itself_against_the_document():
    """An inferred label is a guess about wording. The only way to know it is
    right is to run it back against the document it came from."""
    result = fmt.infer_field(SAMPLE, _index_of(SAMPLE, "$1,042.75"))

    assert result.type == "money"
    assert result.value == Decimal("1042.75")
    assert "net amount paid" in result.labels
    assert result.problem is None


def test_a_candidate_that_reads_the_wrong_value_is_dropped():
    """"paid" appears before an earlier amount too, so the short candidate
    finds that one instead — and must not be offered."""
    text = "Amount paid $50.00\nNet amount paid $1,042.75"

    result = fmt.infer_field(text, _index_of(text, "$1,042.75"))

    assert "net amount paid" in result.labels
    assert "paid" not in result.labels      # would read $50.00


def test_a_written_date_infers_and_reads_back():
    result = fmt.infer_field(SAMPLE, _index_of(SAMPLE, "15"))

    assert result.type == "date"
    assert result.value == dt.date(2026, 3, 15)


def test_a_value_with_no_label_asks_for_one_rather_than_refusing():
    """A value with no label maps, and asks for one.

    **Must not become a refusal.** A value in a table column has no label
    beside it, which is precisely the case the designer exists for — refusing
    it, or telling somebody to "type one" with nowhere to type it, rejects the
    documents the feature is for. It maps, reads back what was pointed at, and
    asks for a label.
    """
    result = fmt.infer_field("Franking credits\n$420.00", 2)

    assert result.problem is None
    assert result.needs_label is True
    assert str(result.value) == "420.00"
    assert "type the words" in result.warning


def test_pointing_at_nothing_is_handled():
    assert fmt.infer_field(SAMPLE, 9999).problem is not None


def test_a_value_that_cannot_be_pinned_down_explains_why():
    """A label that appears twice finds the FIRST one, so no candidate reads
    back what was pointed at. Worth saying — and, since 2026-08-04, not worth
    blocking: the way out is to edit the label, which the interface now
    allows."""
    text = "Amount 10.00\nAmount 20.00"

    result = fmt.infer_field(text, _index_of(text, "20.00"))

    assert result.problem is None
    assert result.needs_label is True
    assert "do not find it again" in result.warning


def test_a_ticker_shaped_label_is_flagged_even_though_it_works():
    """Pointing at a table cell infers the row's ticker as the label. That
    reads correctly on the document in front of you and on nobody else's — the
    likeliest way to author a template that looks right and cannot be shared.
    It succeeds, and says so."""
    text = "Fund Amount\nALPHA 10.00\nBETAX 20.00"

    result = fmt.infer_field(text, _index_of(text, "20.00"))

    assert result.value == Decimal("20.00")     # it did work
    assert result.problem is None
    assert "looks like a ticker" in result.warning
    assert "row shape" in result.warning


def test_an_ordinary_label_is_not_flagged():
    result = fmt.infer_field(SAMPLE, _index_of(SAMPLE, "$1,042.75"))

    assert result.warning is None


# --------------------------------------------------------------------------- #
# Exporting what was designed
# --------------------------------------------------------------------------- #

def test_the_exported_template_is_loadable_and_works(tmp_path):
    """The round trip is the point: what the designer produces must be a file
    the loader accepts, that reads the document it was designed on."""
    path = tmp_path / "example.yaml"
    path.write_text(fmt.template_yaml(
        name="Example Registry",
        fields={"net_amount": {"after": ["net amount paid"], "type": "money"}},
        match=["COMPUTERSHARE"],
    ))

    loaded = fmt.load_statement_template(path)

    assert loaded.matches(SAMPLE)
    assert loaded.extract(SAMPLE)["net_amount"] == Decimal("1042.75")


def test_the_exported_template_explains_itself():
    """A file that arrives with no context gets merged without review."""
    yaml_text = fmt.template_yaml("X", {"a": {"after": ["b"], "type": "money"}}, [])

    assert yaml_text.startswith("#")
    assert "recipe-statement" in yaml_text


def test_the_exported_template_contains_no_values_from_the_document():
    """The redaction property at the format level: a template describes where
    to look, never what was found. A template carrying an amount into a public
    pull request would be the worst failure this feature could have."""
    yaml_text = fmt.template_yaml(
        name="Example Registry",
        fields={"net_amount": {"after": ["net amount paid"], "type": "money"}},
        match=["COMPUTERSHARE"],
    )

    for secret in ("1,042.75", "1042.75", "420.00", "86.89", "4,213", "15 March 2026"):
        assert secret not in yaml_text


# --------------------------------------------------------------------------- #
# The designer, end to end
# --------------------------------------------------------------------------- #

def _designer(client, session_factory, text=SAMPLE):
    from test_routes import session_csrf

    return client.post("/imports-exports/statement/design",
                       data={"text": text, "_csrf": session_csrf(session_factory)},
                       headers={"accept": "text/html"})


def test_the_designer_renders_every_word_as_a_control(client, session_factory):
    """Keyboard operability is not optional here: "click the value" is the
    whole interaction, so the words must be real buttons, not spans."""
    from test_routes import make_login

    make_login(client, session_factory)

    page = _designer(client, session_factory).text

    assert page.count('class="tok"') == len(fmt.tokenize(SAMPLE))
    assert '<button type="button" class="tok"' in page
    assert 'aria-pressed="false"' in page


def test_the_designer_offers_the_fields_the_preview_form_uses(client, session_factory):
    """A template built here has to populate that form, so the names must be
    the same ones."""
    from test_routes import make_login

    make_login(client, session_factory)

    page = _designer(client, session_factory).text

    for key, label, _type in fmt.DESIGNABLE_FIELDS:
        assert f'data-field="{key}"' in page
        assert label in page


def test_inferring_over_http_returns_what_the_ui_needs(client, session_factory):
    from test_routes import make_login, session_csrf

    make_login(client, session_factory)
    index = _index_of(SAMPLE, "$1,042.75")

    resp = client.post("/imports-exports/statement/design/infer",
                       data={"text": SAMPLE, "index": index,
                             "_csrf": session_csrf(session_factory)})

    body = resp.json()
    assert body["type"] == "money"
    assert body["value"] == "1042.75"
    assert "net amount paid" in body["labels"]
    assert body["problem"] is None


def test_inferring_reports_a_problem_rather_than_failing(client, session_factory):
    from test_routes import make_login, session_csrf

    make_login(client, session_factory)

    resp = client.post("/imports-exports/statement/design/infer",
                       data={"text": SAMPLE, "index": 99999,
                             "_csrf": session_csrf(session_factory)})

    assert resp.status_code == 200
    assert resp.json()["problem"]


def test_the_designer_exports_a_working_template(client, session_factory, tmp_path):
    """The round trip that matters: what somebody downloads must be a file the
    app can load and that reads their statement."""
    import json

    from test_routes import make_login, session_csrf

    make_login(client, session_factory)
    mapping = {"net_amount": {"after": ["net amount paid"], "type": "money"}}

    resp = client.post("/imports-exports/statement/design/export",
                       data={"name": "Example Registry", "mapping": json.dumps(mapping),
                             "marker": "COMPUTERSHARE", "_csrf": session_csrf(session_factory)})

    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    assert "example-registry.yaml" in resp.headers["content-disposition"]

    path = tmp_path / "example-registry.yaml"
    path.write_text(resp.text)
    loaded = fmt.load_statement_template(path)
    assert loaded.extract(SAMPLE)["net_amount"] == Decimal("1042.75")


def test_a_downloaded_template_carries_none_of_the_statement_s_values(
    client, session_factory
):
    """The privacy property, at the point it would actually leak: what leaves
    this screen is a description of where to look."""
    import json

    from test_routes import make_login, session_csrf

    make_login(client, session_factory)
    mapping = {"net_amount": {"after": ["net amount paid"], "type": "money"},
               "drp_units": {"after": ["units allotted"], "type": "integer"}}

    resp = client.post("/imports-exports/statement/design/export",
                       data={"name": "Example", "mapping": json.dumps(mapping),
                             "marker": "", "_csrf": session_csrf(session_factory)})

    for secret in ("1,042.75", "1042.75", "420.00", "86.89", "4,213", "X0000000000"):
        assert secret not in resp.text


def test_a_viewer_cannot_reach_the_designer(client, session_factory):
    """It is an authoring tool for someone who can import; a read-only member
    has no business posting statement text into it."""
    from sqlalchemy import select as sa_select

    from app.models import PortfolioMember
    from test_routes import make_login, reading

    make_login(client, session_factory)
    with reading(session_factory) as s:
        s.scalars(sa_select(PortfolioMember)).one().role = "viewer"
        s.commit()

    assert _designer(client, session_factory).status_code == 403


def test_the_shipped_config_does_not_override_the_shipped_broker_formats():
    """A config override wins over the file, permanently.

    `brokers_available()` layers shipped files, then installed ones, then
    config — config last, deliberately, because a value somebody wrote down is
    the most considered of the three. That makes shipping *copies* of the
    built-in formats in the default config.yaml a trap: every install would
    start with a frozen duplicate shadowing the file, and a later fix to the
    file — a broker changing its headings — would never reach anyone.
    """
    import yaml

    root = Path(__file__).resolve().parent.parent
    for name in ("config.yaml", "deploy/compose/config.yaml"):
        data = yaml.safe_load((root / name).read_text()) or {}
        overrides = (data.get("imports") or {}).get("brokers")
        assert not overrides, (
            f"{name} pins broker formats that ship as files: {sorted(overrides)}"
        )

"""Turning a designed template into a contribution somebody can merge.

`test_format_samples.py` requires three files — the template, a redacted sample
of the extracted text, and what that sample should produce (decisions.md #64).
The designer emits one; this makes all three.

**The redaction must not pretend** (decisions.md #69): mask what can be
recognised mechanically, say exactly what was and was not masked, and put the
result in front of the person before anything leaves the machine.

**Expected values are computed from the REDACTED text**, never the original, so
a field redaction ate shows up before the pull request rather than after.
"""

from __future__ import annotations

import io
import re
import zipfile

import pytest

from app import contribute

TEMPLATE = """name: Acme Registry
match:
  any_of:
    - ACME REGISTRY
fields:
  payment_date:
    after:
      - Payment date
    type: date
  net_amount:
    after:
      - Net amount
    type: money
"""

STATEMENT = """ACME REGISTRY PTY LIMITED
GPO BOX 1234 SYDNEY NSW 2000

Distribution Advice
Holder number: X0012345678
Tax File Number: 123 456 789
MR JOHN SMITH
14 Example Street
SUBURBIA NSW 2075
john.smith@example.com
Phone 02 9999 8888

Payment date 15 July 2026
Net amount 1042.75
"""


# --------------------------------------------------------------------------- #
# Redaction — mechanical, honest about its limits
# --------------------------------------------------------------------------- #

def test_a_long_digit_run_is_masked():
    """Holder numbers, account numbers, HINs, SRNs. The shapes differ by
    registry; being a long run of digits does not."""
    out, _ = contribute.redact("Holder number: X0012345678")

    assert "0012345678" not in out.text


def test_a_tax_file_number_is_masked():
    out, _ = contribute.redact("Tax File Number: 123 456 789")

    assert "123 456 789" not in out.text


def test_an_email_address_is_masked():
    out, _ = contribute.redact("john.smith@example.com")

    assert "john.smith" not in out.text
    assert "@" in out.text  # the shape survives, so the layout still reads


def test_a_phone_number_is_masked():
    out, _ = contribute.redact("Phone 02 9999 8888")

    assert "9999 8888" not in out.text


def test_the_labels_survive_redaction():
    """The whole point of the sample is the LAYOUT. Masking "Holder number:"
    along with its value would leave nothing for the template to match on."""
    out, _ = contribute.redact(STATEMENT)

    for label in ("Payment date", "Net amount", "Holder number", "ACME REGISTRY"):
        assert label in out.text


def test_the_amounts_are_replaced_not_removed():
    """A sample with no figures cannot exercise a money field, and the expected
    file would have nothing to assert."""
    out, _ = contribute.redact(STATEMENT)

    assert "Net amount" in out.text
    import re
    assert re.search(r"Net amount\s+[\d.,]+", out.text)


def test_money_is_changed_so_the_real_figures_are_not_published():
    """These are somebody's dividends. The layout is the contribution; the
    numbers are not."""
    out, _ = contribute.redact("Net amount 1042.75")

    assert "1042.75" not in out.text


def test_a_replaced_amount_keeps_its_shape():
    """Two decimal places in, two decimal places out — otherwise the money
    field's type inference is being tested against something the real document
    never looks like."""
    import re
    out, _ = contribute.redact("Net amount 1042.75")

    assert re.search(r"Net amount \d+\.\d{2}$", out.text.strip())


def test_it_reports_what_it_masked():
    _, notes = contribute.redact(STATEMENT)

    kinds = {n.kind for n in notes}
    assert {"digits", "email", "money"} <= kinds


def test_it_lists_names_as_something_to_check():
    """Words have no mechanical shape, so a name is not something this can
    find. Said once and plainly, as an item on a list, not
    a paragraph of warning, which is not how you get something read."""
    _, notes = contribute.redact(STATEMENT)

    assert any(n.kind == "review" and "name" in n.detail.lower() for n in notes)


def test_running_it_twice_shifts_the_amounts_again():
    """**Deliberately not idempotent**, and the trade is worth recording.

    Zeroed amounts were idempotent and useless: every expected value became
    `0.00`, so the file that exists to prove the template read the right fields
    could not tell `net_amount` from `franking_credits`. Shifted amounts keep
    that check working; the cost is that a second pass shifts them again.

    That cost is nil in practice — `redact()` is called once, on the way into
    the bundle, and there is no re-run button. If one is ever added, it must
    re-run from the ORIGINAL text rather than from its own output.
    """
    once, _ = contribute.redact(STATEMENT)
    twice, _ = contribute.redact(once.text)

    assert twice.text != once.text
    # Everything that is masked rather than shifted IS stable.
    assert "0000000000" in once.text and "0000000000" in twice.text


def test_shifted_amounts_stay_distinguishable():
    """The property zeroing destroyed: two different figures must not collapse
    into the same one, or the expected file stops being able to tell one field
    from another."""
    out, _ = contribute.redact("Net 1042.75\nFranking 448.19\nGross 1490.94\n")

    figures = re.findall(r"\d[\d,]*\.\d{2}", out.text)
    assert len(figures) == 3
    assert len(set(figures)) == 3


def test_the_same_amount_twice_stays_the_same_amount():
    """A statement whose internal arithmetic no longer adds up is a confusing
    thing to hand a reviewer."""
    out, _ = contribute.redact("Paid 104.70\nTotal 104.70\n")

    figures = re.findall(r"\d[\d,]*\.\d{2}", out.text)
    assert figures[0] == figures[1]


def test_a_shifted_amount_is_not_the_real_one():
    out, _ = contribute.redact("Net amount 1042.75")

    assert "1042.75" not in out.text


# --------------------------------------------------------------------------- #
# Expected values — computed from the redacted text, never the original
# --------------------------------------------------------------------------- #

def test_expected_values_come_from_the_sample_that_will_be_published():
    """If they came from the original, a redaction that ate a field would
    produce an expected file that CI could never satisfy — discovered by a
    reviewer instead of by the contributor."""
    redacted, _ = contribute.redact(STATEMENT)
    body = contribute.expected_yaml(TEMPLATE, redacted.text)

    from ruamel.yaml import YAML
    values = YAML(typ="safe").load(body)

    assert values["payment_date"] == __import__("datetime").date(2026, 7, 15)
    # The amount is the REDACTED one, not 1042.75.
    assert values["net_amount"] != "1042.75"


def test_a_field_the_template_no_longer_finds_is_flagged():
    """The signal that redaction went too far. Silence here is the failure
    mode this exists to prevent."""
    body = contribute.expected_yaml(TEMPLATE, "ACME REGISTRY\nnothing else at all\n")

    assert "could not be read" in body.lower()


def test_the_expected_file_explains_itself():
    redacted, _ = contribute.redact(STATEMENT)
    body = contribute.expected_yaml(TEMPLATE, redacted.text)

    assert body.lstrip().startswith("#")


# --------------------------------------------------------------------------- #
# The bundle
# --------------------------------------------------------------------------- #

def _bundle():
    redacted, _ = contribute.redact(STATEMENT)
    return contribute.bundle("acme-registry", TEMPLATE, redacted.text)


def test_the_bundle_holds_exactly_the_three_files_ci_requires():
    with zipfile.ZipFile(io.BytesIO(_bundle())) as z:
        names = sorted(z.namelist())

    assert names == [
        "formats/samples/acme-registry.expected.yaml",
        "formats/samples/acme-registry.txt",
        "formats/statements/acme-registry.yaml",
    ]


def test_the_paths_match_the_repository_so_it_is_a_copy_in():
    """`docs/contributing/recipe-statement.md` documents this layout. Matching
    it exactly is the difference between a contribution and a chore."""
    from app import docformats

    with zipfile.ZipFile(io.BytesIO(_bundle())) as z:
        names = z.namelist()

    assert (docformats.BUILTIN_DIR / "statements").is_dir()
    assert (docformats.BUILTIN_DIR / "samples").is_dir()
    assert any(n.startswith("formats/statements/") for n in names)
    assert any(n.startswith("formats/samples/") for n in names)


def test_the_bundled_template_is_the_one_that_was_designed():
    with zipfile.ZipFile(io.BytesIO(_bundle())) as z:
        assert z.read("formats/statements/acme-registry.yaml").decode() == TEMPLATE


def test_the_bundled_sample_carries_no_real_figures():
    with zipfile.ZipFile(io.BytesIO(_bundle())) as z:
        sample = z.read("formats/samples/acme-registry.txt").decode()

    assert "1042.75" not in sample
    assert "0012345678" not in sample


def test_the_bundled_sample_carries_the_redaction_notice():
    """It will sit in a public repository. The header is what tells the next
    reader the values are invented and must stay that way."""
    with zipfile.ZipFile(io.BytesIO(_bundle())) as z:
        sample = z.read("formats/samples/acme-registry.txt").decode()

    assert "REDACTED" in sample


def test_the_bundle_passes_the_harness_it_is_built_for():
    """The real check: the three generated files satisfy the same assertions
    `test_format_samples.py` makes about the shipped ones."""
    from ruamel.yaml import YAML

    with zipfile.ZipFile(io.BytesIO(_bundle())) as z:
        template = z.read("formats/statements/acme-registry.yaml").decode()
        sample = z.read("formats/samples/acme-registry.txt").decode()
        expected = YAML(typ="safe").load(
            z.read("formats/samples/acme-registry.expected.yaml").decode())

    from app import docformats
    loaded = docformats.parse_statement_template("acme-registry", template)
    values = loaded.extract(sample)

    assert loaded.matches(sample)
    for key, want in expected.items():
        got = values.get(key)
        assert str(got) == str(want), f"{key}: template read {got!r}, expected {want!r}"


@pytest.mark.parametrize("slug", ["../escape", "a/b", ""])
def test_a_hostile_slug_cannot_write_outside_the_bundle(slug):
    """The name comes from a form field and becomes a path inside a zip. A
    zip-slip here would be a path traversal on whoever extracts it."""
    data = contribute.bundle(slug, TEMPLATE, "sample")

    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            assert ".." not in name
            assert name.startswith("formats/")
            assert name.count("/") == 2


# --------------------------------------------------------------------------- #
# Through the routes
# --------------------------------------------------------------------------- #

from test_routes import make_login, session_csrf  # noqa: E402

HTML = {"accept": "text/html"}
MAPPING = ('{"payment_date": {"after": ["Payment date"], "type": "date"}, '
           '"net_amount": {"after": ["Net amount"], "type": "money"}}')


def test_a_designed_template_installs_on_this_machine(client, session_factory,
                                                      tmp_path, monkeypatch, app_module):
    """The gap T26d was opened for: the designer offered a download and nothing
    else, so a template you built could not be used without putting a file on
    the container's disk yourself."""
    monkeypatch.setattr(app_module.settings.imports, "templates_dir", str(tmp_path))
    make_login(client, session_factory)

    response = client.post(
        "/imports-exports/statement/design/install",
        data={"_csrf": session_csrf(session_factory), "name": "Acme Registry",
              "mapping": MAPPING, "marker": "ACME REGISTRY"},
        headers=HTML, follow_redirects=False)

    assert response.status_code == 303
    written = (tmp_path / "statements" / "acme-registry.yaml")
    assert written.exists()
    assert "Payment date" in written.read_text()


def test_installing_a_designed_template_is_admin_only(client, session_factory,
                                                      tmp_path, monkeypatch, app_module):
    """A template changes how EVERY user's imports parse — same rule as the
    upload route, and worth a test because it is a different route."""
    monkeypatch.setattr(app_module.settings.imports, "templates_dir", str(tmp_path))
    make_login(client, session_factory, admin=False)

    response = client.post(
        "/imports-exports/statement/design/install",
        data={"_csrf": session_csrf(session_factory), "name": "Acme",
              "mapping": MAPPING},
        headers=HTML)

    assert response.status_code == 403
    assert not (tmp_path / "statements").exists()


def test_the_contribution_downloads_as_a_zip_of_three_files(client, session_factory):
    make_login(client, session_factory)

    response = client.post(
        "/imports-exports/statement/design/contribute",
        data={"_csrf": session_csrf(session_factory), "name": "Acme Registry",
              "mapping": MAPPING, "marker": "ACME REGISTRY", "text": STATEMENT},
        headers=HTML)

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "acme-registry-contribution.zip" in response.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(response.content)) as z:
        assert len(z.namelist()) == 3


def test_the_downloaded_contribution_is_already_redacted(client, session_factory):
    """The route redacts before bundling. If it did not, the first thing a
    contributor did with this feature would be to publish their address."""
    make_login(client, session_factory)

    response = client.post(
        "/imports-exports/statement/design/contribute",
        data={"_csrf": session_csrf(session_factory), "name": "Acme",
              "mapping": MAPPING, "text": STATEMENT},
        headers=HTML)

    with zipfile.ZipFile(io.BytesIO(response.content)) as z:
        sample = z.read("formats/samples/acme.txt").decode()

    assert "1042.75" not in sample
    assert "0012345678" not in sample
    assert "john.smith@example.com" not in sample


def test_the_contribution_needs_the_csrf_token(client, session_factory):
    make_login(client, session_factory)

    response = client.post(
        "/imports-exports/statement/design/contribute",
        data={"name": "Acme", "mapping": MAPPING, "text": STATEMENT}, headers=HTML)

    assert response.status_code == 403


def test_the_contribution_needs_a_session(client):
    response = client.post("/imports-exports/statement/design/contribute",
                           data={"name": "A", "mapping": MAPPING, "text": "x"},
                           headers=HTML, follow_redirects=False)

    assert response.status_code in (302, 303, 401, 403)


@pytest.mark.parametrize("text", ["redacted", "raw"])
def test_the_expected_file_always_describes_the_sample_shipped_beside_it(text):
    """The invariant, whatever goes in: run the bundled template over the
    bundled sample and you get the bundled expected values. A contribution
    where those three disagree fails CI the moment it is opened, and the
    contributor never sees why.

    (Note for anyone mutating this: inside `bundle`, reading the sample with
    or without its header comment is an equivalent change — the header carries
    no labels a template matches. The property worth pinning is this one.)
    """
    from ruamel.yaml import YAML

    from app import docformats

    source = contribute.redact(STATEMENT)[0].text if text == "redacted" else STATEMENT
    data = contribute.bundle("acme-registry", TEMPLATE, source)

    with zipfile.ZipFile(io.BytesIO(data)) as z:
        template = z.read("formats/statements/acme-registry.yaml").decode()
        sample = z.read("formats/samples/acme-registry.txt").decode()
        expected = YAML(typ="safe").load(
            z.read("formats/samples/acme-registry.expected.yaml").decode())

    values = docformats.parse_statement_template("x", template).extract(sample)
    for key, want in expected.items():
        assert str(values.get(key)) == str(want), key

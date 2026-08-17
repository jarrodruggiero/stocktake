"""Mapping a value the app did not expect there.

**The correction this file exists for, from real use:**
the designer refused mappings it could not work out, which reads as the tool
already knowing the answer and declining to let you be wrong. It cannot know
that. A statement it has never seen is exactly the case the designer is for,
and a refusal there leaves no way forward at all — the message even said "type
one", with nowhere to type it.

So the model is now:

  * **A click always maps.** The only genuine refusal left is an index that is
    not a word in the document.
  * **Labels are editable.** Where inference finds none, or finds the wrong
    one, the label is typed in and read back live against the document.
  * **A discrepancy is reported, not enforced** — and only where the app can
    actually tell: the value's type against the type the field expects. It
    clears when the mapping is fixed.
"""

from __future__ import annotations

import pytest

from app import docformats

# The shape of a real MUFG/Betashares distribution advice, with every value
# invented. It is here because its TABLE row is the case most easily
# refused: a figure in a column has no label to its left on the same line.
TABLE_STATEMENT = """Distribution Advice
Payment date: 16 July 2026
Class Description Rate per Unit Participating Units Gross Amount
Ordinary Units 11.222333 cents 100 $123.45
Net Amount: $123.45
This amount has been applied to 3 units at $41.150000 per unit: $123.45
Ordinary units allotted this distribution: 3
"""


def index_of(text: str, word: str, occurrence: int = 0) -> int:
    hits = [t.index for t in docformats.tokenize(text) if t.text == word]
    return hits[occurrence]


# --------------------------------------------------------------------------- #
# A click always maps
# --------------------------------------------------------------------------- #

def test_a_value_in_a_table_column_is_no_longer_refused():
    """The exact failure reported. `$123.45` in the Gross Amount column has no
    label to its left, so the app cannot infer one — which is a reason to ask
    for one, not a reason to refuse the mapping."""
    found = docformats.infer_field(TABLE_STATEMENT,
                                   index_of(TABLE_STATEMENT, "$123.45"))

    assert found.problem is None
    assert found.needs_label is True
    assert found.type == "money"


def test_a_value_whose_label_does_not_read_it_back_is_still_mapped():
    """The other refusal: a label that exists but finds a different value
    first. Worth saying; not worth blocking."""
    text = "Amount: 10.00\nAmount: 20.00\n"
    found = docformats.infer_field(text, index_of(text, "20.00"))

    assert found.problem is None
    assert found.warning


def test_only_a_word_that_is_not_there_is_refused():
    found = docformats.infer_field(TABLE_STATEMENT, 9999)

    assert found.problem


def test_an_inferred_mapping_still_reads_its_value_back():
    """The good path has not regressed: where a label IS found, it is proved
    against the document rather than guessed at."""
    found = docformats.infer_field(TABLE_STATEMENT,
                                   index_of(TABLE_STATEMENT, "$123.45", 1))

    assert found.labels[0] == "net amount"
    assert str(found.value) == "123.45"
    assert found.needs_label is False


# --------------------------------------------------------------------------- #
# A typed label, checked against the document
# --------------------------------------------------------------------------- #

def test_a_typed_label_reads_the_value_it_finds():
    reading = docformats.read_with(TABLE_STATEMENT, "net amount", "money")

    assert str(reading.value) == "123.45"
    assert reading.problem is None


def test_a_typed_label_that_finds_nothing_says_so():
    reading = docformats.read_with(TABLE_STATEMENT, "no such wording", "money")

    assert reading.value is None
    assert reading.problem


def test_a_typed_label_is_matched_case_insensitively():
    """People type labels the way the statement prints them, in title case."""
    assert docformats.read_with(TABLE_STATEMENT, "Net Amount", "money").value is not None


def test_a_typed_label_can_pick_a_table_column_by_its_heading():
    """The way out of the refusal: the column heading IS on the line above,
    and a template that reads `Gross Amount` finds the figure under it."""
    reading = docformats.read_with(TABLE_STATEMENT, "cents 100", "money")

    assert str(reading.value) == "123.45"


# --------------------------------------------------------------------------- #
# Discrepancies: reported, never enforced
# --------------------------------------------------------------------------- #

def test_mapping_a_date_to_a_money_field_is_flagged():
    found = docformats.infer_field(TABLE_STATEMENT,
                                   index_of(TABLE_STATEMENT, "16"), expect="money")

    assert found.mismatch
    assert found.problem is None      # flagged, not refused


def test_mapping_money_to_a_money_field_is_not_flagged():
    found = docformats.infer_field(TABLE_STATEMENT,
                                   index_of(TABLE_STATEMENT, "$123.45", 1),
                                   expect="money")

    assert found.mismatch is None


def test_a_price_with_many_decimals_is_not_flagged_against_money():
    """`$41.150000` infers as a number rather than money, and a unit price
    quoted to six places is a perfectly good allotment price. Flagging it would
    be the tool being wrong about what a mistake is — which is the whole
    complaint."""
    found = docformats.infer_field(TABLE_STATEMENT,
                                   index_of(TABLE_STATEMENT, "$41.150000"),
                                   expect="money")

    assert found.mismatch is None


def test_an_integer_is_not_flagged_against_a_number_field():
    found = docformats.infer_field(TABLE_STATEMENT,
                                   index_of(TABLE_STATEMENT, "3"), expect="number")

    assert found.mismatch is None


def test_text_accepts_anything():
    found = docformats.infer_field(TABLE_STATEMENT,
                                   index_of(TABLE_STATEMENT, "16"), expect="text")

    assert found.mismatch is None


def test_no_expectation_means_no_discrepancy():
    found = docformats.infer_field(TABLE_STATEMENT, index_of(TABLE_STATEMENT, "16"))

    assert found.mismatch is None


# --------------------------------------------------------------------------- #
# A date is one value, not three words
# --------------------------------------------------------------------------- #

def test_a_written_date_spans_its_three_words():
    """"16 July 2026" is one value. The span is what lets the interface
    highlight all three rather than leaving somebody wondering why clicking the
    year did something different from clicking the day."""
    found = docformats.infer_field(TABLE_STATEMENT, index_of(TABLE_STATEMENT, "16"))

    assert found.type == "date"
    assert found.span == 3


@pytest.mark.parametrize("word,offset", [("16", 0), ("July", 1), ("2026", 2)])
def test_clicking_any_part_of_a_date_selects_the_whole_date(word, offset):
    """Clicking the month or the year must map the same date as clicking the
    day. Before this, two of the three did something else entirely."""
    found = docformats.infer_field(TABLE_STATEMENT, index_of(TABLE_STATEMENT, word))

    assert found.type == "date"
    assert found.span == 3
    assert found.starts_at == index_of(TABLE_STATEMENT, "16")
    assert str(found.value) == "2026-07-16"


def test_a_single_word_value_spans_one():
    found = docformats.infer_field(TABLE_STATEMENT,
                                   index_of(TABLE_STATEMENT, "$123.45", 1))

    assert found.span == 1
    assert found.starts_at == index_of(TABLE_STATEMENT, "$123.45", 1)

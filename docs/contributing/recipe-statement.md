# Recipe: support a new statement layout

!!! tip "The designer can produce all three files"
    After mapping a statement, **Prepare a contribution** gives you a zip of the
    template, a redacted sample, and the expected values — under the paths they
    live at here, so it is a copy-in rather than a chore.

    Holder and account numbers, tax file numbers, emails and phone numbers are
    masked; amounts keep their shape and get different digits, so the expected
    values still tell one field from another. Names and addresses are the ones
    no pattern can find — give the sample a read before you send it.

## Check it against your real PDF first

Before you contribute a template, prove it reads the document it was built for:

1. **Imports → Dividend statement (PDF)**, choose your file.
2. Set **Read it with** to your template.
3. **Parse.**
4. Read the preview. Every field appears, filled in from your statement — that
   is the same form you would commit from, so what you see is what the template
   produces.
5. If a field is empty or wrong, go back to the designer, fix the label, install
   the template again and parse again. Nothing is saved until you commit.

**Do this with the real PDF.** It is the only step that exercises the whole
chain — text extraction (or OCR), template selection, and every label — against
the document as it actually arrives.

## Why we do not ship sample PDFs

A contribution carries a **text** sample, never the PDF it came from, and that
is deliberate.

A statement PDF carries far more than the words on the page: metadata, the
producer and author fields, embedded fonts, sometimes the original file path,
and — on several Australian registries — a **barcode encoding the holder
number**. Redacting all of that reliably is much harder than redacting text, and
it is the kind of hard that people get wrong once and cannot take back, because
a merged pull request is public forever.

Text you can read every character of before you send it. It reviews in a diff,
it adds no binaries to the repository, and the parser works on extracted text
anyway — so nothing about the template is left untested.

The scanned-PDF path is covered too: `tests/test_ocr_endtoend.py` renders each
shipped sample to an image-only PDF at test time and checks OCR still reads the
recorded values. That gives us the OCR regression test without anyone
publishing a document.

**You do not write Python for this.** A statement layout is a template file,
and adding one is three files in a pull request.

That is deliberate. Nobody maintaining this project holds accounts at every
registry, so a parser written here could never be verified by the person
merging it. You have the document; you author the format; CI keeps it honest
from then on.

Statements are read with `pdfplumber` **entirely locally** — no cloud OCR, no
LLM, no upload. That is a hard constraint, not a default.

!!! tip "Start with the test"
    [Step 3](#3-contribute-a-redacted-sample) comes first in practice: add the
    redacted sample and its expected values, run
    `pytest tests/test_format_samples.py -q`, and watch it fail to parse. The
    template you then write has a target instead of a guess. See
    [Testing](testing.md#write-the-test-first).

## 1. See what your statement says

The parser works on extracted text, not the visual layout:

```python
from app import statements
print(statements.extract_text(open("statement.pdf", "rb").read()))
```

## 2. Write the template

Save it as `app/formats/statements/<registry>.yaml`. Every field is a label to
look for and the type of value that follows:

```yaml
name: Example Registry (Australia)

match:                       # optional; how this layout is recognised
  any_of: ["Example Registry Services"]

fields:
  payment_date:
    after: ["payment date", "date paid"]
    type: date
  net_amount:
    after: ["net amount", "amount paid"]
    type: money
  franking_credits:
    after: ["franking credit", "imputation credit"]
    type: money
  drp_units:
    after: ["units allotted", "securities issued"]
    type: integer
  drp_price:
    after: ["allotment price", "issue price"]
    type: money
```

**Label order is preference** — the first one found wins, so put the wording
your layout uses most reliably first.

**Types**: `money` (requires cents), `number`, `integer`, `date`, `text`.

Two behaviours worth knowing, because they are the difference between reading
the right number and a plausible wrong one:

- `money` may step over intervening digits. "Franking credits 2024 ... $420.00"
  works, because requiring cents is what stops the year being read as money.
- `integer` and `date` may **not** — they take the first digits after the
  label. This is what keeps `units allotted` from picking up `units held`,
  which appears on the same page and is a bigger, more prominent number. Always
  anchor on the verb.

### Multi-fund advices

If one statement covers several funds, describe the table instead of fields:

```yaml
rows:
  shape: "TICKER NUM INT NUM NUM NUM NUM INT NUM"
  columns: [ticker, drp_price, units_held, per_security, tax_withheld,
            amount, brought_forward, allotted, carried_forward]
```

Shape tokens: `TICKER`, `NUM`, `INT`, `MONEY`, `TEXT`. The token count must
equal the column count — an off-by-one would shift every value one column left,
so it is refused at load.

A template with `rows:` needs no `match:` block. **The rows parsing is the
signal**: a table of one line per fund is unmistakable, and more durable than a
brand name registries reword between years.

### There are no regular expressions

On purpose. A template arrives by pull request, which makes it untrusted input:
a catastrophic backtracking pattern is a denial of service, and a regex is
unreviewable by anyone who does not read regex. Labels are escaped and slotted
into fixed expressions, so nothing you write reaches `re.compile`.

If a layout genuinely cannot be expressed this way, say so in an issue rather
than working around it — the format should grow deliberately.

## 3. Contribute a redacted sample

Two more files, and these are what make the template trustworthy:

`app/formats/samples/<registry>.txt` — the extracted text, **redacted**:

```text
# Sample for: example-registry
# Registry: Example Registry — distribution advice
# Contributed: 2026-08-03
#
# REDACTED. Every amount, date, holder number and name below is invented.

EXAMPLE REGISTRY SERVICES
Payment date:      15 March 2026
Net amount paid:   $1,042.75
...
```

`app/formats/samples/<registry>.expected.yaml` — what it must produce:

```yaml
payment_date: 2026-03-15
net_amount: "1042.75"
```

**Text, not a PDF**, because you can read every character you are publishing —
which is not true of a PDF's metadata, embedded fonts and revision history.

### Redacting properly

Change every amount, date, holder number, name and address. Keep the *layout*:
the column positions, the labels, the line breaks. Those are what the template
matches on and the only part anyone needs.

CI checks two things automatically — that the file says `REDACTED` in its
header, and that nothing in it is shaped like a **tax file number**. Those are
a backstop, not a substitute for reading what you are about to publish.

## 4. Run the tests

```sh
pytest tests/test_format_samples.py -q
```

That generates a test per sample: your template must claim your document, and
extract exactly what your expectation file says. It then runs on every commit
forever, so your format cannot be broken by someone else's change.

## What good looks like

- **A narrow label that misses beats a greedy one that finds the wrong
  number.** The preview catches a miss; nobody catches a plausible wrong value.
- **Test the near-miss.** If your statement has both "Net amount" and "Net
  amount reinvested", make sure your labels pick the one you meant.
- **Two samples are better than one.** One proves it parsed; two prove it
  generalises.

## If it will not parse at all

That is an acceptable outcome and the app is built for it — enter the dividend
by hand on the instrument page. A template that reads your layout is welcome; a
template loosened until it catches yours and misreads others is not.

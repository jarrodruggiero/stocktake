"""Document formats as data: how to read a statement or a broker export.

Adding support for a registry or a broker should not need Python. The CSV side
already worked this way — `kind: mapped` is a column mapping — and this gives
statements the same treatment, in shipped, shareable files.

The reason is not elegance. **Nobody can test a format they have no document
for.** Whoever maintains this holds accounts at almost none of these registries,
so a parser per document is code the merger cannot verify. Formats as data
invert that: the person with the statement authors the template, ships a
redacted text sample with it, and CI keeps it honest forever.

A template is a label and a value of a known type:

    fields:
      net_amount:
        after: ["net amount", "amount paid"]
        type: money

and, for multi-fund advices, a row *shape* rather than a regex:

    rows:
      shape: "TICKER NUM INT NUM"
      columns: [ticker, price, units, amount]

**No pattern in a template ever reaches `re.compile`.** A template arrives by
pull request, which makes it untrusted input, and a raw regex from untrusted
input is two problems: catastrophic backtracking is a denial of service, and a
regex is unreviewable by anyone who does not read regex — which defeats the
point of making formats contributable. Labels are `re.escape`d into a fixed,
known-good expression for the declared type. Nothing was lost: every pattern
that existed before this ports across unchanged, which is the test that proves
it.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

log = logging.getLogger(__name__)

BUILTIN_DIR = Path(__file__).parent / "formats"

# What each declared type is allowed to look like, and what may sit between the
# label and the value. Both are fixed here rather than in a template — this is
# the whole safety property of the format.
TYPE_PATTERNS: dict[str, str] = {
    # Amounts always carry cents in these documents; requiring them stops a
    # label's own trailing digits ("Franking credits 2024") being read as money.
    "money": r"\$?\s*(-?[\d,]+\.\d{2})",
    "number": r"(-?[\d,]+(?:\.\d+)?)",
    "integer": r"([\d,]+)",
    "date": r"(\d{1,2}[ /-][\w]+[ /-]\d{2,4})",
    "text": r"(\S+)",
}

# What may separate a label from its value.
#
# Money may step over intervening digits — "Franking credits 2024 ... $420.00"
# — because the cents requirement above stops the year being read as an amount.
# An integer or a date has no such guard, so nothing may be skipped to reach
# them. All bounded at 80 characters: wide enough for these documents' column
# padding, narrow enough not to reach a value on an unrelated line.
TYPE_SEPARATORS: dict[str, str] = {
    "money": r".{0,80}?",
    "number": r"[^\d-]{0,80}",
    "integer": r"[^\d]{0,80}",
    "date": r"[^\d]{0,80}",
    "text": r"[\s:]{1,10}",
}

DATE_FORMATS = ("%d %B %Y", "%d %b %Y", "%d/%m/%Y", "%d-%m-%Y", "%d %m %Y",
                "%d/%m/%y", "%d %B, %Y")

# Row shapes: a token vocabulary instead of a regex. A creator UI can generate
# one of these by looking at a line somebody pointed at, which it could not
# safely do with a regex.
SHAPE_TOKENS: dict[str, str] = {
    "TICKER": r"([A-Z]{2,6})",
    "NUM": r"([\d,]+(?:\.\d+)?)",
    "INT": r"(\d+)",
    "MONEY": r"\$?([\d,]+\.\d{2})",
    "TEXT": r"(\S+)",
}


class TemplateError(ValueError):
    """A template is malformed. Raised at load time, never at parse time —
    a broken template must fail when it is installed, not when somebody is
    halfway through importing a document."""


@dataclass
class FieldSpec:
    after: list[str]
    type: str

    def __post_init__(self) -> None:
        if self.type not in TYPE_PATTERNS:
            raise TemplateError(
                f"unknown field type {self.type!r}; expected one of "
                f"{', '.join(sorted(TYPE_PATTERNS))}"
            )
        if not self.after:
            raise TemplateError("a field needs at least one `after` label")

    def compiled(self) -> list[re.Pattern]:
        """One safe expression per label. The label is escaped; everything
        around it comes from this module."""
        sep = TYPE_SEPARATORS[self.type]
        value = TYPE_PATTERNS[self.type]
        return [
            re.compile(re.escape(label) + sep + value, re.I | re.S)
            for label in self.after
        ]


@dataclass
class RowSpec:
    shape: str
    columns: list[str]

    def __post_init__(self) -> None:
        tokens = self.shape.split()
        unknown = [t for t in tokens if t not in SHAPE_TOKENS]
        if unknown:
            raise TemplateError(
                f"unknown row shape token(s) {unknown}; expected from "
                f"{', '.join(sorted(SHAPE_TOKENS))}"
            )
        if len(tokens) != len(self.columns):
            raise TemplateError(
                f"row shape has {len(tokens)} tokens but {len(self.columns)} "
                "column names — they must line up one to one"
            )

    def compiled(self) -> re.Pattern:
        body = r"\s+".join(SHAPE_TOKENS[t] for t in self.shape.split())
        return re.compile(rf"^{body}\s*$", re.M)


@dataclass
class StatementTemplate:
    key: str
    name: str
    match_any: list[str] = field(default_factory=list)
    fields: dict[str, FieldSpec] = field(default_factory=dict)
    rows: RowSpec | None = None
    # "builtin" or "installed". `pick()` uses it as a tie-break, because a
    # template somebody installed here says more about their statements than
    # one that ships with the app — and without it, an installed template with
    # no markers could never beat the generic fallback that matches everything.
    source: str = "builtin"

    def matches(self, text: str) -> bool:
        """Whether this template claims the document.

        For a table layout, **the rows matching IS the signal** — a document
        laid out as one row per fund is unmistakable, and far more reliable
        than hunting for a brand name that registries reword between years.
        So a template with a `rows:` shape must actually find a row, whatever
        its markers say.

        No `match_any` and no rows means "try me for anything", which is how a
        generic fallback works — `pick` tries those last.
        """
        low = text.lower()
        if self.match_any and not any(n.lower() in low for n in self.match_any):
            return False
        if self.rows is not None:
            return bool(self.rows.compiled().search(text))
        return True

    def extract(self, text: str) -> dict[str, object]:
        out: dict[str, object] = {}
        for name, spec in self.fields.items():
            out[name] = _first_value(spec, text)
        if self.rows is not None:
            out["rows"] = _extract_rows(self.rows, text)
        return out


def _first_value(spec: FieldSpec, text: str):
    """The first label that matches wins, in the order the template lists them.

    Order is the template author's statement of preference: "net amount" before
    "amount paid" means the former is the more reliable label on that layout.
    """
    for pattern in spec.compiled():
        found = pattern.search(text)
        if found:
            return coerce(found.group(1), spec.type)
    return None


def _extract_rows(spec: RowSpec, text: str) -> list[dict]:
    pattern = spec.compiled()
    rows = []
    for found in pattern.finditer(text):
        rows.append(dict(zip(spec.columns, found.groups())))
    return rows


def coerce(raw: str | None, type_: str):
    """A captured string as the type the template declared, or None.

    None rather than an exception: a document that does not quite match is the
    normal case, and every value is shown for correction before anything is
    written. A hard failure here would turn a partially-read statement into no
    statement at all.
    """
    if raw is None:
        return None
    cleaned = raw.replace(",", "").replace("$", "").strip()
    if type_ == "date":
        return _parse_date(raw)
    if type_ == "text":
        return raw.strip() or None
    try:
        value = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None
    return int(value) if type_ == "integer" else value


def _parse_date(raw: str | None) -> dt.date | None:
    if not raw:
        return None
    candidate = re.sub(r"[/-]", " ", raw).strip()
    for fmt in ("%d %B %Y", "%d %b %Y", "%d %m %Y", "%d %B, %Y", "%d %m %y"):
        try:
            return dt.datetime.strptime(candidate, fmt).date()
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #

def _yaml():
    """Imported on use — see app/memory.py. ruamel is already a dependency for
    the settings editor, so this adds nothing to the image."""
    from ruamel.yaml import YAML

    return YAML(typ="safe")


def replace(template: StatementTemplate, **changes) -> StatementTemplate:
    """A copy with fields changed. `dataclasses.replace` under a name that does
    not collide with the module's other imports."""
    import dataclasses

    return dataclasses.replace(template, **changes)


def mapping_of(template: StatementTemplate) -> dict[str, dict]:
    """A template's fields in the shape the designer holds and `template_yaml`
    consumes.

    This is what makes editing possible: the editor loads a template, shows the
    mapping, and saves it back through the same function that writes a new one
    — so a load-edit-save cycle cannot lose a field to a second code path.
    """
    return {name: {"after": list(spec.after), "type": spec.type}
            for name, spec in template.fields.items()}


def parse_statement_template(key: str, text: str) -> StatementTemplate:
    """A template from YAML text.

    Split out from `load_statement_template` so a template can be checked
    before it is a file — the designers build one in memory and need to run it
    over a sample to see whether it works, which is the whole point of
    contributing one.
    """
    data = _yaml().load(text) or {}
    if not isinstance(data, dict):
        raise TemplateError(f"{key}: expected a mapping at the top level")
    try:
        fields = {
            name: FieldSpec(after=list(spec.get("after") or []),
                            type=spec.get("type", "text"))
            for name, spec in (data.get("fields") or {}).items()
        }
    except AttributeError as exc:  # a field that isn't a mapping
        raise TemplateError(f"{key}: each field needs `after` and `type`") from exc
    rows = None
    if data.get("rows"):
        rows = RowSpec(shape=data["rows"].get("shape", ""),
                       columns=list(data["rows"].get("columns") or []))
    return StatementTemplate(
        key=key,
        name=data.get("name") or key,
        match_any=list((data.get("match") or {}).get("any_of") or []),
        fields=fields,
        rows=rows,
    )


def load_statement_template(path: Path, source: str = "builtin") -> StatementTemplate:
    return replace(parse_statement_template(path.stem, path.read_text()), source=source)


def load_statement_templates(extra_dir: Path | None = None) -> list[StatementTemplate]:
    """Every shipped template, then any the operator has added.

    Operator templates come last so a local file wins over a shipped one of the
    same name — the same override direction the settings layer uses. A template
    that fails to load is logged and skipped rather than taking the app down:
    one bad file in a drop-in directory must not stop the others working.
    """
    templates: dict[str, StatementTemplate] = {}
    for directory, source in ((BUILTIN_DIR / "statements", "builtin"),
                              (extra_dir, "installed")):
        if directory is None or not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.yaml")):
            try:
                template = load_statement_template(path, source)
            except (TemplateError, Exception) as exc:  # noqa: B014 - yaml errors vary
                log.warning("ignoring statement template %s: %s", path.name, exc)
                continue
            templates[template.key] = template
    return list(templates.values())


# --------------------------------------------------------------------------- #
# Authoring: turning "that number there" into a template. Someone points at a
# value and says which field it is; everything below works out how to
# *describe* finding it. The inference lives here, not in JavaScript, so it
# can be tested — decisions.md #37.
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Token:
    index: int
    text: str
    line: int


MONTHS = ("january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december")


def tokenize(text: str) -> list[Token]:
    """Whitespace-separated words, numbered, with the line each came from.

    The index is what the UI sends back when someone points at a word, and the
    line is what stops a label being taken from the row above.
    """
    tokens: list[Token] = []
    for line_no, line in enumerate(text.splitlines()):
        for word in line.split():
            tokens.append(Token(index=len(tokens), text=word, line=line_no))
    return tokens


def _looks_like(word: str) -> str | None:
    """The narrowest type this word could be read as, or None for prose."""
    bare = word.replace(",", "").replace("$", "").rstrip(".:;")
    if re.fullmatch(r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}", bare):
        return "date"
    if re.fullmatch(r"-?\d+\.\d{2}", bare):
        return "money"
    if re.fullmatch(r"-?\d+", bare):
        return "integer"
    if re.fullmatch(r"-?\d+\.\d+", bare):
        return "number"
    return None


def infer_type(tokens: list[Token], index: int) -> tuple[str, int]:
    """The type of the value at `index`, and how many tokens it spans.

    Dates are the reason for the span: "15 March 2026" is three words, and a
    template that captured only "15" would be worse than useless.
    """
    word = tokens[index].text
    following = [t.text.lower().rstrip(",") for t in tokens[index + 1:index + 3]]
    if (re.fullmatch(r"\d{1,2}", word.rstrip(".:;"))
            and len(following) == 2
            and any(following[0].startswith(m[:3]) for m in MONTHS)
            and re.fullmatch(r"\d{2,4}", following[1])):
        return "date", 3
    return _looks_like(word) or "text", 1


# A label ends at punctuation that separates it from its value, and cannot
# contain a number — "Units held 4,213 Units allotted 12" must not produce the
# label "held 4,213 units allotted".
_LABEL_WORD = re.compile(r"^[A-Za-z][A-Za-z'&/()-]*$")


def infer_labels(tokens: list[Token], index: int, max_words: int = 4) -> list[str]:
    """Candidate `after:` labels for the value at `index`, longest first.

    Longest first because a longer label is more specific and less likely to
    collide with another line: "net amount paid" before "amount paid" before
    "paid". Offering several is deliberate — the author picks the one that
    reads like their document, and can see immediately if a short one matches
    the wrong place.

    Only words on the SAME line are considered. A label from the line above is
    usually a column heading, which describes a whole column rather than the
    one value that was pointed at.
    """
    line = tokens[index].line
    words: list[str] = []
    for token in reversed(tokens[:index]):
        if token.line != line:
            break
        candidate = token.text.rstrip(":.")
        if not _LABEL_WORD.match(candidate):
            break
        words.insert(0, candidate)
        if len(words) >= max_words:
            break
    return [" ".join(words[i:]).lower() for i in range(len(words))]


# Which inferred types are near enough to a field's declared type to say
# nothing. The point of the discrepancy check is to catch a date mapped to a
# money field, NOT to second-guess a unit price quoted to six decimal places —
# being wrong about what a mistake is was the original complaint.
COMPATIBLE: dict[str, set[str]] = {
    "money": {"money", "number", "integer"},
    "number": {"number", "money", "integer"},
    "integer": {"integer", "number"},
    "date": {"date"},
    "text": {"text", "money", "number", "integer", "date"},
}


@dataclass
class Inference:
    """What the app worked out from a value somebody pointed at.

    **`problem` means the click was impossible, not that the mapping was
    unwise.** Refusing anything it could not work out would read as the tool
    knowing the answer — and it cannot, because a statement it has never seen
    is what the designer is for. Anything it is unsure about arrives as
    `warning`, `needs_label` or `mismatch`, and the person mapping decides.
    """

    type: str
    labels: list[str]          # candidates, longest (most specific) first
    value: object              # what the best candidate actually reads back
    span: int                  # tokens the value occupies
    starts_at: int = 0         # the FIRST token of the value, which is not
                               # necessarily the one that was clicked
    problem: str | None = None  # the click could not be honoured at all
    warning: str | None = None  # it worked, but the result is probably wrong
    needs_label: bool = False   # nothing to infer from; type one in
    mismatch: str | None = None  # the value's type is not what the field wants


@dataclass
class Reading:
    """What a hand-typed label finds in this document."""

    value: object = None
    problem: str | None = None


def read_with(text: str, label: str, type_: str) -> Reading:
    """Run a label the person typed against the document, and report what it
    reads.

    This is what makes a hand-typed label usable rather than a guess: a label
    is only worth anything if it finds the value, and the only way to know is
    to try it on the document it came from.
    """
    label = (label or "").strip()
    if not label:
        return Reading(problem="type the words that come before this value")
    got = _first_value(FieldSpec(after=[label.lower()], type=type_), text)
    if got is None:
        return Reading(problem=f"nothing found after {label!r} in this document")
    return Reading(value=got)


def _value_start(tokens: list[Token], index: int) -> tuple[int, str, int]:
    """The first token of the value the clicked token belongs to.

    A written date is three words. Clicking the month or the year has to map
    the same date as clicking the day, or two of the three do something else
    entirely and nobody can tell why. Looks back a couple of tokens for one
    whose inferred span covers the click.
    """
    type_, span = infer_type(tokens, index)
    if span > 1:
        return index, type_, span
    # (Removing this early return is an EQUIVALENT change, not a bug the tests
    # miss: the fall-through at the bottom returns the same triple. It is here
    # to say the common case plainly rather than by exhausting a loop.)
    for back in (1, 2):
        start = index - back
        if start < 0 or tokens[start].line != tokens[index].line:
            break
        earlier_type, earlier_span = infer_type(tokens, start)
        if earlier_span > back:
            return start, earlier_type, earlier_span
    return index, type_, span


def infer_field(text: str, index: int, expect: str | None = None) -> Inference:
    """Describe how to find the value at `index`, and prove it reads back.

    The proof matters: an inferred label is a guess about wording, and the only
    way to know it is right is to run it against the document it came from.

    **What it will not do is refuse.** Where no label can be inferred, or where
    none of them reads the value back, the mapping is still returned — with
    `needs_label` set, or a warning — because the person mapping can type a
    label the app could not guess, and a statement nobody has seen before is
    precisely the case this exists for.

    `expect` is the type the chosen field wants. Where it is given and the
    value's type is not near it, `mismatch` says so. That is the only kind of
    mistake the app can actually recognise.
    """
    tokens = tokenize(text)
    if not 0 <= index < len(tokens):
        return Inference(type="text", labels=[], value=None, span=1,
                         problem="that is not a word in this document")

    index, type_, span = _value_start(tokens, index)
    mismatch = None
    if expect and type_ not in COMPATIBLE.get(expect, {expect}):
        mismatch = (f"that reads as a {type_} and this field expects "
                    f"{'an' if expect[0] in 'aeiou' else 'a'} {expect}")

    labels = infer_labels(tokens, index)
    wanted = " ".join(t.text for t in tokens[index:index + span])
    if not labels:
        return Inference(
            type=type_, labels=[], value=coerce(wanted, type_), span=span,
            starts_at=index, needs_label=True, mismatch=mismatch,
            warning="nothing on this line reads as a label for it — type the "
                    "words that come before it on your statement",
        )

    # Keep only the candidates that actually find this value, and read it.
    working, value = [], None
    for label in labels:
        spec = FieldSpec(after=[label], type=type_)
        got = _first_value(spec, text)
        if got is not None and str(got) == str(coerce(wanted, type_)):
            working.append(label)
            value = got
    if not working:
        return Inference(
            type=type_, labels=labels, value=coerce(wanted, type_), span=span,
            starts_at=index, needs_label=True, mismatch=mismatch,
            warning="the words before this value do not find it again — the "
                    "label may appear more than once, or the value may be in a "
                    "table. Edit the label to something that does.",
        )

    # A label that is a ticker works perfectly on the document in front of you
    # and on nobody else's. It is the single most likely way to author a
    # template that looks right and is useless to share, so it is called out
    # rather than silently accepted.
    warning = None
    if all(re.fullmatch(r"[a-z]{2,6}", label) for label in working):
        warning = (
            f"the label {working[0]!r} looks like a ticker or a holding name, "
            "so this would only match your own statement. If the value is in a "
            "table, describe the table with a row shape instead."
        )
    return Inference(type=type_, labels=working, value=value, span=span,
                     starts_at=index, warning=warning, mismatch=mismatch)


def template_yaml(name: str, fields: dict[str, dict], match: list[str],
                  rows: RowSpec | None = None) -> str:
    """A template file, as text, ready to save or attach to a pull request.

    Hand-built rather than dumped by the YAML library so the output carries the
    comments a contributor needs — a file that arrives explaining itself gets
    reviewed properly.
    """
    lines = [
        "# Generated by the statement template designer.",
        "# Check the labels below read the way YOUR statement words them, then",
        "# see docs/contributing/recipe-statement.md to contribute it.",
        f"name: {name}",
    ]
    if match:
        lines += ["", "match:", "  any_of:"]
        lines += [f"    - {m}" for m in match]
    lines += ["", "fields:"]
    for field_name, spec in fields.items():
        lines.append(f"  {field_name}:")
        lines.append("    after:")
        lines += [f"      - {label}" for label in spec["after"]]
        lines.append(f"    type: {spec['type']}")
    if rows is not None:
        # A row shape is the strongest signal a template has. Dropping it on
        # save would quietly turn a specific template into one that matches
        # everything — which is how an edit meant to fix one label breaks
        # every other statement.
        lines += ["", "rows:", f"  shape: \"{rows.shape}\"", "  columns:"]
        lines += [f"    - {column}" for column in rows.columns]
    return "\n".join(lines) + "\n"


def load_broker_formats(overrides: dict | None = None,
                        extra_dir: Path | None = None) -> dict[str, dict]:
    """Shipped broker CSV formats, with the operator's config layered on top.

    Brokers are files so they can be contributed: a format in one person's
    `config.yaml` is a pull request against an example block CI never runs. As
    files they ship with the app and are tested like any other template, and a
    config entry still overrides one by name.

    Returns plain dicts rather than `BrokerFormat` models — the settings layer
    validates them, and duplicating that here would be two places to keep in
    step.
    """
    formats: dict[str, dict] = {}
    for directory in (BUILTIN_DIR / "brokers", extra_dir):
        if directory is None or not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.yaml")):
            try:
                data = _yaml().load(path.read_text()) or {}
                if not isinstance(data, dict) or not data.get("kind"):
                    raise TemplateError("a broker format needs a `kind`")
            except Exception as exc:  # noqa: BLE001 - yaml errors vary by cause
                log.warning("ignoring broker format %s: %s", path.name, exc)
                continue
            # `name:` is documentation for the file, not part of the format.
            data.pop("name", None)
            formats[path.stem] = data
    # Config wins, per broker, so a local tweak to one shipped format does not
    # mean re-declaring the others.
    for key, value in (overrides or {}).items():
        formats[key] = value.model_dump() if hasattr(value, "model_dump") else dict(value)
    return formats


def pick(templates: list[StatementTemplate], text: str,
         wanted: str | None = None) -> StatementTemplate | None:
    """The template to read this document with.

    **A named one wins outright**, matching or not. Being told "read it with
    this" and then refusing because the markers disagree is the app
    second-guessing a choice it was asked to make — and guessing was the whole
    problem: an installed template with no `match:` block could never beat the
    generic fallback, so somebody's own template silently never ran.

    Otherwise, ordered by how much evidence a template demands, most first: a
    row shape is the strongest signal, then a text marker, then nothing at all.
    **At equal evidence an installed template beats a shipped one**, because a
    template somebody put on this machine says more about their statements than
    one that came in the box.
    """
    if wanted:
        for template in templates:
            if template.key == wanted:
                return template

    def specificity(t: StatementTemplate) -> int:
        return -((2 if t.rows is not None else 0)
                 + (1 if t.match_any else 0)
                 + (0.5 if t.source == "installed" else 0))

    for template in sorted(templates, key=specificity):
        if template.matches(text):
            return template
    return None


# The fields the designer asks about, in the order it asks. Same names the
# statement preview form uses, so a template built here populates that form.
DESIGNABLE_FIELDS = [
    ("payment_date", "Payment date", "date"),
    ("net_amount", "Net amount paid", "money"),
    ("franked_amount", "Franked amount", "money"),
    ("franking_credits", "Franking credits", "money"),
    ("drp_units", "Units allotted (DRP)", "integer"),
    ("drp_price", "Allotment price (DRP)", "money"),
]

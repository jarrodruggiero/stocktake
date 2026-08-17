"""Dividend-statement PDF reader.

Parses registry dividend/distribution statements (Computershare, Link/MUFG,
Vanguard AMMA-style layouts) with pdfplumber — ENTIRELY locally, no cloud
OCR/LLM, so statements never leave the cluster. Extraction is best-effort by
design: the preview form shows every parsed field for correction before
anything is committed, so an unrecognised layout costs a minute of typing,
not a bad row.

**Layouts are data, not code.** What to look for lives in template files under
app/formats/statements/, read by app/docformats.py — adding a registry needs
no Python. That is deliberate: nobody can write a parser for a document they do
not have, so the person holding the statement has to be able to author the
format. The shipped templates say everything a hand-written parser said — the
tests here are the same ones either way, which is what proves it.

Captured per statement: instrument, payment date, net cash amount, franked
amount + franking credits (feeds the v2 tax work), and DRP reinvestment
(units + price) when present → committed as a Dividend row, plus a linked
DRP Trade when units were allotted.
"""

from __future__ import annotations

import datetime as dt
import io
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import docformats, ocr
from .models import Dividend, Instrument


@dataclass
class StatementRow:
    """One fund's line on a combined DRP advice."""

    ticker: str
    cash_amount: str  # the distribution for the period ("Reinvestment Amount")
    drp_price: str
    drp_units: str  # "0" = nothing allotted, cash carried forward
    units_held: str  # per the statement — sanity-check against the DB
    db_units: str | None = None
    carry_note: str = ""
    status: str = "new"  # new / duplicate / unknown-instrument


@dataclass
class ParsedStatement:
    text_sample: str
    # Which template read this document, and its full extracted text. Both are
    # for the person looking at a field that came back empty: without them the
    # only feedback is a blank box, and there is no way to tell a layout this
    # app has never seen from a label it looked for and did not find.
    template_key: str | None = None
    template_name: str | None = None
    full_text: str = ""
    ticker: str | None = None
    payment_date: dt.date | None = None
    net_amount: str | None = None
    franked_amount: str | None = None
    franking_credits: str | None = None
    drp_units: str | None = None
    drp_price: str | None = None
    rows: list[StatementRow] | None = None  # combined multi-fund advice
    # This document was read from pixels, not from a text layer. Carried all
    # the way to the preview because OCR misreads digits and these are money —
    # the person checking the form needs to know to check it harder.
    used_ocr: bool = False
    # Why nothing came out, when nothing came out. "Statement not recognised"
    # is true and useless — it sends the reader to their templates when the
    # answer is that the PDF has no text in it.
    problem: str = ""


# A text layer this short is page furniture — a producer string, a page number,
# a stray header — not a statement. The number sits above what a scan wrapper
# typically carries and well below the shortest real advice; there is a
# parametrised test pinning both edges.
SCANNED_UNDER_CHARS = 60


def looks_scanned(text: str) -> bool:
    """Whether this PDF's text layer is worth nothing.

    Not `if not text`: a scan is usually wrapped in a PDF that carries a few
    characters of metadata, so "not empty" is a long way from "has the
    statement in it".
    """
    return len(" ".join(text.split())) < SCANNED_UNDER_CHARS


@dataclass
class Extracted:
    text: str
    used_ocr: bool = False
    problem: str = ""


def _pdf_text(data: bytes) -> str:
    # Imported here, not at module scope: uploading a statement is a rare,
    # deliberate act, and this module is imported at startup by main.py.
    # pdfplumber costs ~8 MiB in the container — small next to yfinance, but it
    # is the same principle, and this is the only function that touches it.
    import pdfplumber

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def extract(data: bytes, *, ocr_enabled: bool = True) -> Extracted:
    """The document's text, from its text layer or — failing that — from OCR.

    OCR is the fallback and never the first choice: running it on a PDF that
    already has a text layer is slower and strictly worse, because it
    re-derives from pixels what the file already states exactly.

    Never raises. Every way this can fail produces a sentence for the person
    who uploaded the file, because all of them are things about *their file*
    rather than faults in the app.
    """
    text = _pdf_text(data)
    if not looks_scanned(text):
        return Extracted(text=text)

    if not ocr_enabled:
        return Extracted(text="", problem=(
            "This PDF has no text layer, so it is probably a scan. Reading "
            "scans is turned off on this install (imports.ocr.enabled)."))
    if not ocr.available():
        return Extracted(text="", problem=(
            "This PDF has no text layer, so it is probably a scan. Reading one "
            "needs OCR, and the tesseract program is not installed here. "
            "Install tesseract-ocr, or enter the figures by hand below."))
    try:
        scanned = ocr.read_pdf(data)
    except ocr.OcrFailed as exc:
        return Extracted(text="", problem=(
            f"This PDF has no text layer and OCR could not read it: {exc}"))
    # A DIFFERENT question from `looks_scanned`, deliberately. That one asks
    # whether a text layer is worth using when an exact one might exist; this
    # one asks whether OCR found anything at all, and there is no better
    # alternative to fall back to. Holding OCR output to the same length bar
    # would throw away a terse but perfectly good read.
    if not scanned.strip():
        return Extracted(text="", problem=(
            "This PDF has no text layer, and OCR found no words in it. Check "
            "it is the right file, and that the scan is the right way up."))
    return Extracted(text=scanned, used_ocr=True)


def extract_text(data: bytes) -> str:
    """The text alone. Kept because it is the simple question, and because the
    statement designer and several tests ask exactly it."""
    return extract(data).text


def _rows_from(
    values: dict, payment_date: dt.date | None, session: Session
) -> list[StatementRow] | None:
    """Turn a template's extracted rows into the multi-fund preview.

    Only templates that declare a `rows:` shape produce these; a single-payment
    statement returns None and the caller falls back to the one-off form.
    """
    matches = values.get("rows") or []
    if not matches:
        return None
    from decimal import Decimal

    from .models import Trade

    rows = []
    for row_values in matches:
        ticker = row_values["ticker"]
        price = row_values["drp_price"]
        held = row_values["units_held"]
        amount = row_values["amount"]
        brought = row_values["brought_forward"]
        allotted = row_values["allotted"]
        carried = row_values["carried_forward"]
        row = StatementRow(
            ticker=ticker,
            cash_amount=amount.replace(",", ""),
            drp_price=price,
            drp_units=allotted,
            units_held=held.replace(",", ""),
            carry_note=f"DRP carry: brought fwd {brought}, carried fwd {carried}",
        )
        inst = session.scalars(select(Instrument).where(Instrument.ticker == ticker)).first()
        if inst is None:
            row.status = "unknown-instrument"
        else:
            trades = session.scalars(select(Trade).where(Trade.instrument_id == inst.id)).all()
            units = sum(t.quantity for t in trades if t.type in ("buy", "drp")) - sum(
                t.quantity for t in trades if t.type == "sell"
            )
            row.db_units = f"{units.normalize():f}" if trades else "0"
            if payment_date and duplicate_of(session, inst, payment_date, Decimal(row.cash_amount)):
                row.status = "duplicate"
        rows.append(row)
    return rows


def parse_statement(data: bytes, session: Session,
                    templates_dir: Path | None = None,
                    *, ocr_enabled: bool = True,
                    wanted_template: str | None = None) -> ParsedStatement:
    read = extract(data, ocr_enabled=ocr_enabled)
    text = read.text
    parsed = ParsedStatement(text_sample="\n".join(text.splitlines()[:25]),
                             full_text=text, used_ocr=read.used_ocr,
                             problem=read.problem)

    # Instrument: match against what we track — ticker first, then name words.
    instruments = session.scalars(select(Instrument)).all()
    upper = f" {text.upper()} "
    for inst in instruments:
        if re.search(rf"\b{re.escape(inst.ticker)}\b", upper):
            parsed.ticker = inst.ticker
            break
    if parsed.ticker is None:
        for inst in instruments:
            if inst.name and inst.name.upper() in upper:
                parsed.ticker = inst.ticker
                break

    # Which layout this is, and what it says, both come from a template file
    # rather than from patterns in this module — see app/docformats.py for why
    # formats have to be data if anyone but the author is to add one.
    # `wanted_template` is somebody choosing on the upload form. Guessing is
    # the fallback, not the only option — an installed template with no marker
    # could never beat the generic fallback, so a template you built for your
    # own statement silently never ran.
    template = docformats.pick(
        docformats.load_statement_templates(templates_dir), text,
        wanted=wanted_template)
    if template is None:  # no templates installed at all
        return parsed
    parsed.template_key = template.key
    parsed.template_name = template.name
    values = template.extract(text)

    parsed.payment_date = values.get("payment_date")
    parsed.rows = _rows_from(values, parsed.payment_date, session)
    if parsed.rows:
        return parsed  # combined advice: per-row preview supersedes the single form
    # The preview form is text inputs, so values are handed over as the strings
    # they will be edited as. `_text` keeps the trailing zeros a Decimal would
    # otherwise drop — "104.70" must not become "104.7" in a money field.
    parsed.net_amount = _text(values.get("net_amount"))
    parsed.franked_amount = _text(values.get("franked_amount"))
    parsed.franking_credits = _text(values.get("franking_credits"))
    parsed.drp_units = _text(values.get("drp_units"))
    parsed.drp_price = _text(values.get("drp_price"))
    return parsed


def _text(value) -> str | None:
    return None if value is None else str(value)


def duplicate_of(session: Session, instrument: Instrument, date: dt.date, amount) -> Dividend | None:
    return session.scalars(
        select(Dividend).where(
            Dividend.instrument_id == instrument.id,
            Dividend.date == date,
            Dividend.cash_amount == amount,
        )
    ).first()

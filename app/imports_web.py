"""/imports-exports — upload broker CSVs and dividend-statement PDFs.

Both flows are two-step: parse → human-reviewed preview → commit. CSV previews
are staged as JSON under /tmp (pod-local, minutes-lived); the statement preview
is an editable form that carries its own values, so imperfect PDF parsing is
corrected in the browser rather than trusted blindly.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import tempfile
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import APIRouter, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from sqlalchemy import select

from . import (
    auth,
    brokercsv,
    brokerdesign,
    contribute,
    docformats,
    exports,
    fyreport,
    navigation,
    ofx,
    pagemap,
    statements,
    tenancy,
)
from .models import Dividend, Instrument, Trade

log = logging.getLogger(__name__)
router = APIRouter()


def _require_write(ctx) -> None:
    """Viewers can read a shared portfolio but not import into it."""
    if not ctx.can_write:
        raise HTTPException(403, "You have read-only access to this portfolio")

STAGING = Path(tempfile.gettempdir()) / "portfolio-staged"
_CHUNK = 64 * 1024


async def _read_capped(upload: UploadFile, settings) -> bytes:
    """Read an upload, refusing anything past the configured cap.

    Two checks, because neither alone is enough. The Content-Length header
    rejects an oversized upload without reading it, but a client can omit it or
    lie; reading in chunks and stopping is what actually bounds the memory
    used. Note the body has already been received by the time a handler runs —
    the cap bounds what THIS process holds, not what a client can send, which
    is a job for the reverse proxy in front.
    """
    limit = settings.imports.max_upload_mb * 1024 * 1024
    chunks: list[bytes] = []
    total = 0
    while chunk := await upload.read(_CHUNK):
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                413,
                f"That file is larger than the {settings.imports.max_upload_mb} MB "
                f"limit. Raise imports.max_upload_mb if you genuinely need to "
                f"import something this size.",
            )
        chunks.append(chunk)
    return b"".join(chunks)
_UID = re.compile(r"^[0-9a-f-]{36}$")


def _export_instruments(session):
    """Instruments this portfolio has actually traded — the export filter list."""
    held = set(session.scalars(select(Trade.instrument_id).distinct()).all())
    return [
        i
        for i in session.scalars(
            select(Instrument).order_by(Instrument.asset_class, Instrument.ticker)
        )
        if i.id in held
    ]


def _ctx(request: Request):
    templates = request.app.state.templates
    settings = request.app.state.settings
    session_factory = request.app.state.session_factory
    return templates, settings, session_factory


@router.get("/imports-exports", response_class=HTMLResponse)
def imports_page(request: Request):
    templates, settings, _ = _ctx(request)
    with auth.scoped_session(request) as (ctx, _s):
        return templates.TemplateResponse(
            request,
            "imports.html",
            {"auth": ctx, "nav": navigation.nav_for(ctx, settings), "active_nav": "imports",
             "brokers": sorted(settings.imports.brokers_available()), "reports": exports.TITLES,
             "fys": fyreport.available_fys(_s), "instruments": _export_instruments(_s),
             "installed": _installed(settings),
             # Every template that could read a statement, so the upload form
             # can offer them by name instead of always guessing.
             "statement_templates": sorted(
                 docformats.load_statement_templates(
                     settings.imports.user_dir("statement")),
                 key=lambda x: (x.source != "installed", x.name)),
             "installed_error": request.query_params.get("error"),
             "installed_ok": (request.query_params.get("installed")
                              or request.query_params.get("removed"))},
        )


def _came_from(request: Request, form=None) -> tuple[str, str]:
    """Where the back button goes, and what it says.

    These pages are reached by POSTing a file, so the origin travels in a
    hidden field rather than a query string. Validated the same way a
    `?return=` is — it ends up in an href.
    """
    where = None
    if form is not None:
        where = form.get("return")
    where = where or request.query_params.get("return")
    path = navigation.safe_path(str(where) if where else None, "/imports-exports")
    return path, navigation.back_label(path, "Imports")


@router.post("/imports-exports/csv", response_class=HTMLResponse)
async def csv_preview(request: Request, file: UploadFile, broker: str = Form(...)):
    templates, settings, session_factory = _ctx(request)
    text = (await _read_capped(file, settings)).decode("utf-8-sig", errors="replace")
    # OFX is broker-agnostic — the file says which securities it is about, so
    # there is nothing to configure and no format to pick. It shares the whole
    # preview-and-commit path from here on.
    if broker == "ofx":
        result = ofx.parse_ofx(text)
    else:
        fmt = settings.imports.brokers_available().get(broker)
        if fmt is None:
            raise HTTPException(400, f"unknown broker {broker!r}")
        result = brokercsv.parse_csv(text, broker, fmt)
    with auth.scoped_session(request) as (ctx, s):
        await auth.verify_csrf(request, s)
        _require_write(ctx)
        brokercsv.annotate(s, result.candidates, settings.imports)

    uid = None
    if result.candidates and not result.errors:
        STAGING.mkdir(mode=0o700, exist_ok=True)
        uid = str(uuid.uuid4())
        # The staged file records who uploaded it: commit refuses to import one
        # person's CSV into another's portfolio.
        (STAGING / f"{uid}.json").write_text(
            json.dumps(
                {
                    "broker": broker,
                    "filename": file.filename,
                    "user_id": ctx.user.id,
                    "rows": [c.as_dict() for c in result.candidates],
                }
            )
        )
    return templates.TemplateResponse(
        request,
        "csv_preview.html",
        {
            "auth": ctx,
            "nav": navigation.nav_for(ctx, settings),
            "active_nav": "imports",
            "broker": broker,
            "filename": file.filename,
            "return_to": _came_from(request)[0],
            "back_label": _came_from(request)[1],
            "candidates": result.candidates,
            "skipped": result.skipped,
            "errors": result.errors,
            "uid": uid,
            "insertable": sum(1 for c in result.candidates if c.status != "duplicate"),
            "unresolved": brokercsv.unresolved(result.candidates),
        },
    )


@router.post("/imports-exports/csv/{uid}/resolve", response_class=HTMLResponse)
async def csv_resolve(request: Request, uid: str):
    """Apply corrected tickers and exchanges, then re-check against the database.

    The same preview page comes back, so a correction that resolves to an
    instrument already held reads as such before anything is written — which is
    the point when one ticker exists on two exchanges.
    """
    templates, settings, _ = _ctx(request)
    if not _UID.match(uid):
        raise HTTPException(400, "bad staging id")
    staged_path = STAGING / f"{uid}.json"
    if not staged_path.is_file():
        raise HTTPException(404, "that preview has expired — upload the file again")
    staged = json.loads(staged_path.read_text())
    candidates = [brokercsv.CandidateTrade.from_dict(d) for d in staged["rows"]]

    with auth.scoped_session(request) as (ctx, s):
        await auth.verify_csrf(request, s)
        _require_write(ctx)
        if staged.get("user_id") != ctx.user.id:
            raise HTTPException(403, "that staged upload belongs to another account")

        form = await request.form()
        moves: dict[tuple[str, str], tuple[str, str]] = {}
        for key in form:
            if not key.startswith("ticker__"):
                continue
            was_ticker, _, was_exchange = key[len("ticker__"):].partition("__")
            new_ticker = str(form.get(key, "")).strip().upper()
            new_exchange = str(
                form.get(f"exchange__{was_ticker}__{was_exchange}", "")).strip().upper()
            if new_ticker and new_exchange:
                moves[(was_ticker, was_exchange)] = (new_ticker, new_exchange)
        brokercsv.relabel(candidates, moves)
        brokercsv.annotate(s, candidates, settings.imports)
        staged["rows"] = [c.as_dict() for c in candidates]
        staged_path.write_text(json.dumps(staged))

        return templates.TemplateResponse(
            request,
            "csv_preview.html",
            {
                "auth": ctx,
                "nav": navigation.nav_for(ctx, settings),
                "active_nav": "imports",
                "broker": staged["broker"],
                "filename": staged.get("filename", ""),
                "return_to": _came_from(request)[0],
                "back_label": _came_from(request)[1],
                "candidates": candidates,
                "skipped": [],
                "errors": [],
                "uid": uid,
                "insertable": sum(1 for c in candidates if c.status != "duplicate"),
                "unresolved": brokercsv.unresolved(candidates),
                "rechecked": True,
            },
        )


@router.post("/imports-exports/csv/{uid}/commit")
async def csv_commit(request: Request, uid: str):
    _, settings, _ = _ctx(request)
    if not _UID.match(uid):
        raise HTTPException(400, "bad staging id")
    staged_path = STAGING / f"{uid}.json"
    if not staged_path.exists():
        raise HTTPException(410, "staged upload expired — re-upload the CSV")
    staged = json.loads(staged_path.read_text())
    candidates = [brokercsv.CandidateTrade.from_dict(d) for d in staged["rows"]]
    with auth.scoped_session(request) as (ctx, s):
        await auth.verify_csrf(request, s)
        _require_write(ctx)
        if staged.get("user_id") != ctx.user.id:
            raise HTTPException(403, "that staged upload belongs to another account")
        # Re-annotate: the DB may have changed since preview.
        brokercsv.annotate(s, candidates, settings.imports)
        blocked = [c for c in candidates if c.status == "unknown-instrument"]
        if blocked and not settings.imports.allow_new_instruments:
            raise HTTPException(409, f"unknown instruments: {sorted({c.ticker for c in blocked})}")
        counts = brokercsv.commit(s, candidates, settings.imports, staged["broker"])
    staged_path.unlink(missing_ok=True)
    return RedirectResponse(
        f"/imports-exports?done=csv&inserted={counts['inserted']}&skipped={counts['duplicates_skipped']}",
        status_code=303,
    )


@router.post("/imports-exports/statement", response_class=HTMLResponse)
async def statement_preview(request: Request, file: UploadFile,
                            template: str = Form("")):
    templates, settings, _ = _ctx(request)
    data = await _read_capped(file, settings)
    with auth.scoped_session(request) as (ctx, s):
        await auth.verify_csrf(request, s)
        _require_write(ctx)
        parsed = statements.parse_statement(
            data, s, settings.imports.user_dir("statement"),
            ocr_enabled=settings.imports.ocr.enabled,
            wanted_template=template or None)
        tickers = s.scalars(select(Instrument.ticker).order_by(Instrument.ticker)).all()
        template = "statement_rows_preview.html" if parsed.rows else "statement_preview.html"
        return templates.TemplateResponse(
            request,
            template,
            {
                "auth": ctx,
                "nav": navigation.nav_for(ctx, settings),
                "active_nav": "imports",
                "p": parsed,
                "filename": file.filename,
                "tickers": tickers,
            },
        )


@router.post("/imports-exports/statement/commit")
async def statement_commit(
    request: Request,
    ticker: str = Form(...),
    payment_date: str = Form(...),
    net_amount: str = Form(...),
    franked_amount: str = Form(""),
    franking_credits: str = Form(""),
    drp_units: str = Form(""),
    drp_price: str = Form(""),
    note_extra: str = Form(""),
):
    _, _, session_factory = _ctx(request)

    def _dec(raw: str) -> Decimal | None:
        raw = raw.strip().replace(",", "").replace("$", "")
        if not raw:
            return None
        try:
            return Decimal(raw)
        except InvalidOperation:
            raise HTTPException(400, f"not a number: {raw!r}")

    try:
        date = dt.date.fromisoformat(payment_date)
    except ValueError:
        raise HTTPException(400, f"bad payment date {payment_date!r} (use YYYY-MM-DD)")
    amount = _dec(net_amount)
    if amount is None:
        raise HTTPException(400, "net amount is required")

    with auth.scoped_session(request) as (ctx, s):
        await auth.verify_csrf(request, s)
        _require_write(ctx)
        inst = s.scalars(select(Instrument).where(Instrument.ticker == ticker.upper())).first()
        if inst is None:
            raise HTTPException(400, f"unknown instrument {ticker!r}")
        if statements.duplicate_of(s, inst, date, amount):
            raise HTTPException(409, f"a {ticker} dividend of {amount} on {date} already exists")

        note = "from dividend statement"
        if franked_amount.strip():
            note += f"; franked amount {_dec(franked_amount)}"
        if note_extra.strip():
            note += f"; {note_extra.strip()}"

        units, price = _dec(drp_units), _dec(drp_price)
        reinvest = None
        if units and price:
            reinvest = tenancy.owned(
                s,
                Trade(
                    instrument=inst,
                    date=date,
                    type="drp",
                    quantity=units,
                    unit_price=price,
                    brokerage=Decimal(0),
                    fx_rate=Decimal(1) if inst.currency == "AUD" else None,
                    note=note,
                ),
            )
            s.add(reinvest)
        s.add(
            tenancy.owned(
                s,
                Dividend(
                    instrument=inst,
                    date=date,
                    cash_amount=amount,
                    fx_rate=Decimal(1) if inst.currency == "AUD" else None,
                    franking_credits=_dec(franking_credits),
                    reinvest_trade=reinvest,
                    note=note,
                ),
            )
        )
    return RedirectResponse(f"/holding/{ticker.upper()}", status_code=303)


# --------------------------------------------------------------------------- #
# The statement template designer. The feature most likely to drift, and the
# three principles holding it straight — the document never leaves the machine,
# inference lives in `docformats` where it can be tested, and a template says
# where to look and never what was found: decisions.md #37.
# --------------------------------------------------------------------------- #

@router.post("/imports-exports/statement/design", response_class=HTMLResponse)
async def statement_designer(request: Request, text: str = Form(...)):
    templates, settings, _factory = _ctx(request)
    with auth.scoped_session(request) as (ctx, s):
        await auth.verify_csrf(request, s)
        _require_write(ctx)
        return templates.TemplateResponse(
            request,
            "statement_designer.html",
            {
                "auth": ctx,
                "nav": navigation.nav_for(ctx, settings),
                "active_nav": "imports",
                "text": text,
                "tokens": docformats.tokenize(text),
                "fields": docformats.DESIGNABLE_FIELDS,
            },
        )


@router.post("/imports-exports/statement/design/infer")
async def statement_designer_infer(
    request: Request, text: str = Form(...), index: int = Form(...),
    expect: str = Form(""),
):
    """What the app makes of the value somebody pointed at."""
    with auth.scoped_session(request) as (ctx, s):
        await auth.verify_csrf(request, s)
        _require_write(ctx)
        found = docformats.infer_field(text, index, expect or None)
        return {
            "type": found.type,
            "labels": found.labels,
            "value": None if found.value is None else str(found.value),
            "problem": found.problem,
            "warning": found.warning,
            # Where the value STARTS and how many words it covers, so the
            # interface can highlight all of a written date rather than the one
            # word that happened to be clicked.
            "starts_at": found.starts_at,
            "span": found.span,
            "needs_label": found.needs_label,
            "mismatch": found.mismatch,
        }


@router.post("/imports-exports/statement/design/check")
async def statement_designer_check(
    request: Request, text: str = Form(...), label: str = Form(...),
    type: str = Form("text"),
):
    """What a hand-typed label reads out of this document.

    The other half of not refusing a mapping: a label somebody types is only
    worth something if it finds the value, and the only way to know is to run
    it against the document it came from.
    """
    with auth.scoped_session(request) as (ctx, s):
        await auth.verify_csrf(request, s)
        _require_write(ctx)
        reading = docformats.read_with(text, label, type)
        return {"value": None if reading.value is None else str(reading.value),
                "problem": reading.problem}


@router.post("/imports-exports/statement/design/export")
async def statement_designer_export(
    request: Request,
    name: str = Form(...),
    mapping: str = Form(...),
    marker: str = Form(""),
):
    """The finished template, as a downloadable file."""
    with auth.scoped_session(request) as (ctx, s):
        await auth.verify_csrf(request, s)
        _require_write(ctx)
        try:
            fields = json.loads(mapping)
        except ValueError:
            raise HTTPException(400, "could not read the mapping")
        body = docformats.template_yaml(
            name=name.strip() or "My registry",
            fields=fields,
            match=[m.strip() for m in marker.splitlines() if m.strip()],
        )
        slug = _slug(name) or "template"
        return PlainTextResponse(
            body,
            headers={"content-disposition": f'attachment; filename="{slug}.yaml"'},
        )


def _designed_yaml(name: str, mapping: str, marker: str) -> str:
    """The designer's form fields as a template file.

    Shared by download, install and contribute so the three cannot disagree
    about what was designed.
    """
    try:
        fields = json.loads(mapping)
    except ValueError:
        raise HTTPException(400, "could not read the mapping") from None
    return docformats.template_yaml(
        name=name.strip() or "My registry",
        fields=fields,
        match=[m.strip() for m in marker.splitlines() if m.strip()],
    )


@router.post("/imports-exports/statement/design/install")
async def statement_designer_install(
    request: Request, name: str = Form(...), mapping: str = Form(...),
    marker: str = Form(""),
):
    """Save what was just designed, so the next upload uses it.

    T26d's second half. The designer only ever offered a download, which meant
    a template you built could not be USED without putting a file on the
    container's disk yourself — and on Kubernetes the obvious place is a
    read-only ConfigMap. This hands the same bytes to the install route rather
    than growing a second copy of the containment check.
    """
    _, settings, _ = _ctx(request)
    with auth.scoped_session(request) as (ctx, s):
        await auth.verify_csrf(request, s)
        _require_admin(ctx)
        body = _designed_yaml(name, mapping, marker)
        slug = _slug(name)
        problem = _validate("statement", body.encode())
        if problem:
            return RedirectResponse("/imports-exports?error=" + quote_plus(problem),
                                    status_code=303)
        path = _template_path(settings, "statement", slug)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        log.info("installed designed statement template %s by user %d", slug, ctx.user.id)
        return RedirectResponse("/imports-exports?installed=" + quote_plus(slug), status_code=303)


@router.post("/imports-exports/statement/design/contribute")
async def statement_designer_contribute(
    request: Request, name: str = Form(...), mapping: str = Form(...),
    marker: str = Form(""), text: str = Form(""),
):
    """The three files a pull request needs, as a zip.

    A template alone is not mergeable: `tests/test_format_samples.py` wants a
    redacted sample and its expected values too, because nobody here holds an
    account at most registries and a format has to be checkable by CI forever.

    The redaction is deliberately not claimed to be complete — see
    `app/contribute.py`. The bundle carries a header saying so, and the page
    that offers this says it in the one place somebody will read it.
    """
    with auth.scoped_session(request) as (ctx, s):
        await auth.verify_csrf(request, s)
        _require_write(ctx)
        body = _designed_yaml(name, mapping, marker)
        redacted, _notes = contribute.redact(text)
        slug = _slug(name) or "my-registry"
        data = contribute.bundle(slug, body, redacted.text)
        return Response(
            data,
            media_type="application/zip",
            headers={"content-disposition":
                     f'attachment; filename="{slug}-contribution.zip"'},
        )


@router.post("/imports-exports/statement/visual", response_class=HTMLResponse)
async def visual_designer(request: Request, file: UploadFile):
    """Map a statement by pointing at the page rather than at a word list.

    The text designer shows the extracted words; for anything that came through
    OCR that reads as a jumble, and somebody who cannot see *where* a number sat
    cannot be sure they mapped the right one. This renders the pages and puts
    the boxes back where they belong.

    Clicking a box sends the same token index the text designer sends, to the
    same `/design/infer`. The inference is not duplicated — only the way you
    point at it.
    """
    templates, settings, _factory = _ctx(request)
    data = await _read_capped(file, settings)
    with auth.scoped_session(request) as (ctx, s):
        await auth.verify_csrf(request, s)
        _require_write(ctx)
        try:
            token = pagemap.create(settings, user_id=ctx.user.id, data=data)
        except Exception as exc:
            log.info("visual mapper could not render %s: %s", file.filename, exc)
            return RedirectResponse("/imports-exports?error=" + quote_plus(
                "That PDF could not be rendered for the visual mapper. The text "
                "designer still works on it."), status_code=303)
        session = pagemap.load(settings, token, user_id=ctx.user.id)
        return templates.TemplateResponse(
            request, "statement_visual.html",
            _visual_context(request, ctx, settings, token, session,
                            filename=file.filename))


@router.post("/imports-exports/statement/visual/edit", response_class=HTMLResponse)
async def visual_designer_edit(request: Request, token: str = Form(...),
                               template: str = Form("")):
    """The same page, with an existing template's mapping already applied.

    **Any template, including the ones that ship.** A correction to a shipped
    format is the most valuable kind of contribution — the person who found it
    is the person holding the statement it gets wrong — and telling them to
    write YAML instead is how that correction never arrives.
    """
    templates, settings, _factory = _ctx(request)
    with auth.scoped_session(request) as (ctx, s):
        await auth.verify_csrf(request, s)
        _require_write(ctx)
        session = pagemap.load(settings, token, user_id=ctx.user.id)
        if session is None:
            return RedirectResponse("/imports-exports?error=" + quote_plus(
                "That mapping session has expired. Upload the statement again."),
                status_code=303)
        return templates.TemplateResponse(
            request, "statement_visual.html",
            _visual_context(request, ctx, settings, token, session,
                            filename="", editing=template))


def _visual_context(request, ctx, settings, token, session, *, filename,
                    editing: str = ""):
    """Everything the visual mapper page needs, built once for both the upload
    and the edit-an-existing-template routes."""
    available = docformats.load_statement_templates(
        settings.imports.user_dir("statement"))
    chosen = next((t for t in available if t.key == editing), None)
    return {
        "auth": ctx,
        "nav": navigation.nav_for(ctx, settings),
        "active_nav": "imports",
        "token": token,
        "pages": session.pages,
        "text": session.text,
        "filename": filename,
        "fields": docformats.DESIGNABLE_FIELDS,
        "idle_minutes": pagemap.IDLE_MINUTES,
        "templates": sorted(available, key=lambda t: (t.source != "installed", t.name)),
        "editing": chosen,
        "editing_name": chosen.name if chosen else "",
        "editing_markers": "\n".join(chosen.match_any) if chosen else "",
        # The mapping as the page's JavaScript holds it, so opening a template
        # shows where every field currently points rather than a blank slate.
        "editing_mapping": json.dumps(
            docformats.mapping_of(chosen) if chosen else {}),
    }


@router.get("/imports-exports/statement/visual/{token}/page/{index}.png")
async def visual_page(request: Request, token: str, index: int):
    """One rendered page.

    Served through the app rather than from a static mount on purpose: these are
    pictures of somebody's dividend statement, and the ownership check is the
    point. A static mount would make the token the only thing standing between
    a URL and the file.
    """
    _, settings, _ = _ctx(request)
    with auth.scoped_session(request) as (ctx, _s):
        png = pagemap.page_png(settings, token, user_id=ctx.user.id, index=index)
    if png is None:
        raise HTTPException(404, "no such page")
    return Response(png, media_type="image/png",
                    headers={"cache-control": "private, max-age=600"})


# --------------------------------------------------------------------------- #
# The broker CSV column mapper. Simpler than the statement designer — a CSV's
# fields are already delimited, so the only question is which column is which.
# The guessing lives in `app/brokerdesign.py`, and the file is never stored:
# decisions.md #37.
# --------------------------------------------------------------------------- #

@router.post("/imports-exports/broker/design", response_class=HTMLResponse)
async def broker_designer(request: Request, file: UploadFile = None,
                          text: str = Form(""), name: str = Form(""),
                          exchange: str = Form("ASX"), currency: str = Form("AUD"),
                          date_format: str = Form(""),
                          custom_date: str = Form(""),
                          date_format_custom: str = Form("")):
    """Upload a CSV and say which column is which.

    One route for both passes: the first arrives with a file and nothing else
    and gets the guessed mapping; the second arrives with the text and the
    chosen columns and gets a preview of the trades they would produce. A single
    handler because they render the same page, and two would drift.
    """
    templates, settings, _factory = _ctx(request)
    if file is not None and file.filename:
        text = (await _read_capped(file, settings)).decode("utf-8-sig", errors="replace")

    with auth.scoped_session(request) as (ctx, s):
        await auth.verify_csrf(request, s)
        _require_write(ctx)

        read = brokerdesign.read_csv(text)
        # The mapping arrives as one form field per field (`col_date`, ...),
        # not as a JSON blob: named selects submit on their own, so the page
        # works with JavaScript off. brokermap.js only refreshes the sample
        # values shown beside each dropdown.
        form = await request.form()
        chosen = {f: (form.get(f"col_{f}") or "").strip() or None
                  for f in brokerdesign.FIELDS}
        # This broker's word for a trade type -> ours, one field per word.
        actions = {}
        for key in form:
            if key.startswith("act_"):
                kind = str(form.get(key, "")).strip()
                if kind in ("buy", "sell", "drp"):
                    actions[key[len("act_"):]] = kind
        first_pass = not any(chosen.values())
        if first_pass:
            chosen = brokerdesign.guess(read.headers)
        # Only a column that is actually in this file. A stale form, or a hand
        # posted field, must not reach the parser naming a column that is not
        # there — it would be reported as a mapping error rather than refused.
        chosen = {k: (v if v in read.headers else None) for k, v in chosen.items()}

        dates = [r.get(chosen.get("date") or "", "") for r in read.rows]
        # Inference is a convenience, not an authority: a sample can be
        # genuinely undecidable, and the answer to that is a box to type the
        # real format into rather than a cleverer guess. The tick-box wins
        # whenever it is ticked, including over a dropdown left at its default.
        using_custom = bool(custom_date) and bool(date_format_custom.strip())
        if using_custom:
            date_format = date_format_custom.strip()
        elif not date_format:
            date_format = brokerdesign.guess_date_format(dates) or "%d/%m/%Y"

        result = None
        if read.rows and not first_pass:
            result = brokerdesign.preview(text, exchange=exchange, currency=currency,
                                          date_format=date_format, columns=chosen)

        return templates.TemplateResponse(
            request,
            "broker_designer.html",
            {
                "auth": ctx,
                "nav": navigation.nav_for(ctx, settings),
                "active_nav": "imports",
                "text": text,
                "read": read,
                "fields": brokerdesign.FIELDS,
                "required": brokerdesign.REQUIRED,
                "chosen": chosen,
                # NOT defaulted from the filename. A broker export is commonly
                # named after the account holder — "Movements_Jane Smith_..." —
                # and this value is written into the format file that the
                # contribute flow attaches to a public pull request. The field
                # is required, so an empty one asks rather than guesses.
                "name": name,
                "exchange": exchange,
                "currency": currency,
                "date_format": date_format,
                "date_formats": brokerdesign.DATE_FORMATS,
                "using_custom": using_custom,
                "date_format_custom": date_format_custom,
                "return_to": _came_from(request, form)[0],
                "back_label": _came_from(request, form)[1],
                "ambiguous": brokerdesign.dates_are_ambiguous(dates),
                "action_words": brokerdesign.action_words(read.rows, chosen.get("action")),
                "drp_skipped": brokerdesign.drp_skipped(result.skipped) if result else 0,
                "actions": actions,
                "result": result,
                "yaml": brokerdesign.broker_yaml(
                    name=name or "My broker", exchange=exchange, currency=currency,
                    date_format=date_format, columns=chosen, actions=actions),
            },
        )


# --------------------------------------------------------------------------- #
# Installing import templates through the interface. Safe only because a
# template is inert YAML that never reaches `re.compile` — decisions.md #38.
# --------------------------------------------------------------------------- #

KINDS = ("statement", "broker")


def _require_admin(ctx) -> None:
    """A template changes how EVERY user's imports are parsed — it is not
    portfolio-scoped, so installing one is not a per-member decision."""
    if ctx is None or not ctx.is_admin:
        raise HTTPException(403, "Admins only")


def _slug(name: str) -> str:
    """A filename reduced to the shipped convention: lowercase, hyphenated.

    Also the whole path-traversal defence's first half — `../../etc/passwd`
    contains no character that survives this. The second half is
    `_template_path`, which checks the result really is inside the directory,
    because relying on one sanitiser to be perfect is how these go wrong.
    """
    stem = Path(name or "").name.rsplit(".", 1)[0]
    return re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")


def _template_path(settings, kind: str, slug: str) -> Path:
    """Where that template lives, or a 400 if the name tried to leave.

    Resolved and re-checked rather than trusted: `_slug` should make escape
    impossible, and this is what makes it so even if `_slug` is one day
    loosened to allow, say, a dot.
    """
    if kind not in KINDS:
        raise HTTPException(400, "unknown template kind")
    if not slug:
        raise HTTPException(400, "that file needs a name")
    directory = settings.imports.user_dir(kind).resolve()
    path = (directory / f"{slug}.yaml").resolve()
    if directory not in path.parents:
        raise HTTPException(400, "that name is not allowed")
    return path


def _validate(kind: str, body: bytes) -> str | None:
    """Parse it as the thing it claims to be. Returns a problem, or None.

    Validated on the way in rather than on first use: a file that is valid YAML
    but not a valid template would otherwise sit there until somebody uploaded
    a statement, and then fail as "your statement wasn't recognised" — which
    sends them looking in entirely the wrong place.
    """
    import tempfile as _tempfile

    with _tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe.yaml"
        try:
            probe.write_bytes(body)
        except OSError as exc:  # pragma: no cover - a full disk, not a bad file
            return str(exc)
        try:
            if kind == "statement":
                template = docformats.load_statement_template(probe)
                if not template.fields and not template.rows:
                    return ("That template has no fields and no rows, so it "
                            "would never read anything.")
            else:
                loaded = docformats.load_broker_formats(extra_dir=Path(tmp))
                if "probe" not in loaded:
                    return "That is not a broker format — it needs a `kind`."
        except Exception as exc:  # noqa: BLE001 - yaml and template errors vary
            return f"{type(exc).__name__}: {exc}".replace(str(probe), "the file")
    return None


def _installed(settings) -> dict[str, list[str]]:
    """What has been installed through the interface, per kind. Shipped
    templates are deliberately not listed — they cannot be removed here."""
    out: dict[str, list[str]] = {}
    for kind in KINDS:
        directory = settings.imports.user_dir(kind)
        out[kind] = sorted(p.stem for p in directory.glob("*.yaml")) \
            if directory.is_dir() else []
    return out


@router.post("/imports-exports/formats")
async def install_format(request: Request, kind: str = Form("statement"),
                         file: UploadFile = None, body: str = Form(""),
                         filename: str = Form("")):
    """Put a template on this machine so imports start using it.

    Two ways in, one code path. A **file** is somebody installing a template
    another person sent them. A **body** is one of the designers saying "save
    what I just built" — which is the same act, and giving it its own route
    would mean two places to get the containment check right.
    """
    templates, settings, _ = _ctx(request)
    with auth.scoped_session(request) as (ctx, s):
        await auth.verify_csrf(request, s)
        _require_admin(ctx)
        if file is not None and file.filename:
            raw = await _read_capped(file, settings)
            source = file.filename
        elif body.strip():
            # Capped like an upload: it arrives as a form field, so it is no
            # more trusted than a file just because a page of ours produced it.
            raw = body.encode("utf-8")[: settings.imports.max_upload_mb * 1024 * 1024]
            source = filename or "template"
        else:
            return RedirectResponse("/imports-exports?error=" + quote_plus(
                "Choose a template file."), status_code=303)
        slug = _slug(source)
        path = _template_path(settings, kind, slug)

        problem = _validate(kind, raw)
        if problem:
            return RedirectResponse(
                "/imports-exports?error=" + quote_plus(f"{source}: {problem}"),
                status_code=303)
        body_bytes = raw

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body_bytes)
        log.info("installed %s template %s by user %d", kind, slug, ctx.user.id)
        return RedirectResponse(
            "/imports-exports?installed=" + quote_plus(slug), status_code=303)


@router.post("/imports-exports/broker/design/export")
async def broker_designer_export(request: Request, body: str = Form(...),
                                 filename: str = Form("broker")):
    """The format file, to attach to a pull request.

    Named by the same slug rule the shipped formats follow, so it drops into
    `app/formats/brokers/` without being renamed — which is the difference
    between a contribution and a chore.
    """
    with auth.scoped_session(request) as (ctx, s):
        await auth.verify_csrf(request, s)
        _require_write(ctx)
        slug = _slug(filename) or "broker"
        return PlainTextResponse(
            body,
            headers={"content-disposition": f'attachment; filename="{slug}.yaml"'},
        )


@router.get("/imports-exports/formats/{kind}/{slug}.yaml")
def download_format(request: Request, kind: str, slug: str):
    """Hand back an installed template as a file.

    The third verb. Install and remove already existed, so a template could
    arrive and be deleted but never leave — which broke the loop the whole
    contribution flow opens: somebody corrects a shipped layout here and has no
    way to get the corrected file back out to send it on.

    Not admin-only, unlike install and remove: those change how *everyone's*
    imports are read, while this only reads a file that is already on the
    machine and contains no portfolio data — a template is column names and
    labels, never anyone's figures.
    """
    _, settings, _ = _ctx(request)
    with auth.scoped_session(request) as (_ctx_user, _s):
        # Re-slugged for the same reason `remove_format` does it: the slug
        # arrives in a URL, and a decoded `../` would otherwise escape.
        path = _template_path(settings, kind, _slug(slug))
        if not path.is_file():
            raise HTTPException(404, "no such template")
        return Response(
            content=path.read_text(),
            media_type="application/yaml",
            headers={"Content-Disposition":
                     f'attachment; filename="{path.name}"'},
        )


@router.post("/imports-exports/formats/{kind}/{slug}/delete")
async def remove_format(request: Request, kind: str, slug: str):
    _, settings, _ = _ctx(request)
    with auth.scoped_session(request) as (ctx, s):
        await auth.verify_csrf(request, s)
        _require_admin(ctx)
        # Re-slugged, not trusted: the slug arrives in the URL, and a decoded
        # `../victim` would otherwise resolve outside the directory.
        path = _template_path(settings, kind, _slug(slug))
        if path.is_file():
            path.unlink()
            log.info("removed %s template %s by user %d", kind, path.stem,
                     ctx.user.id)
        return RedirectResponse("/imports-exports?removed=" + quote_plus(path.stem),
                                status_code=303)

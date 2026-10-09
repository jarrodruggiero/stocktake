"""Buttons that destroy something look like it, everywhere.

`button.danger` only coloured the label, so a destructive button was the
default purple with red text on it: the look of a primary action. Test users
read it as one. It is now solid red with white text, and every button that
deletes, removes, revokes or unlinks carries the class.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import factories as fac
from test_routes import bind_to_only_portfolio, make_login

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "app" / "templates"
CSS = (ROOT / "app" / "static" / "style.css").read_text()
HTML = {"accept": "text/html"}
DESTROYS = re.compile(r"/(delete|remove|revoke|unlink)\b")


def _luminance(hex_colour: str) -> float:
    channels = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
              for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(a: str, b: str) -> float:
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _theme_blocks() -> dict[str, str]:
    """The light tokens, and the two ways of asking for dark."""
    light = CSS[CSS.index(":root {"):]
    dark_auto = CSS[CSS.index('body[data-theme="auto"] {'):]
    dark = CSS[CSS.index('body[data-theme="dark"] {'):]
    return {name: block[:block.index("}")]
            for name, block in (("light", light), ("auto dark", dark_auto),
                                ("dark", dark))}


@pytest.mark.parametrize("theme", ["light", "auto dark", "dark"])
def test_white_reads_on_the_danger_red_in_every_theme(theme):
    """WCAG AA for body text, 4.5:1. The dark theme's `--down` (#ef4444) is
    only 3.8:1 behind white, which is why the button has its own token."""
    found = re.search(r"--danger:\s*(#[0-9a-fA-F]{6})", _theme_blocks()[theme])
    assert found, f"no --danger colour in the {theme} theme"
    assert _contrast(found.group(1), "#ffffff") >= 4.5, found.group(1)


def test_the_danger_button_is_solid_red_with_white_text():
    rule = re.search(r"\nbutton\.danger\s*\{([^}]*)\}", CSS)
    assert rule, "no button.danger rule"
    assert re.search(r"background:\s*var\(--danger\)", rule.group(1)), rule.group(1)
    assert re.search(r"color:\s*#fff(fff)?\b", rule.group(1)), rule.group(1)


def _forms_that_destroy() -> list[tuple[str, str, str]]:
    """(template, form id or "", the form's markup) for every POST form whose
    action deletes, removes, revokes or unlinks.

    Signing a session out is not on the list though its route says "revoke":
    nothing is lost, and a red button there would be alarm for nothing."""
    found = []
    for path in sorted(TEMPLATES.glob("*.html")):
        text = path.read_text()
        for match in re.finditer(r"<form\b[^>]*>.*?</form>", text, re.S):
            tag = match.group(0)[:match.group(0).index(">") + 1]
            action = re.search(r'action="([^"]*)"', tag)
            if (action and DESTROYS.search(action.group(1))
                    and not action.group(1).startswith("/profile/sessions/")):
                form_id = re.search(r'\bid="([^"]+)"', tag)
                found.append((path.name, form_id.group(1) if form_id else "",
                              match.group(0)))
    return found


def test_there_are_destructive_forms_to_check():
    """An empty sweep proves nothing."""
    assert len(_forms_that_destroy()) >= 8


def test_every_button_that_destroys_something_is_a_danger_button():
    everything = "\n".join(p.read_text() for p in TEMPLATES.glob("*.html"))
    plain = []
    for name, form_id, markup in _forms_that_destroy():
        buttons = re.findall(r"<button\b[^>]*>", markup)
        if form_id:
            # A button outside the form that submits it with `form=`.
            buttons += re.findall(rf'<button\b[^>]*\bform="{form_id}"[^>]*>', everything)
        submits = [b for b in buttons if 'type="button"' not in b]
        assert submits, f"{name}: a destructive form with no button submitting it"
        plain += [(name, b) for b in submits if not re.search(r'class="[^"]*\bdanger\b', b)]
    assert not plain, plain


def test_a_button_is_quiet_or_destructive_not_both():
    """`secondary danger` was a grey button with red text: neither style."""
    both = [(p.name, b) for p in TEMPLATES.glob("*.html")
            for b in re.findall(r'<button\b[^>]*class="[^"]*"[^>]*>', p.read_text())
            if re.search(r'class="[^"]*\bsecondary\b', b)
            and re.search(r'class="[^"]*\bdanger\b', b)]
    assert not both, both


@pytest.fixture
def edit_page(client, session_factory):
    make_login(client, session_factory)
    with session_factory() as s:
        bind_to_only_portfolio(s)
        acme = fac.make_instrument(s, "ACME")
        buy = fac.add_trade(s, acme, "2026-03-02", "buy", 100, "5.00")
        s.commit()
        trade_id = buy.id
    page = client.get(f"/trade/{trade_id}/edit", headers=HTML)
    assert page.status_code == 200
    return trade_id, page.text


def _trade_form(page: str, trade_id: int) -> str:
    match = re.search(rf'<form\b[^>]*action="/trade/{trade_id}/edit".*?</form>', page, re.S)
    assert match, "no edit form on the page"
    return match.group(0)


def test_editing_a_trade_puts_delete_with_save_and_cancel(edit_page):
    """Delete sat in the page header, apart from the card holding the trade.
    It belongs with the form's other buttons; a form cannot hold another
    form, so it submits the delete form from where it sits."""
    trade_id, page = edit_page
    form = _trade_form(page, trade_id)
    delete = re.search(r'<button\b[^>]*\bform="([^"]+)"[^>]*>\s*Delete\s*</button>', form)
    assert delete, "no Delete button inside the trade's form"
    target = re.search(rf'<form\b[^>]*\bid="{delete.group(1)}"[^>]*>', page)
    assert target and f'action="/trade/{trade_id}/delete"' in target.group(0)


def test_cancel_on_the_edit_page_is_a_button(edit_page):
    trade_id, page = edit_page
    form = _trade_form(page, trade_id)
    assert re.search(r'<a href="[^"]+">\s*<button type="button" class="secondary">\s*Cancel',
                     form), "Cancel should look like the dialog's button, not a bare link"

"""A question with a name in it is asked by confirm.js, never by a handler.

An inline handler such as `onclick="return confirm('Disabling {{ u.name }}…')"`
puts the name inside a script, so an apostrophe, as in "Jo's portfolio",
broke the handler and the question silently never appeared. In
`data-confirm` the words are an attribute, and only ever text
(test_confirm_in_the_browser.py checks it in a browser).
"""

from __future__ import annotations

import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"
HANDLER = re.compile(r"\son[a-z]+=\"([^\"]*)\"", re.S)


def _handlers() -> list[tuple[str, str]]:
    return [(path.name, " ".join(value.split()))
            for path in sorted(TEMPLATES.glob("*.html"))
            for value in HANDLER.findall(path.read_text(encoding="utf-8"))]


def test_there_are_handlers_to_check():
    """A sweep that finds nothing proves nothing."""
    assert len(_handlers()) > 5


def test_no_handler_holds_what_a_template_writes():
    assert [(name, value) for name, value in _handlers() if "{{" in value or "{%" in value] == []


def test_every_question_is_asked_by_confirm_js():
    assert [(name, value) for name, value in _handlers() if "confirm(" in value] == []

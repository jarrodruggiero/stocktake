"""What a browser would send from a form on a page, read off its HTML."""

from __future__ import annotations

from html.parser import HTMLParser


def as_sent(page: str, action: str) -> dict[str, str | list[str]]:
    """Every field of the form posting to `action`, as a browser submits it: a
    checkbox only when ticked, a select as its chosen option, a textarea as
    its text, nothing disabled. A name used more than once is a list."""
    form = _Form(action)
    form.feed(page)
    return {name: values[0] if len(values) == 1 else values
            for name, values in form.fields.items()}


class _Form(HTMLParser):
    def __init__(self, action: str):
        super().__init__()
        self.action = action
        self.fields: dict[str, list[str]] = {}
        self.inside = False
        self.text_of: str | None = None
        self.select: str | None = None

    def _add(self, name: str, value: str) -> None:
        self.fields.setdefault(name, []).append(value)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self.inside = a.get("action") == self.action
        if not self.inside or "disabled" in a:
            return
        if tag == "input" and a.get("name"):
            if a.get("type") not in ("checkbox", "radio") or "checked" in a:
                self._add(a["name"], a.get("value") or "")
        elif tag == "textarea":
            self.text_of = a["name"]
            self._add(a["name"], "")
        elif tag == "select":
            self.select = a["name"]
        elif tag == "option" and self.select and "selected" in a:
            self._add(self.select, a.get("value") or "")

    def handle_endtag(self, tag):
        if tag == "form":
            self.inside = False
        elif tag == "textarea":
            self.text_of = None
        elif tag == "select":
            self.select = None

    def handle_data(self, data):
        if self.text_of:
            self.fields[self.text_of][-1] += data

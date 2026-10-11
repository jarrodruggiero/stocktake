"""How a count is said: "1 trade", "2 trades", never "2 trade(s)"."""

from __future__ import annotations


def plural(count, word: str, words: str | None = None) -> str:
    """`count` and the noun that agrees with it. `words` for a plural that is
    not the word plus "s". A count that is not a number is shown as given."""
    try:
        one = int(count) == 1
    except (TypeError, ValueError):
        one = False
    return f"{count} {word if one else (words or word + 's')}"

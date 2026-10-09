"""One record per form, however many times its Save is pressed.

A form that creates something carries an id minted when it is rendered. The
first POST claims it; a repeat — a double click, a resubmitted page — gets the
first one's answer instead of a second record. A repeat that arrives while
the first is still working waits for it, because the first may be waiting on
the price provider and the person should land where it lands.

Held in memory. The app is one process, and a restart between two presses of
the same button is not worth a table. A refused or failed attempt gives its id
back, so the corrected form can still be saved.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import time

# How long an answered id is remembered, and how many at most. A page left
# open longer than this and resubmitted records again, which is the same as
# filling the form in twice.
TTL_SECONDS = 3600
LIMIT = 10_000
_TOKEN = re.compile(r"[A-Za-z0-9_-]{22,64}")

# token -> (expires_at, answer). The answer is None while the first attempt
# is still working, and the URL it was sent to once it has finished.
_claims: dict[str, tuple[float, str | None]] = {}
_finished: dict[str, asyncio.Event] = {}


def mint() -> str:
    return secrets.token_urlsafe(16)


def valid(token: str) -> bool:
    return bool(_TOKEN.fullmatch(token or ""))


def _prune(now: float) -> None:
    for token in [t for t, (expires, _a) in _claims.items() if expires < now]:
        _claims.pop(token, None)
        _finished.pop(token, None)
    while len(_claims) >= LIMIT:
        oldest = next(iter(_claims))
        _claims.pop(oldest, None)
        _finished.pop(oldest, None)


async def claim(token: str, wait: float = 30.0) -> str | None:
    """None if this caller now owns the token and should go ahead; otherwise
    where the first attempt sent its person, or "/" if it is still working
    after `wait` seconds."""
    now = time.monotonic()
    _prune(now)
    held = _claims.get(token)
    if held is None:
        _claims[token] = (now + TTL_SECONDS, None)
        _finished[token] = asyncio.Event()
        return None
    if held[1] is not None:
        return held[1]
    event = _finished.get(token)
    if event is not None:
        try:
            await asyncio.wait_for(event.wait(), timeout=wait)
        except asyncio.TimeoutError:
            return "/"
    answered = _claims.get(token)
    if answered is None:            # the first gave it back: this one goes ahead
        return await claim(token, wait)
    return answered[1] or "/"


def finish(token: str, target: str) -> None:
    """The first attempt succeeded and sent its person to `target`."""
    _claims[token] = (time.monotonic() + TTL_SECONDS, target)
    event = _finished.pop(token, None)
    if event is not None:
        event.set()


def release(token: str) -> None:
    """The attempt was refused or failed: the id may be used again. A no-op
    once the attempt has finished, so it is safe in a `finally`."""
    held = _claims.get(token)
    if held is None or held[1] is not None:
        return
    _claims.pop(token, None)
    event = _finished.pop(token, None)
    if event is not None:
        event.set()

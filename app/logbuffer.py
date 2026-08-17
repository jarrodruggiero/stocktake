"""The last few hundred log lines, kept in memory for the Settings page.

**Why this exists.** When something goes wrong in a self-hosted app the answer
is in the container's stdout, and getting at that means knowing `kubectl logs`
or `docker logs` and having access to the host. That is a reasonable thing to
ask of the person who deployed it and an unreasonable thing to ask of them at
the moment they are trying to work out why an import failed.

**The level is the whole design.** Error-only says "something broke" and
nothing about what led there; INFO is every HTTP request, which buries the one
line that matters. WARNING and above is the band that carries the useful
middle — a price feed falling back to its secondary source, a statement that
parsed with missing fields, a scheduled job that skipped — without the noise.

Memory only, and bounded. Nothing is written to disk: these lines can quote a
ticker or a filename, and a log file is one more thing to think about when the
promise is that the data stays in the deployment. A restart clears it, which is
also why the real logs remain the source of truth for anything historical.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import deque
from dataclasses import dataclass

# A few hundred lines is minutes of a busy install and days of a quiet one, and
# costs a handful of kilobytes.
CAPACITY = 400


@dataclass(frozen=True)
class Line:
    # A monotonic id, so a page that is tailing can ask for "everything after
    # what I already have" rather than re-fetching the window and working out
    # what is new. Survives eviction: ids keep counting up even as old lines
    # fall off the front.
    seq: int
    when: str
    level: str
    logger: str
    message: str


_lines: deque[Line] = deque(maxlen=CAPACITY)
_next_seq = 0


class BufferHandler(logging.Handler):
    """Formats into the ring buffer. Never raises into the caller.

    A logging handler that throws takes down whatever was being logged, which
    in this app is usually the error path of something already going wrong.
    """

    def emit(self, record: logging.LogRecord) -> None:
        global _next_seq
        try:
            _next_seq += 1
            _lines.append(Line(
                seq=_next_seq,
                when=dt.datetime.fromtimestamp(
                    record.created, dt.timezone.utc).isoformat(timespec="seconds"),
                level=record.levelname,
                logger=record.name,
                message=record.getMessage(),
            ))
        except Exception:                                    # noqa: BLE001
            pass


def install(level: int = logging.WARNING) -> None:
    """Attach to the root logger. Idempotent, so a reload cannot double up."""
    root = logging.getLogger()
    if any(isinstance(h, BufferHandler) for h in root.handlers):
        return
    handler = BufferHandler()
    handler.setLevel(level)
    root.addHandler(handler)


def recent(limit: int = CAPACITY) -> list[Line]:
    """Newest last, which is the order a console is read in."""
    return list(_lines)[-limit:]


def since(seq: int, limit: int = CAPACITY) -> list[Line]:
    """Everything newer than `seq` — what a tailing page asks for.

    If the buffer has wrapped past `seq` (a burst longer than the window while
    the page sat idle), this returns the whole window rather than nothing: the
    reader has missed lines either way, and showing the recent ones is more
    use than showing none.
    """
    fresh = [line for line in _lines if line.seq > seq]
    return fresh[-limit:]


def clear() -> None:
    global _next_seq
    _lines.clear()
    _next_seq = 0

"""Restarting the application from inside it.

A process cannot restart itself; it can stop and rely on whatever started it.
`supervision()` reports which of three situations this is — Kubernetes, Docker
with a restart policy, or neither — and the page words the button accordingly
rather than pretending they are the same (decisions.md #74).

The stop is a SIGTERM to our own process from a background thread, a moment
after the response has gone out, so uvicorn shuts down gracefully and the
browser has the page telling it what happened.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Long enough for the response to be written and the socket flushed, short
# enough that nobody wonders whether the button worked.
STOP_DELAY_SECONDS = 0.75


@dataclass(frozen=True)
class Supervision:
    """Whether stopping will result in starting again."""

    restarts: bool          # will something bring us back?
    certain: bool           # …and do we know that, or are we inferring it?
    platform: str           # what we think we are running under
    detail: str             # the sentence shown next to the button


def _in_container() -> bool:
    if Path("/.dockerenv").exists():
        return True
    # cgroup v1 names the runtime in the path; v2 exposes this file only inside
    # a container. Either is a good enough signal, and neither is load-bearing:
    # being wrong downgrades the wording, it does not break anything.
    try:
        return "docker" in Path("/proc/1/cgroup").read_text() or Path(
            "/run/.containerenv").exists()
    except OSError:
        return False


def supervision() -> Supervision:
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        return Supervision(
            restarts=True, certain=True, platform="Kubernetes",
            detail="Kubernetes starts a replacement immediately, so the "
                   "application will be back within a few seconds.",
        )
    if _in_container():
        return Supervision(
            restarts=True, certain=False, platform="a container",
            detail="Your container runtime will start it again if this "
                   "container has a restart policy — `restart: unless-stopped` "
                   "in a compose file, or `--restart` on `docker run`. Without "
                   "one, the application will stop and stay stopped.",
        )
    return Supervision(
        restarts=False, certain=True, platform="a plain process",
        detail="Nothing here will start it again: this looks like the "
               "application running directly rather than under a container "
               "runtime or service manager. Restarting will stop it, and you "
               "will need to start it yourself.",
    )


def request_stop(reason: str) -> None:
    """Ask this process to shut down gracefully, a moment from now.

    Separated out so tests can replace it: calling the real one under pytest
    would take the test runner with it.
    """
    log.info("restart requested (%s); stopping in %.2fs", reason, STOP_DELAY_SECONDS)

    def _stop() -> None:  # pragma: no cover - sends SIGTERM to this very process; running it under pytest would kill the test session. Verified by hand instead, and `request_stop` is stubbed wherever a route is tested.
        # SIGTERM rather than sys.exit: this runs on a worker thread, and only
        # a signal reaches uvicorn's shutdown handling from here.
        os.kill(os.getpid(), signal.SIGTERM)

    timer = threading.Timer(STOP_DELAY_SECONDS, _stop)
    timer.daemon = True
    timer.start()

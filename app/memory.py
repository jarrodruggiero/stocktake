"""Giving memory back to the operating system after the price feed runs.

Python frees the feed's pandas frames, but freeing is not returning: glibc
keeps the pages in its arenas, so RSS stays at the high-water mark and the pod
looks like it permanently needs its peak.

Two steps, in order, because neither works alone: `gc.collect()` first, since
pandas leaves reference cycles a refcount drop will not release, then
`malloc_trim(0)` to hand the free arena pages back. `malloc_trim` is a glibc
extension — a no-op on musl and macOS, which is where the tests run.

None of this changes what the app computes, only what the cgroup reports.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import gc
import logging
import os

log = logging.getLogger(__name__)

# The cgroup v2 file the kubelet reads. Absent outside a container, in which
# case we fall back to the process's own RSS.
_CGROUP_CURRENT = "/sys/fs/cgroup/memory.current"


def rss_mib() -> float | None:
    """Memory currently charged to this container, in MiB.

    Prefers the cgroup figure over `/proc/self/status` because that is the
    number the limit is enforced against — it includes page cache and any
    child processes, which VmRSS does not.
    """
    try:
        with open(_CGROUP_CURRENT) as handle:
            return int(handle.read().strip()) / 1048576
    except (OSError, ValueError):
        pass
    try:
        with open("/proc/self/status") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    return None  # macOS and anything else without procfs


def _malloc_trim() -> bool:
    """Return freed arena pages to the OS. False when unavailable."""
    name = ctypes.util.find_library("c")
    if name is None:
        return False
    try:
        libc = ctypes.CDLL(name)
        trim = libc.malloc_trim
    except (OSError, AttributeError):
        # musl has no malloc_trim; macOS's libc has neither the symbol nor the
        # arena behaviour that makes it necessary.
        return False
    trim.argtypes = [ctypes.c_size_t]
    trim.restype = ctypes.c_int
    trim(0)
    return True


def release(context: str = "") -> dict:
    """Collect garbage, then hand the pages back. Logs what it recovered.

    Safe to call from anywhere and safe to call often — it is cheap next to the
    network round-trips it follows, and it never raises: a failure to trim is
    reported, not propagated, because the caller's work is already done.
    """
    before = rss_mib()
    collected = gc.collect()
    trimmed = _malloc_trim()
    after = rss_mib()
    freed = None if (before is None or after is None) else round(before - after, 1)
    log.info(
        "memory release%s: %s objects collected, malloc_trim=%s, RSS %s -> %s MiB (%s)",
        f" after {context}" if context else "",
        collected,
        "yes" if trimmed else "unavailable",
        None if before is None else round(before, 1),
        None if after is None else round(after, 1),
        f"freed {freed} MiB" if freed is not None else "not measurable here",
    )
    return {
        "collected": collected,
        "trimmed": trimmed,
        "rss_before": before,
        "rss_after": after,
        "freed": freed,
    }


def loaded_heavyweights() -> list[str]:
    """Which of the expensive optional imports are currently in memory.

    Used by the tests that keep them lazy, and worth having as a one-line
    answer to "why is this pod using 190 MiB when it is only serving pages".
    """
    import sys

    return sorted(
        name for name in ("yfinance", "pandas", "numpy", "pdfplumber", "openpyxl")
        if name in sys.modules
    )


def log_snapshot(context: str) -> None:
    rss = rss_mib()
    if rss is not None:
        log.info("memory %s: RSS %.1f MiB, loaded=%s",
                 context, rss, ",".join(loaded_heavyweights()) or "none")


# Kept out of the module's public surface but handy when investigating: the
# limit the cgroup will actually kill us at.
def limit_mib() -> float | None:
    try:
        with open(os.path.join(os.path.dirname(_CGROUP_CURRENT), "memory.max")) as h:
            raw = h.read().strip()
        return None if raw == "max" else int(raw) / 1048576
    except (OSError, ValueError):
        return None

"""Keeping the heavyweight imports out of a pod that is only serving pages.

Measured inside the container: a bare interpreter is 8.9 MiB, and importing
yfinance takes it to 116.6 MiB — pandas and numpy come with it. The app used to
pay that at startup, every start, whether or not the feed ever ran. It is more
than half the pod's idle footprint and the reason the memory limit is 512 Mi.

So `pricefeed` imports yfinance on first use and `statements` imports pdfplumber
on first use, and the tests below are what stop either drifting back to module
scope — an easy, invisible regression, because the app works perfectly either
way. Only the memory graph changes.

**Be honest about what this buys in a running deployment: nothing, when the
feed is on.** `main._feed_loop` does a catch-up run at startup, so the import
these tests defer happens ~6 seconds after boot anyway and stays for the life
of the process (measured 2026-08-12: `/proc/1/maps` in the live pod has numpy
and pandas mapped). What laziness genuinely protects is the install that turns
the feed off, plus every CLI entry point and the test suite itself — which is
why these tests spawn fresh interpreters rather than trusting `sys.modules` in
a process the suite has already dirtied. The deployed pod's footprint is a
separate question, and the answer there is the price of yfinance, not the
placement of its import.

`memory.release()` is the other half: freeing pandas' frames is not the same as
returning their pages, and glibc keeps them unless asked. It is tested for
honesty about what it could not do rather than for a number, because the number
depends on the allocator and the platform.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from app import memory

APP_ROOT = Path(__file__).resolve().parent.parent
HEAVY = ("yfinance", "pandas", "numpy", "pdfplumber")


def _in_fresh_interpreter(body: str) -> str:
    """Run a snippet in a new process, with the app importable.

    A fresh interpreter is the whole point: by the time this test file runs,
    another test may already have imported anything, so asking `sys.modules` in
    THIS process would prove nothing about what starting the app costs.
    """
    script = textwrap.dedent(f"""
        import os, sys
        sys.path[:0] = [{str(APP_ROOT)!r}, {str(APP_ROOT.parent.parent / "libs" / "appkit")!r}]
        os.environ.setdefault("APP_CONFIG_FILE", {str(APP_ROOT / "tests" / "config.test.yaml")!r})
        os.environ.setdefault("APP_DATABASE__TYPE", "sqlite")
        os.environ.setdefault("APP_DATABASE__PATH", "/tmp/pf-memtest.db")
        {body}
    """)
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=180
    )
    assert result.returncode == 0, f"probe failed:\n{result.stdout}\n{result.stderr}"
    return result.stdout.strip()


# --------------------------------------------------------------------------- #
# The imports stay lazy
# --------------------------------------------------------------------------- #

@pytest.mark.slow
def test_importing_the_app_does_not_load_the_heavyweights():
    """The regression this whole task is about. If someone moves `import
    yfinance` back to the top of pricefeed.py, every pod grows ~108 MiB at
    startup and nothing else changes — no test fails, no page breaks. This one
    fails."""
    loaded = _in_fresh_interpreter("""
        import app.main  # noqa: F401
        print(",".join(sorted(m for m in %r if m in sys.modules)))
    """ % (HEAVY,))

    assert loaded == "", f"app import pulled in: {loaded}"


@pytest.mark.slow
def test_the_feed_loads_yfinance_when_it_actually_runs():
    """The mirror: lazy must mean deferred, not broken. `_yahoo()` has to
    really import the library when something needs it."""
    loaded = _in_fresh_interpreter("""
        from app import pricefeed
        assert pricefeed._yf is None, "cached before first use"
        module = pricefeed._yahoo()
        assert module is not None
        assert pricefeed._yf is module, "second call must reuse the import"
        print("yfinance" if "yfinance" in sys.modules else "missing")
    """)

    assert loaded == "yfinance"


@pytest.mark.slow
def test_statements_loads_pdfplumber_only_when_parsing():
    loaded = _in_fresh_interpreter("""
        from app import statements
        before = "pdfplumber" in sys.modules
        try:
            statements.extract_text(b"not a pdf")
        except Exception:
            pass  # it is not a PDF; we only care that the import happened
        print(f"{before},{'pdfplumber' in sys.modules}")
    """)

    assert loaded == "False,True"


# --------------------------------------------------------------------------- #
# release()
# --------------------------------------------------------------------------- #

def test_release_collects_and_reports_what_it_did():
    result = memory.release("a test")

    assert result["collected"] >= 0
    assert isinstance(result["trimmed"], bool)
    # rss/freed are None off Linux; the shape must be stable either way so the
    # log line and any future dashboard can rely on it.
    assert set(result) == {"collected", "trimmed", "rss_before", "rss_after", "freed"}


def test_release_never_raises_when_the_platform_cannot_trim(monkeypatch):
    """`malloc_trim` is a glibc extension: absent on musl and on macOS. A feed
    run must not fail because the allocator wouldn't co-operate — the work it
    just did is already committed."""
    monkeypatch.setattr(memory.ctypes.util, "find_library", lambda _name: None)

    result = memory.release()

    assert result["trimmed"] is False


def test_release_survives_a_libc_without_the_symbol(monkeypatch):
    class _NoTrim:
        def __getattr__(self, name):
            raise AttributeError(name)

    monkeypatch.setattr(memory.ctypes, "CDLL", lambda _name: _NoTrim())

    assert memory.release()["trimmed"] is False


def test_rss_is_read_from_the_cgroup_when_there_is_one(monkeypatch, tmp_path):
    """The cgroup figure, not VmRSS: that is the number the limit is enforced
    against, and the one that gets the pod OOM-killed."""
    current = tmp_path / "memory.current"
    current.write_text(str(200 * 1048576))
    monkeypatch.setattr(memory, "_CGROUP_CURRENT", str(current))

    assert memory.rss_mib() == 200.0


def test_rss_falls_back_to_proc_when_there_is_no_cgroup(monkeypatch, tmp_path):
    monkeypatch.setattr(memory, "_CGROUP_CURRENT", str(tmp_path / "absent"))

    value = memory.rss_mib()

    # Linux has /proc/self/status and returns a number; macOS has neither and
    # returns None. Both are correct — what must not happen is an exception.
    assert value is None or value > 0


def test_a_garbled_cgroup_file_is_ignored_rather_than_fatal(monkeypatch, tmp_path):
    current = tmp_path / "memory.current"
    current.write_text("not a number")
    monkeypatch.setattr(memory, "_CGROUP_CURRENT", str(current))

    value = memory.rss_mib()

    assert value is None or value > 0  # fell through to /proc, or gave up


def test_the_limit_is_read_when_one_is_set(monkeypatch, tmp_path):
    (tmp_path / "memory.current").write_text("1")
    (tmp_path / "memory.max").write_text(str(512 * 1048576))
    monkeypatch.setattr(memory, "_CGROUP_CURRENT", str(tmp_path / "memory.current"))

    assert memory.limit_mib() == 512.0


def test_an_unlimited_cgroup_reports_no_limit(monkeypatch, tmp_path):
    (tmp_path / "memory.current").write_text("1")
    (tmp_path / "memory.max").write_text("max")
    monkeypatch.setattr(memory, "_CGROUP_CURRENT", str(tmp_path / "memory.current"))

    assert memory.limit_mib() is None


def test_the_limit_is_none_outside_a_cgroup(monkeypatch, tmp_path):
    monkeypatch.setattr(memory, "_CGROUP_CURRENT", str(tmp_path / "nothing" / "memory.current"))

    assert memory.limit_mib() is None


def test_loaded_heavyweights_reports_what_is_in_memory():
    """Used in the startup log line, so a 190 MiB pod can be explained without
    attaching a profiler to it."""
    import pandas  # noqa: F401  — deliberately loaded, to have something to find

    loaded = memory.loaded_heavyweights()

    assert "pandas" in loaded
    assert loaded == sorted(loaded)


def test_log_snapshot_does_not_raise_without_a_cgroup(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(memory, "_CGROUP_CURRENT", str(tmp_path / "absent"))

    memory.log_snapshot("in a test")  # must be safe to call anywhere


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #

def test_a_feed_run_releases_memory_afterwards(session_factory, monkeypatch):
    """The trim has to be attached to the thing that allocates. A feed run that
    doesn't release leaves the pod at its high-water mark until it restarts."""
    import datetime as dt
    from types import SimpleNamespace

    from app import pricefeed, tenancy
    from app.settings import PriceFeedSettings

    calls = []
    monkeypatch.setattr(memory, "release", lambda context="": calls.append(context))
    monkeypatch.setattr(pricefeed, "_closes", lambda *a, **k: [])
    monkeypatch.setattr(pricefeed, "_yf", SimpleNamespace(Ticker=lambda _s: None))
    settings = SimpleNamespace(
        price_feed=PriceFeedSettings(backfill_start=dt.date(2020, 1, 1))
    )

    with tenancy.unscoped_session(session_factory) as session:
        pricefeed.run_feed(session, settings)

    assert calls == ["feed run"]

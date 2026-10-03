"""The suite cannot reach Yahoo, and a test that tries fails saying so.

`conftest._no_network` shuts the app's own HTTP helper, but yfinance talks
through curl_cffi, which is C and never touches Python's socket module. A test
that reached `pricefeed.lookup` unstubbed made a real call to Yahoo and passed,
because `lookup` treats any failure as "not found" (decisions.md #53).
"""

from __future__ import annotations

import pytest

from app import pricefeed


def test_yahoo_is_out_of_reach_until_a_test_stubs_it():
    with pytest.raises(pytest.fail.Exception, match="tried to reach Yahoo"):
        pricefeed._yahoo().Ticker("ZULU.AX")


def test_a_lookup_cannot_swallow_it_and_pass_as_if_offline():
    """`lookup` catches Exception, reasonably for the app, so what stops a test
    has to be something it cannot catch."""
    with pytest.raises(pytest.fail.Exception, match="tried to reach Yahoo"):
        pricefeed.lookup("ZULU", "ASX")

"""The suite cannot reach Yahoo, and a test that tries fails saying so.

`conftest._no_network` shuts the app's own HTTP helper, but yfinance talks
through curl_cffi, which is C and never touches Python's socket module. A test
that reached `pricefeed.lookup` unstubbed made a real call to Yahoo and passed,
because `lookup` treats any failure as "not found" (decisions.md #53).
"""

from __future__ import annotations

import threading

import pytest

from app import pricefeed, providers
from appcore import oidc


def test_yahoo_is_out_of_reach_until_a_test_stubs_it():
    with pytest.raises(pytest.fail.Exception, match="tried to reach Yahoo"):
        pricefeed._yahoo().Ticker("ZULU.AX")


def test_a_lookup_cannot_swallow_it_and_pass_as_if_offline():
    """`lookup` catches Exception, reasonably for the app, so what stops a test
    has to be something it cannot catch."""
    with pytest.raises(pytest.fail.Exception, match="tried to reach Yahoo"):
        pricefeed.lookup("ZULU", "ASX")


def test_the_apps_own_requests_cannot_be_swallowed_either(_no_network):
    """`providers.fetch` catches Exception and moves on to the next provider,
    which is how the old AssertionError was swallowed: a test reaching the
    network passed as if it were offline."""
    with pytest.raises(pytest.fail.Exception, match="tried to reach the network"):
        try:
            providers._get_json("https://example.invalid/", {})
        except Exception:                       # what a provider's caller does
            pass
    _no_network.clear()                         # it was meant to happen here


def test_a_request_from_another_thread_still_fails_the_test(_no_network):
    """Nothing raised in a worker thread reaches the test, so the guard keeps
    every attempt, and one left at the end fails the test there."""
    def refresh():
        try:
            oidc._fetch("https://idp.example.invalid/.well-known/openid-configuration")
        except BaseException:                    # an executor keeps it to itself
            pass

    worker = threading.Thread(target=refresh)
    worker.start()
    worker.join()

    assert _no_network == ["https://idp.example.invalid/.well-known/openid-configuration"]
    _no_network.clear()


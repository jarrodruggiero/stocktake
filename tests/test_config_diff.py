"""What the wizard asks somebody to add when it cannot write the file itself.

Issue #18: a reader who had written their own config.yaml — timezone set, price
feed on — reached the last step and was told to set both. The wizard applies
its choices to the RUNNING settings before drawing that page, so comparing
against those says everything matches; the comparison has to be against what
survives a restart.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app import configfile


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    """A real config.yaml on disk, which is what `load()` reads."""
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(configfile, "CONFIG_FILE", str(path))
    return path


def test_a_value_the_file_already_has_is_not_asked_for(config_file):
    config_file.write_text("timezone: Australia/Melbourne\n")

    outstanding = configfile.not_yet_provided({"timezone": "Australia/Melbourne"})

    assert outstanding == {}


def test_a_value_the_file_has_differently_is_asked_for(config_file):
    config_file.write_text("timezone: Europe/Dublin\n")

    outstanding = configfile.not_yet_provided({"timezone": "Australia/Melbourne"})

    assert outstanding == {"timezone": "Australia/Melbourne"}


def test_only_the_missing_half_of_a_nested_block_is_asked_for(config_file):
    """The reporter's case: the feed was already on, the hour was not. Being
    told to set both is what made the page read as though nothing was read."""
    config_file.write_text("price_feed:\n  enabled: true\n")

    outstanding = configfile.not_yet_provided(
        {"price_feed": {"enabled": True, "hour": 18}})

    assert outstanding == {"price_feed": {"hour": 18}}


def test_a_booleans_spelling_does_not_make_it_look_different(config_file):
    """YAML gives back `True` where a form gave "true". Both mean the same, and
    a diff that reported them as different would send somebody to change a line
    into itself."""
    config_file.write_text("price_feed:\n  enabled: true\n")

    assert configfile.not_yet_provided({"price_feed": {"enabled": True}}) == {}


def test_a_value_an_environment_variable_supplies_is_not_asked_for(
    config_file, monkeypatch
):
    """An env var wins at load and survives a restart, so there is nothing to
    write down. Telling somebody to add it would be telling them to duplicate
    a setting that is already winning."""
    config_file.write_text("")
    monkeypatch.setenv("APP_TIMEZONE", "Australia/Melbourne")

    assert configfile.not_yet_provided({"timezone": "Australia/Melbourne"}) == {}


def test_an_empty_file_is_asked_for_everything(config_file):
    config_file.write_text("")
    wanted = {"timezone": "Australia/Melbourne",
              "price_feed": {"enabled": True}}

    assert configfile.not_yet_provided(wanted) == wanted


def test_a_missing_file_is_asked_for_everything(tmp_path, monkeypatch):
    monkeypatch.setattr(configfile, "CONFIG_FILE", str(tmp_path / "nothing.yaml"))

    assert configfile.not_yet_provided({"timezone": "X"}) == {"timezone": "X"}


def test_an_unreadable_file_asks_for_everything_rather_than_guessing(
    config_file, monkeypatch
):
    """Unreadable is not "absent". Saying "nothing to change" because we could
    not look would be worse than asking for something already there."""
    config_file.write_text("timezone: Australia/Melbourne\n")

    def refuse():
        raise PermissionError("nope")

    monkeypatch.setattr(configfile, "load", refuse)

    assert configfile.not_yet_provided({"timezone": "Australia/Melbourne"}) == {
        "timezone": "Australia/Melbourne"}


def test_a_date_compares_by_what_it_reads_as(config_file):
    """`backfill_start` is a date in the draft and a date in YAML; they must
    not look different for being different Python objects."""
    config_file.write_text("price_feed:\n  backfill_start: 2020-01-01\n")

    outstanding = configfile.not_yet_provided(
        {"price_feed": {"backfill_start": dt.date(2020, 1, 1)}})

    assert outstanding == {}

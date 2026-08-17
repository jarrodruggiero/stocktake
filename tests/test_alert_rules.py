"""The shipped alerting rules in `deploy/monitoring/`.

**Why a monitoring template gets tests at all.** A rule that can never fire
looks exactly like a system that is healthy. There is no error, no log line and
no red panel — it just sits there being quiet, and you find out it was wrong on
the day you needed it. The two failure modes that produce that are both
mechanical, so both are checked here:

  * an alert referring to a metric this app does not publish — one typo in
    `stocktake_last_price_date_seconds` and the expression evaluates to nothing,
    forever;
  * the plain rules file and the operator's `PrometheusRule` drifting apart,
    because two copies of an alert is how one of them ends up stale.

The thresholds themselves are judgement, not fact, so they are argued in
comments in the YAML rather than asserted here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from test_metrics import ALLOWED

from app import metrics

MONITORING = Path(__file__).resolve().parent.parent / "deploy" / "monitoring"
PLAIN = MONITORING / "prometheus-rules.yaml"
OPERATOR = MONITORING / "prometheusrule.yaml"


def load(path: Path):
    from ruamel.yaml import YAML

    return YAML(typ="safe").load(path.read_text())


@pytest.fixture(scope="module")
def plain():
    return load(PLAIN)


@pytest.fixture(scope="module")
def rules(plain):
    return [rule for group in plain["groups"] for rule in group["rules"]]


# --------------------------------------------------------------------------- #
# The rules refer to metrics that exist
# --------------------------------------------------------------------------- #

def test_every_metric_referenced_is_one_the_app_publishes(rules):
    """The typo check, and the reason this file exists.

    `time() - stocktake_last_price_seconds > x` is valid PromQL over a series
    that does not exist: it evaluates to the empty vector and the alert is
    silent for the life of the deployment.
    """
    referenced = set()
    for rule in rules:
        referenced |= set(re.findall(r"\bstocktake_[a-z_]+\b", rule["expr"]))

    unknown = referenced - ALLOWED
    assert not unknown, (
        f"alert rules reference metrics the app does not publish: {sorted(unknown)}. "
        f"Published: {sorted(ALLOWED)}"
    )


def test_the_metrics_the_rules_depend_on_are_actually_rendered(rules, db):
    """`ALLOWED` is a list in a test file; this checks it against the real
    exposition, so a metric that was renamed in `app/metrics.py` but left in
    the rules is caught even if both test files were updated together."""
    from factories import add_fx, add_price, make_instrument

    inst = make_instrument(db, "ALPHA")
    add_price(db, inst, "2026-08-01", "101.00")
    add_fx(db, "USDAUD", "2026-08-01", "1.53")
    db.commit()
    metrics.record_feed_run(ok=True)

    rendered = {line.split(" ")[0] for line in metrics.render(db).splitlines()
                if line and not line.startswith("#")}

    for rule in rules:
        for name in re.findall(r"\bstocktake_[a-z_]+\b", rule["expr"]):
            assert name in rendered, f"{rule['alert']} watches {name}, which is never rendered"


# --------------------------------------------------------------------------- #
# The two files stay the same rules
# --------------------------------------------------------------------------- #

def test_the_operator_resource_carries_the_same_groups(plain):
    operator = load(OPERATOR)

    assert operator["kind"] == "PrometheusRule"
    assert operator["spec"]["groups"] == plain["groups"], (
        "deploy/monitoring/prometheusrule.yaml has drifted from "
        "prometheus-rules.yaml. Regenerate it rather than editing both."
    )


def test_the_operator_resource_carries_a_release_label():
    """Without a label the operator's selector matches, the rules apply
    cleanly and are then never evaluated — the quietest possible failure."""
    operator = load(OPERATOR)

    assert operator["metadata"]["labels"].get("release")


# --------------------------------------------------------------------------- #
# Shape — things a linter would catch if we ran one, and we do not
# --------------------------------------------------------------------------- #

def test_every_alert_has_a_severity_and_a_summary(rules):
    for rule in rules:
        assert rule["labels"]["severity"] in ("critical", "warning", "info"), rule["alert"]
        assert rule["annotations"]["summary"].strip(), rule["alert"]


def test_every_alert_name_is_namespaced_to_this_app(rules):
    """Alert names share a flat namespace across everything one Alertmanager
    routes. `PricesStale` from two different apps is one silence and two
    surprises.

    The prefix is the app's NAME, so it moved with the rename to Stocktake —
    and this test is what made that a two-line change rather than a hunt: it
    failed on every rule the sweep had missed."""
    for rule in rules:
        assert rule["alert"].startswith("Stocktake"), rule["alert"]


def test_alert_names_are_unique(rules):
    names = [rule["alert"] for rule in rules]
    assert len(names) == len(set(names))


def test_gauge_comparisons_wait_before_firing(rules):
    """Every rule here watches something that changes at most daily, so a
    single missed scrape must not page anyone. `for:` is what makes that true,
    and it is easy to leave off."""
    for rule in rules:
        assert "for" in rule, f"{rule['alert']} fires on a single evaluation"


def test_the_readme_documents_every_alert():
    """A rule nobody can look up gets deleted by the next person to see it
    fire at 3am."""
    readme = (MONITORING / "README.md").read_text()
    for rule in [r for g in load(PLAIN)["groups"] for r in g["rules"]]:
        assert rule["alert"] in readme, f"{rule['alert']} is not in the README"

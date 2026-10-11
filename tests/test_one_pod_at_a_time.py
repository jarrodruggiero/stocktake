"""One pod at a time, whatever the database.

The chart chose Recreate on SQLite and RollingUpdate on Postgres, as if one
writer were the only reason for one replica. It is not: the import preview,
the statement mapper and the price feed each live in the one process. During
a rolling update the new pod migrated while the old code served, two price
feeds ran, and a preview committed on the other pod was lost. The data
volume is ReadWriteOnce on either database, so the new pod could also hang
on Multi-Attach.
"""

from __future__ import annotations

import pytest
from test_release_version import CHART, _deployment, _helm_template, needs_helm

DATABASES = {
    "sqlite": [],
    "postgres": ["--set", "database.postgres.host=db",
                 "--set", "database.postgres.existingSecret=stocktake-db"],
}


@needs_helm
@pytest.mark.parametrize("database", sorted(DATABASES))
def test_an_upgrade_stops_the_old_pod_before_starting_the_new(database):
    rendered = _helm_template(str(CHART), "--set", "image.tag=0.67.1",
                              "--set", f"database.type={database}", *DATABASES[database])

    assert rendered.returncode == 0, rendered.stderr
    spec = _deployment(rendered.stdout)["spec"]
    assert spec["replicas"] == 1
    assert spec["strategy"] == {"type": "Recreate"}

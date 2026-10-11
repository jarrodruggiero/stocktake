"""When each price, rate, instrument and holding setting last changed.

The series cache fingerprints on these, as on trade.updated_at (decisions.md
#91): each is rewritten in place, which moves neither a row count nor a latest
date. Existing rows start empty, which the fingerprint reads as no change.

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None

TABLES = ("price", "fx_rate", "instrument", "holding_pref")


def upgrade() -> None:
    for table in TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(sa.Column("updated_at", sa.DateTime(timezone=True),
                                          nullable=True))


def downgrade() -> None:
    for table in reversed(TABLES):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_column("updated_at")

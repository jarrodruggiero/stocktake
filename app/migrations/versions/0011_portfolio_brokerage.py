"""What a portfolio's broker charges: a fee for local trades and one for foreign.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None

COLUMNS = (
    ('brokerage_flat', sa.Numeric(10, 2)),
    ('brokerage_percent', sa.Numeric(7, 4)),
    ('brokerage_minimum', sa.Numeric(10, 2)),
    ('foreign_brokerage_flat', sa.Numeric(10, 2)),
    ('foreign_brokerage_percent', sa.Numeric(7, 4)),
    ('foreign_brokerage_minimum', sa.Numeric(10, 2)),
)


def upgrade() -> None:
    with op.batch_alter_table('portfolio', schema=None) as batch_op:
        for name, kind in COLUMNS:
            batch_op.add_column(sa.Column(name, kind, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('portfolio', schema=None) as batch_op:
        for name, _kind in reversed(COLUMNS):
            batch_op.drop_column(name)

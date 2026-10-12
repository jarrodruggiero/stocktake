"""A distribution's period end: the year a trust's income belongs to.

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0015'
down_revision = '0014'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("dividend", schema=None) as batch_op:
        batch_op.add_column(sa.Column("period_end", sa.Date(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("dividend", schema=None) as batch_op:
        batch_op.drop_column("period_end")

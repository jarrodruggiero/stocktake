"""Foreign tax withheld from a distribution.

Revision ID: 0016
Revises: 0015
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0016'
down_revision = '0015'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("dividend", schema=None) as batch_op:
        batch_op.add_column(sa.Column("withholding_tax", sa.Numeric(14, 4), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("dividend", schema=None) as batch_op:
        batch_op.drop_column("withholding_tax")

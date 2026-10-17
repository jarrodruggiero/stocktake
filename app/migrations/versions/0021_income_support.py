"""Whether a portfolio's taxpayer gets income support, which lifts the 2027
minimum tax on capital gains.

Revision ID: 0021
Revises: 0020
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0021'
down_revision = '0020'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("portfolio") as batch:
        batch.add_column(sa.Column("income_support", sa.Boolean(), nullable=False,
                                   server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("portfolio") as batch:
        batch.drop_column("income_support")

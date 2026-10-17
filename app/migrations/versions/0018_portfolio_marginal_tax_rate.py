"""The marginal tax rate a portfolio's gains are taxed at, for the sale simulator.

Revision ID: 0018
Revises: 0017
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0018'
down_revision = '0017'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('portfolio', schema=None) as batch_op:
        batch_op.add_column(sa.Column('marginal_tax_rate', sa.Numeric(5, 2), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('portfolio', schema=None) as batch_op:
        batch_op.drop_column('marginal_tax_rate')

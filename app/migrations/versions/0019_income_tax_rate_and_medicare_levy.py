"""A portfolio's income tax rate and Medicare levy, kept apart.

0018's one rate was asked for with the 2% Medicare levy in it. From 1 July 2027
the minimum tax on capital gains compares 30% with income tax alone, so the two
are kept apart: a rate set under 0018 becomes that rate less 2, and a levy of 2
(decisions.md #147). A rate too small to hold the levy is left as it was.

Revision ID: 0019
Revises: 0018
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0019'
down_revision = '0018'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('portfolio', schema=None) as batch_op:
        batch_op.alter_column('marginal_tax_rate', new_column_name='income_tax_rate',
                              existing_type=sa.Numeric(5, 2), existing_nullable=True)
        batch_op.add_column(sa.Column('medicare_levy', sa.Numeric(5, 2), nullable=True))
    op.execute("UPDATE portfolio SET income_tax_rate = income_tax_rate - 2, medicare_levy = 2 "
               "WHERE income_tax_rate >= 2")


def downgrade() -> None:
    op.execute("UPDATE portfolio SET income_tax_rate = income_tax_rate + medicare_levy "
               "WHERE income_tax_rate IS NOT NULL AND medicare_levy IS NOT NULL")
    with op.batch_alter_table('portfolio', schema=None) as batch_op:
        batch_op.drop_column('medicare_levy')
        batch_op.alter_column('income_tax_rate', new_column_name='marginal_tax_rate',
                              existing_type=sa.Numeric(5, 2), existing_nullable=True)

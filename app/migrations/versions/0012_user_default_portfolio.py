"""The portfolio a sign-in opens, chosen in the profile.

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'default_portfolio_id', sa.Integer(),
            sa.ForeignKey('portfolio.id', name='fk_user_default_portfolio',
                          ondelete='SET NULL'),
            nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('default_portfolio_id')

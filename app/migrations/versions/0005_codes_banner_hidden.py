"""Let the recovery-codes banner be put away for one session.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('user_session', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'codes_banner_hidden', sa.Boolean(), nullable=False,
            server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table('user_session', schema=None) as batch_op:
        batch_op.drop_column('codes_banner_hidden')

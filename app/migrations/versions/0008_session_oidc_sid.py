"""Key a session to the provider's own session id, for back-channel logout.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('user_session', schema=None) as batch_op:
        batch_op.add_column(sa.Column('oidc_sid', sa.String(length=255),
                                      nullable=True))
        batch_op.create_index('ix_user_session_oidc_sid', ['oidc_sid'])


def downgrade() -> None:
    with op.batch_alter_table('user_session', schema=None) as batch_op:
        batch_op.drop_index('ix_user_session_oidc_sid')
        batch_op.drop_column('oidc_sid')

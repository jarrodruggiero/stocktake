"""Somewhere to keep an issued WebAuthn challenge between the two requests
of a ceremony.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'webauthn_challenge',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('challenge', sa.String(length=255), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        # Named here rather than `unique=True` on the index — decisions.md #24.
        sa.UniqueConstraint('token_hash', name='uq_webauthn_challenge_token'),
    )
    with op.batch_alter_table('webauthn_challenge', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_webauthn_challenge_token_hash'), ['token_hash'], unique=False)
        batch_op.create_index(
            batch_op.f('ix_webauthn_challenge_user_id'), ['user_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('webauthn_challenge', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_webauthn_challenge_user_id'))
        batch_op.drop_index(batch_op.f('ix_webauthn_challenge_token_hash'))
    op.drop_table('webauthn_challenge')

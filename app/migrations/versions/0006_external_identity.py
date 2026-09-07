"""Accounts as an identity provider knows them, and a password that may be absent.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'external_identity',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('issuer', sa.String(length=255), nullable=False),
        sa.Column('subject', sa.String(length=255), nullable=False),
        sa.Column('email', sa.String(length=320), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        # Named here rather than `unique=True` on the index — decisions.md #24.
        sa.UniqueConstraint('issuer', 'subject', name='uq_external_identity_subject'),
    )
    with op.batch_alter_table('external_identity', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_external_identity_user_id'), ['user_id'], unique=False)
        batch_op.create_index(
            batch_op.f('ix_external_identity_issuer'), ['issuer'], unique=False)

    op.create_table(
        'oidc_state',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('state', sa.String(length=64), nullable=False),
        sa.Column('nonce', sa.String(length=64), nullable=False),
        sa.Column('verifier', sa.String(length=128), nullable=False),
        sa.Column('invite_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['invite_id'], ['portfolio_invite.id'],
                                ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token_hash', name='uq_oidc_state_token'),
    )
    with op.batch_alter_table('oidc_state', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_oidc_state_token_hash'), ['token_hash'], unique=False)

    # An account provisioned by a provider never had a password.
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.alter_column('password_hash', existing_type=sa.String(length=255),
                              nullable=True)


def downgrade() -> None:
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.alter_column('password_hash', existing_type=sa.String(length=255),
                              nullable=False)
    with op.batch_alter_table('oidc_state', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_oidc_state_token_hash'))
    op.drop_table('oidc_state')
    with op.batch_alter_table('external_identity', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_external_identity_issuer'))
        batch_op.drop_index(batch_op.f('ix_external_identity_user_id'))
    op.drop_table('external_identity')

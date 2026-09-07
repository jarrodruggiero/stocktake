"""Invitations to a portfolio: single-use, revocable, hashed.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'portfolio_invite',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('portfolio_id', sa.Integer(), nullable=False),
        sa.Column('role', sa.String(length=20), nullable=False),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('used_by', sa.Integer(), nullable=True),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['portfolio_id'], ['portfolio.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['created_by'], ['user.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['used_by'], ['user.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        # Named here rather than `unique=True` on the index — decisions.md #24.
        sa.UniqueConstraint('token_hash', name='uq_portfolio_invite_token'),
    )
    with op.batch_alter_table('portfolio_invite', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_portfolio_invite_token_hash'), ['token_hash'], unique=False)
        batch_op.create_index(
            batch_op.f('ix_portfolio_invite_portfolio_id'), ['portfolio_id'],
            unique=False)


def downgrade() -> None:
    with op.batch_alter_table('portfolio_invite', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_portfolio_invite_portfolio_id'))
        batch_op.drop_index(batch_op.f('ix_portfolio_invite_token_hash'))
    op.drop_table('portfolio_invite')

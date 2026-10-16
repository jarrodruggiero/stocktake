"""Corporate actions: splits, consolidations, returns of capital and AMIT
cost base adjustments, per portfolio like the trades they change.

Revision ID: 0017
Revises: 0016
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0017'
down_revision = '0016'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "corporate_action",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("portfolio_id", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("ratio_new", sa.Integer(), nullable=True),
        sa.Column("ratio_old", sa.Integer(), nullable=True),
        sa.Column("amount", sa.Numeric(14, 4), nullable=True),
        sa.Column("fx_rate", sa.Numeric(12, 6), nullable=True),
        sa.Column("note", sa.String(length=400), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('split','capital_return','cost_base')",
                           name="corporate_action_kind"),
        sa.CheckConstraint("ratio_new IS NULL OR ratio_new > 0",
                           name="action_ratio_new_positive"),
        sa.CheckConstraint("ratio_old IS NULL OR ratio_old > 0",
                           name="action_ratio_old_positive"),
        sa.ForeignKeyConstraint(["instrument_id"], ["instrument.id"]),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolio.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("corporate_action", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_corporate_action_date"), ["date"], unique=False)
        batch_op.create_index(batch_op.f("ix_corporate_action_instrument_id"),
                              ["instrument_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_corporate_action_portfolio_id"),
                              ["portfolio_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_corporate_action_user_id"), ["user_id"],
                              unique=False)


def downgrade() -> None:
    op.drop_table("corporate_action")

"""The CPI by quarter, which the 2027 CGT rules index a cost base by.

Revision ID: 0020
Revises: 0019
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0020'
down_revision = '0019'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cpi_quarter",
        sa.Column("quarter", sa.Date(), nullable=False),
        sa.Column("index_number", sa.Numeric(9, 3), nullable=False),
        sa.Column("reference_period", sa.String(length=40), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("quarter"),
    )


def downgrade() -> None:
    op.drop_table("cpi_quarter")

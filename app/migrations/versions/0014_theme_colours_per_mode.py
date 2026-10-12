"""Theme colours per mode: one set for light, one for dark.

Each stored colour applied to both modes, so it is copied into both, and every
page looks as it did. Going down keeps the light set.

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from appcore import JSONType

revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None

MODES = ("light", "dark")
users = sa.table("user", sa.column("id", sa.Integer), sa.column("theme_colors", JSONType))


def _rewrite(change) -> None:
    bind = op.get_bind()
    for row in bind.execute(sa.select(users.c.id, users.c.theme_colors)
                            .where(users.c.theme_colors.is_not(None))).all():
        colours = row.theme_colors
        if isinstance(colours, dict):
            bind.execute(users.update().where(users.c.id == row.id)
                         .values(theme_colors=change(colours)))


def upgrade() -> None:
    _rewrite(lambda colours: colours if set(colours) <= set(MODES)
             else {mode: dict(colours) for mode in MODES})


def downgrade() -> None:
    _rewrite(lambda colours: (colours.get("light") or colours.get("dark") or None)
             if set(colours) <= set(MODES) else colours)

"""Backendy runnera ze sposobem uwierzytelnienia (HAR-34).

Dodaje runner.backends: lista {"nazwa", "auth"}, domyslnie pusta.

Revision ID: 0003
Revises: 0002
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "runner",
        sa.Column("backends", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )


def downgrade() -> None:
    op.drop_column("runner", "backends")

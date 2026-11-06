"""Pomiar kroku (HAR-174): koszt, tokeny, tury i session_id w step_run.

Wszystkie kolumny dopuszczaja NULL: brak pomiaru to brak, nigdy zero (measured_or_null).
Wiersze sprzed migracji zostaja z NULL.

Downgrade usuwa kolumny, wiec gubi zapisane pomiary.

Revision ID: 0012
Revises: 0011
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("step_run", sa.Column("cost_usd", sa.Float(), nullable=True))
    op.add_column("step_run", sa.Column("tokens_in", sa.Integer(), nullable=True))
    op.add_column("step_run", sa.Column("tokens_out", sa.Integer(), nullable=True))
    op.add_column("step_run", sa.Column("num_turns", sa.Integer(), nullable=True))
    op.add_column("step_run", sa.Column("session_id", sa.String(length=100), nullable=True))


def downgrade() -> None:
    op.drop_column("step_run", "session_id")
    op.drop_column("step_run", "num_turns")
    op.drop_column("step_run", "tokens_out")
    op.drop_column("step_run", "tokens_in")
    op.drop_column("step_run", "cost_usd")

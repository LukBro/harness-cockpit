"""Pokrycie bramkami w rekordzie runu (HAR-178).

Piec kolumn `run`, wszystkie nullable i bez wartosci domyslnych: run sprzed migracji
i run bez pomiaru maja NULL (brak pomiaru, nigdy zero). Migracja nie przepisuje danych.
Odwzorowanie 1:1 cockpit.db.models.Run.

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
    op.add_column("run", sa.Column("bramki_zaplanowane", sa.Integer(), nullable=True))
    op.add_column("run", sa.Column("bramki_wykonane", sa.Integer(), nullable=True))
    op.add_column("run", sa.Column("bramki_pominiete", sa.Integer(), nullable=True))
    op.add_column("run", sa.Column("stan_pokrycia", sa.String(length=16), nullable=True))
    op.add_column("run", sa.Column("bramki_powody", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("run", "bramki_powody")
    op.drop_column("run", "stan_pokrycia")
    op.drop_column("run", "bramki_pominiete")
    op.drop_column("run", "bramki_wykonane")
    op.drop_column("run", "bramki_zaplanowane")
